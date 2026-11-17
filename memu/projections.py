"""Durable replacement of a projection while retaining source-based idempotency."""
from copy import deepcopy
import re

from .persistence import digest
from .refs import Ref
from .storage import InvalidReference


def projection_identity(source, metadata):
    value = metadata.get('projection')
    if value is None:
        return None
    if (not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k]
            for k in ('source_instance_id', 'object_id', 'raw_sha256', 'version')) or
            not re.fullmatch('[a-f0-9]{64}', value['raw_sha256']) or
            type(value.get('generation')) is not int or value['generation'] < 1):
        raise ValueError('Projection identity and positive generation are required')
    identity = [value['source_instance_id'], value['object_id'], value['raw_sha256']]
    return digest([source, 'source:' + digest(identity)])


class ProjectionReplacement:
    def retire_projections(self, keys, *, replaced_by='projection-rebuild'):
        """Single-writer operation. Idempotent after an interrupted retirement."""
        originals = [deepcopy(self.repo.state['writes'][key]) for key in keys if key in self.repo.state['writes']]
        aliases = {Ref.parse(ref).alias for row in originals for ref in row.get('source_refs', [])}
        sources = []
        for row in originals:
            for ref in row.get('source_refs', []):
                try:
                    self.repo.read(ref); sources.append(ref)
                except (InvalidReference, FileNotFoundError):
                    pass
        blocks = (self.index.dependent_blocks(sources) if sources and self.index is not None and
                  hasattr(self.index, 'dependent_blocks') and not self.repo.state['index_dirty'] else
                  self.repo.iter_blocks(['unit', 'detail']))
        derived = set()
        while True:
            found = {b['ref'] for b in blocks if b['kind'] in {'unit', 'detail'} and
                     any(Ref.parse(f).alias in aliases or any(self._overlaps(f,t) for t in derived) for f in b.get('facts', []))}
            if found <= derived:
                break
            derived |= found
        units = [b['ref'] for b in blocks if b['kind'] == 'unit' and b['ref'] in derived]
        details = [b['ref'] for b in blocks if b['kind'] == 'detail' and b['ref'] in derived]
        # Automatic re-import may retire generated Units, but never a manual
        # revision or the evidence needed to keep its stable references valid.
        protected = set(units) & set(self.repo.state.get('unit_pins', {}))
        kept_aliases, visited = set(), set()
        pending = [fact for ref in protected for fact in self.repo.read(ref)['facts']]
        while pending:
            ref = pending.pop()
            if ref in visited:
                continue
            visited.add(ref)
            kept_aliases.add(Ref.parse(ref).alias)
            try:
                pending.extend(self.repo.read(ref).get('facts', []))
            except (InvalidReference, FileNotFoundError):
                pass
        units = [ref for ref in units if ref not in protected]
        sources = [ref for ref in sources if Ref.parse(ref).alias not in kept_aliases]
        details = [ref for ref in details if Ref.parse(ref).alias not in kept_aliases]
        with self.repo.transaction():
            for old in originals:
                self._unlink_conversation(old['request_id'], exclude=keys)
            self.repo.save_state()
        if units:
            self._delete(units, with_source=bool(sources + details), source_refs=sources + details or None)
        elif sources or details:
            with self.repo.transaction():
                self.repo.blank_many(sources + details)
                self.repo.state['index_dirty'] = True
                self.repo.save_state()
        with self.repo.transaction():
            for old in originals:
                key = old['request_id']
                # Receipt is deliberately small. Original bytes live in immutable audit blobs/backups.
                receipt = {'fingerprint': old['fingerprint'], 'projection': old.get('metadata', {}).get('projection'),
                           'replaced_by': replaced_by,
                           'retired_units': [ref for ref in old.get('result', {}).get('units', []) if ref not in protected],
                           'retained_units': [ref for ref in old.get('result', {}).get('units', []) if ref in protected]}
                self.repo.state.setdefault('projection_receipts', {})[key + ':' + str(old.get('metadata', {}).get('projection', {}).get('generation', 0))] = receipt
                self.repo.state['writes'][key] = {
                    'request_id': key, 'source': old['source'], 'fingerprint': old['fingerprint'], 'state': 'superseded',
                    'stage': 'retired', 'source_refs': [], 'result': {}, 'metadata': {}, 'superseded_by': replaced_by}
            for alias in aliases:
                if alias in kept_aliases:
                    continue
                row = self.repo.catalog.get(alias)
                if not row:
                    continue
                self.repo._transaction({row['path']: b''})
                row['metadata'] = {'content': {'audit_only': True, 'superseded_by': replaced_by}}
            # A retirement cannot invalidate unrelated queued sources; no other model writer runs concurrently.
            if getattr(self.repo, 'postgres', False):
                active = [r['key'] for r in self.repo.db.execute("SELECT key FROM memu_control WHERE namespace='writes' AND value->>'state' IN ('queued','running','waiting')")]
            else:
                active = list(self.repo.state['writes'])
            for key in active:
                row = self.repo.state['writes'][key]
                if row['state'] in {'queued', 'running', 'waiting'} and not row.get('deleted'):
                    row['epoch'] = self.repo.state['deletion_epoch']
            self.repo.save_state()
        return {'units': len(units), 'details': len(details), 'sources': len(aliases - kept_aliases),
                'protected_units': len(protected)}

    def _activate_projection(self, key):
        with self._write_lock:
            candidate = deepcopy(self.repo.state['projection_updates'][key])
            self.retire_projections([key], replaced_by=candidate['metadata']['projection']['version'])
            with self.repo.transaction():
                candidate['epoch'] = self.repo.state['deletion_epoch']
                self.repo.state['writes'][key] = candidate
                for ref in candidate['source_refs']:
                    alias = Ref.parse(ref).alias
                    self.repo.catalog[alias]['metadata'].get('content', {}).pop('pending_projection', None)
                    self.repo.state['dirty_files'][alias] = self.repo.revision(alias)
                del self.repo.state['projection_updates'][key]
                self.repo.state['index_dirty'] = True
                self.repo.save_state()

    def collect_retired_metadata(self):
        """Explicit maintenance only; never scan metadata as part of a normal write."""
        if not getattr(self.repo, 'postgres', False):
            return 0
        used = {row['ref'] for row in self.repo.db.execute('''
            SELECT payload->'metadata'->>'metadata_ref' AS ref FROM memu_catalog
            UNION SELECT value->'metadata'->>'metadata_ref' FROM memu_control WHERE namespace IN ('writes','projection_updates')
        ''') if row['ref']}
        removed = 0
        for path in (self.repo.root / '.meta/material-metadata').glob('*.json'):
            if str(path.relative_to(self.repo.root)) not in used:
                path.unlink(); removed += 1
        return removed
