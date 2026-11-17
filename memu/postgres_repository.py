"""Markdown bodies with authoritative PostgreSQL control and recoverable publication."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import base64
import json
import os
from pathlib import Path
import threading
import uuid
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .control import RowMap, StateMap, _MISSING
from .events import event
from .errors import RevisionConflict
from .storage import Repository, InvalidReference, _KINDS, _LOCKS, _LOCKS_GUARD, _json, _sha


class PostgresRepository(Repository):
    """One core owns a repository. SQL records and redo intents commit together.

    Changed file bytes are fsynced before the SQL transaction. SQL commits the
    new catalog/state and a publication intent; readers recover that intent
    before opening files. A crash never requires regenerating a model result.
    """
    postgres = True

    def __init__(self, root, dsn, timezone='Asia/Shanghai'):
        self.root = Path(root).expanduser().resolve()
        self.timezone = timezone
        ZoneInfo(timezone)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / '.meta').mkdir(exist_ok=True)
        with _LOCKS_GUARD:
            self._mutex = _LOCKS.setdefault(str(self.root), threading.RLock())
        self._local = threading.local()
        self._frames, self._maps = [], {}
        self._dsn = dsn
        self.db = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        self.json = Jsonb
        self.catalog = self.mapping('catalog', catalog=True)
        self.state = StateMap(self)
        self.db.execute(Path(__file__).with_name('sql').joinpath('control.sql').read_text())
        with self.lock():
            identity = self.db.execute('SELECT * FROM memu_repository').fetchone()
            marker = self.root / '.meta/postgres.json'
            if identity:
                expected = json.loads(marker.read_text())['repository_id'] if marker.exists() else None
                if ((expected is not None and expected != identity['repository_id']) or
                        (expected is None and str(self.root) != identity['root'])):
                    raise ValueError('This database belongs to a different memory repository')
            else:
                if marker.exists():
                    raise ValueError('Authoritative PostgreSQL control state is missing; restore the database backup')
                self._migrate()
                identity = self.db.execute('SELECT * FROM memu_repository').fetchone()
            self.repository_id = identity['repository_id']
            if not marker.exists():
                self._replace_file('.meta/postgres.json', _json({'repository_id': self.repository_id,
                                                               'schema_version': 1}).encode())

    def mapping(self, name, **kwargs):
        if name not in self._maps:
            self._maps[name] = RowMap(self, name, **kwargs)
        return self._maps[name]

    @contextmanager
    def lock(self):
        with self._mutex:
            depth = getattr(self._local, 'depth', 0)
            if depth == 0:
                descriptor = os.open(self.root / '.meta/repository.lock', os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._local.descriptor = descriptor
            self._local.depth = depth + 1
            try:
                if depth == 0:
                    if self.db.closed or self.db.broken:
                        self.db = psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row)
                    self._recover()
                yield self
            finally:
                self._local.depth = depth
                if depth == 0:
                    fcntl.flock(self._local.descriptor, fcntl.LOCK_UN)
                    os.close(self._local.descriptor)

    @contextmanager
    def transaction(self):
        with self.lock():
            depth = getattr(self._local, 'transaction_depth', 0)
            staged = dict(getattr(self._local, 'staged', {}))
            frame = {}
            self._frames.append(frame)
            self._local.transaction_depth = depth + 1
            try:
                yield self
                self._local.transaction_depth = depth
                if depth == 0:
                    self._transaction(getattr(self._local, 'staged', {}))
                    self._local.staged = {}
            except BaseException:
                self._local.transaction_depth = depth
                self._local.staged = staged
                for (_, key), (mapping, old, dirty, removed) in reversed(list(frame.items())):
                    if old is _MISSING:
                        mapping.cache.pop(key, None)
                    else:
                        mapping._remember(key, old)
                    (mapping.dirty.add if dirty else mapping.dirty.discard)(key)
                    (mapping.removed.add if removed else mapping.removed.discard)(key)
                raise
            finally:
                self._frames.pop()

    def _migrate(self):
        """One-time import only. Legacy files remain as a frozen recovery backup."""
        old = self.root / '.meta/catalog.tsv'
        if old.exists():
            # Replay any pre-migration filesystem commit before reading its state.
            Repository._recover(self)
            legacy = object.__new__(Repository)
            legacy.root = self.root
            Repository._load(legacy)
            self.catalog.update(legacy.catalog)
            ignored = {'idempotency', 'pending', 'tasks', 'generation', 'deleted'}
            for key, value in legacy.state.items():
                if key not in ignored:
                    self.state[key] = value
            for alias in self.catalog:
                row = self.catalog[alias]
                if row['kind'] in {'source', 'media'}:
                    row['metadata'] = self.prepare_metadata(row['metadata'])
            for request in self.state['writes'].values():
                request['metadata'] = self.prepare_metadata(request.get('metadata', {}))
                if request.get('state') == 'done':
                    # Vectors awaiting synchronization live in staged_vectors;
                    # completed model drafts are not needed for recovery.
                    for field in ('drafts', 'processed', 'digests', 'digest_refs', 'compact', 'input_revisions'):
                        request.pop(field, None)
        defaults = {'deleted_refs': [], 'deletion_epoch': 0, 'index_dirty': True,
                    'blanked_ranges': [], 'media_blanked_ranges': []}
        for key, value in defaults.items():
            self.state.setdefault(key, value)
        # Legacy indexes do not contain the new materialized evidence mappings.
        # They cannot serve dependency queries until the initial delta is applied.
        self.state['index_dirty'] = True
        self.state['topic_registry'].setdefault('job', {'state': 'done', 'generation': 0})
        with self.db.transaction():
            self.db.execute('INSERT INTO memu_repository(root,repository_id) VALUES (%s,%s)',
                            (str(self.root), uuid.uuid4().hex))
            for mapping in self._maps.values():
                mapping.flush()
            for kind, prefix in _KINDS.items():
                number = max((int(key[1:]) for key in self.catalog if key.startswith(prefix)), default=0)
                self.db.execute('INSERT INTO memu_counters(kind,value) VALUES (%s,%s)', (kind, number))
            self.db.execute("INSERT INTO memu_control(namespace,key,value) SELECT 'dirty_files',alias,to_jsonb(revision) FROM memu_catalog ON CONFLICT DO NOTHING")
        for mapping in self._maps.values():
            mapping.committed()
        event('control_migrated', count=len(self.catalog))

    def _recover(self):
        rows = self.db.execute('SELECT id,files FROM memu_commits ORDER BY created_at,id').fetchall()
        for commit in rows:
            for file in commit['files']:
                staged, target = self._path(file['staged']), self._path(file['path'])
                if staged.exists():
                    data = staged.read_bytes()
                    if _sha(data) != file['revision']:
                        raise RuntimeError('Publication checksum mismatch')
                    self._replace_file(file['path'], data)
                elif not target.exists() or _sha(target.read_bytes()) != file['revision']:
                    raise RuntimeError('Committed publication bytes are missing')
            self.db.execute('DELETE FROM memu_commits WHERE id=%s', (commit['id'],))
            for file in commit['files']:
                self._path(file['staged']).unlink(missing_ok=True)
            event('files_published', commit_id=commit['id'], count=len(commit['files']))

    def _transaction(self, writes, expected_revisions=None):
        for alias, revision in (expected_revisions or {}).items():
            if self.revision(alias) != revision:
                raise RevisionConflict('Input revision changed')
        payload = {key: value.encode() if isinstance(value, str) else value for key, value in writes.items()}
        for path, data in payload.items():
            self._path(path)
            alias = self.alias_for_path(path)
            if alias:
                revision = _sha(data)
                self.catalog[alias]['revision'] = revision
                self.state['dirty_files'][alias] = revision
                self.state['index_dirty'] = True
        if getattr(self._local, 'transaction_depth', 0):
            self._local.staged = {**getattr(self._local, 'staged', {}), **payload}
            return
        commit_id = uuid.uuid4().hex
        files = []
        for position, (path, data) in enumerate(payload.items()):
            staged = f'.meta/publications/{commit_id}-{position}'
            self._replace_file(staged, data)
            files.append({'path': path, 'staged': staged, 'revision': _sha(data)})
        try:
            with self.db.transaction():
                for mapping in self._maps.values():
                    mapping.flush()
                if files:
                    self.db.execute('INSERT INTO memu_commits(id,files) VALUES (%s,%s)',
                                    (commit_id, Jsonb(files)))
                    self.db.execute('UPDATE memu_repository SET content_version=content_version+1')
        except (psycopg.OperationalError, psycopg.InterfaceError):
            # An uncertain COMMIT must retain staged bytes. Recovery checks SQL.
            for frame in self._frames:
                frame.clear()
            for mapping in self._maps.values():
                mapping.cache.clear()
                mapping.dirty.clear()
                mapping.removed.clear()
            self.db.close()
            raise
        for mapping in self._maps.values():
            mapping.committed()
        for frame in self._frames:
            frame.clear()  # SQL is committed; a publication error must not undo it.
        self._recover()

    def save_state(self):
        with self.lock():
            self._transaction({})

    def alias_for_path(self, path):
        for alias in self.catalog.dirty:
            if alias not in self.catalog.removed and self.catalog.cache[alias]['path'] == path:
                return alias
        row = self.db.execute('SELECT alias FROM memu_catalog WHERE path=%s', (path,)).fetchone()
        return row['alias'] if row else None

    def _allocate(self, kind, path, metadata=None):
        self._path(path)
        alias = self.alias_for_path(path)
        if alias:
            if self.catalog[alias]['kind'] != kind:
                raise ValueError('Existing file has a different kind')
            return alias
        number = int(Path(path).stem[1:]) if kind in {'source', 'media'} else self.next_number(kind)
        alias = _KINDS[kind] + str(number)
        self.catalog[alias] = {'alias': alias, 'kind': kind, 'path': path,
                               'revision': '', 'metadata': metadata or {}}
        return alias

    def next_number(self, kind):
        row = self.db.execute('''INSERT INTO memu_counters(kind,value) VALUES (%s,1)
            ON CONFLICT(kind) DO UPDATE SET value=memu_counters.value+1 RETURNING value''', (kind,)).fetchone()
        return row['value']

    _new_number = next_number

    def prepare_metadata(self, metadata):
        if not metadata or metadata.get('metadata_ref'):
            return deepcopy(metadata or {})
        metadata = deepcopy(metadata)
        audit = metadata.get('memulife', {}).get('transport_audit')
        if isinstance(audit, dict):
            for record in audit.get('records', [audit]):
                raw = record.pop('raw_utf8', None)
                data = raw.encode('utf-8') if raw is not None else None
                if data is None and isinstance(record.get('raw'), str):
                    data = base64.b64decode(record.pop('raw'), validate=True)
                if data is not None:
                    path = '.meta/source-originals/' + _sha(data) + '.bin'
                    if not self._exists(path):
                        self._replace_file(path, data)
                    record['raw_ref'] = path
        encoded = _json(metadata).encode()
        path = '.meta/material-metadata/' + _sha(encoded) + '.json'
        if not self._exists(path):
            # Immutable metadata is durable before a SQL row can reference it.
            # An aborted transaction may leave an unreferenced blob, never a
            # reference to missing bytes or another full catalog rewrite.
            self._replace_file(path, encoded)
        small = {key: metadata[key] for key in ('projection', 'operation', 'view', 'scope', 'source', 'time',
                 'mime_type', 'modality', 'duration', 'original_filename') if key in metadata}
        origin = metadata.get('memulife', {})
        small['memulife'] = {key: origin[key] for key in (
            'adapter_id', 'source_profile', 'source_instance_id', 'source_namespace', 'object_id',
            'revision', 'raw_sha256') if key in origin}
        small['content'] = {key: metadata.get('content', {})[key] for key in ('material_kind', 'audit_only', 'projection_version', 'evidence_contract', 'projection_counts',
                            'unit_limit', 'sequential_relations', 'conversation_id', 'turn_id')
                            if key in metadata.get('content', {})}
        small['metadata_ref'] = path
        return small

    def full_metadata(self, value):
        metadata = value.get('metadata', value)
        path = metadata.get('metadata_ref')
        if path:
            if not path.startswith('.meta/material-metadata/'):
                raise ValueError('Invalid metadata reference')
            return json.loads(self._bytes(path))
        return metadata

    def original_audit(self, metadata):
        audit = deepcopy(self.full_metadata(metadata).get('memulife', {}).get('transport_audit', {}))
        for record in audit.get('records', [audit]):
            path = record.get('raw_ref')
            if path:
                if not path.startswith('.meta/source-originals/'):
                    raise ValueError('Invalid original source reference')
                record['raw_utf8'] = self._bytes(path).decode('utf-8')
        return audit

    def revision(self, alias):
        with self.lock():
            alias = alias.split(':', 1)[0].split('@', 1)[0]
            try:
                return self.catalog[alias]['revision']
            except KeyError:
                raise InvalidReference('Unknown alias') from None

    def view_ref(self, view, scope, create=False):
        with self.lock():
            alias = self.alias_for_path(self._view_path(view, scope))
            return alias or (self.write_view(view, scope, []) if create else None)

    def version(self, field='content_version'):
        if field not in {'content_version', 'search_version', 'grep_version'}:
            raise ValueError('Invalid version field')
        with self.lock():
            return self.db.execute(f'SELECT {field} FROM memu_repository').fetchone()[field]

    def ready_write_keys(self, now, *, limit=1, exclude=()):
        with self.lock():
            return [r['key'] for r in self.db.execute('''SELECT key FROM memu_control WHERE namespace='writes'
                AND NOT(key=ANY(%s)) AND (value->>'state' IN ('queued','running') OR (value->>'state'='waiting'
                     AND (value->>'retry_at')::double precision<=%s))
                ORDER BY COALESCE((value->>'created_at')::double precision,0),key LIMIT %s''',
                (list(exclude), now, limit))]

    def next_write_due(self, *, exclude=()):
        with self.lock():
            if not exclude and self.db.execute("SELECT 1 FROM memu_control WHERE namespace='projection_updates' LIMIT 1").fetchone():
                return 0
            row = self.db.execute('''SELECT min(CASE WHEN value->>'state' IN ('queued','running') THEN 0
                ELSE (value->>'retry_at')::double precision END) AS due FROM memu_control
                WHERE namespace='writes' AND NOT(key=ANY(%s))
                  AND value->>'state' IN ('queued','running','waiting')''', (list(exclude),)).fetchone()
            return row['due']

    def write_backlog_at_least(self, count):
        with self.lock():
            return bool(self.db.execute("""SELECT 1 FROM memu_control WHERE namespace='writes'
                AND value->>'state' IN ('queued','running') OFFSET %s LIMIT 1""", (count - 1,)).fetchone())

    def write_statuses(self, keys):
        with self.lock():
            rows = self.db.execute("""SELECT key,value - ARRAY['drafts','processed','digests','compact',
                'input_revisions'] AS value FROM memu_control WHERE namespace='writes' AND key=ANY(%s)""",
                (keys,)).fetchall()
            return {row['key']: row['value'] for row in rows}

    def writes_referencing(self, aliases):
        with self.lock():
            keys = [row['request_id'] for row in self.db.execute(
                'SELECT DISTINCT request_id FROM memu_write_inputs WHERE alias=ANY(%s)', (list(aliases),))]
            return [self.state['writes'][key] for key in keys]

    def import_sources(self, source_instance_id, object_ids):
        with self.lock():
            return [row['alias'] for row in self.db.execute('''SELECT alias FROM memu_catalog
                WHERE kind='source' AND payload->'metadata'->'memulife'->>'source_instance_id'=%s
                AND payload->'metadata'->'memulife'->>'object_id'=ANY(%s)''',
                (source_instance_id, object_ids))]

    def browse_catalog(self):
        """One committed metadata snapshot, without one SQL roundtrip per file."""
        with self.lock():
            rows = {r['alias']: r['payload'] for r in self.db.execute(
                'SELECT alias,payload FROM memu_catalog ORDER BY alias')}
            for alias in self.catalog.dirty:
                if alias not in self.catalog.removed:
                    rows[alias] = self.catalog[alias]
            return [row for alias, row in rows.items() if alias not in self.catalog.removed]

    def read(self, ref, *, _document=None):
        from .refs import Ref
        with self.lock():
            record = super().read(ref, _document=_document)
            if record['kind'] in {'unit', 'detail'}:
                provenance = self.state['worklog_evidence'].get(Ref.parse(record['ref']).canonical)
                if provenance:
                    record['provenance'] = dict(provenance)
            return record

    def iter_blocks(self, kinds=None, *, aliases=None):
        with self.lock():
            if aliases is None and kinds is not None:
                aliases = [row['alias'] for row in self.db.execute('''SELECT alias FROM memu_catalog
                    WHERE kind=ANY(%s) OR payload->'metadata'->>'view'=ANY(%s)''', (list(kinds), list(kinds)))]
                aliases = list(dict.fromkeys(aliases + list(self.catalog.dirty - self.catalog.removed)))
            return super().iter_blocks(kinds, aliases=aliases)

    def close(self):
        self.db.close()

    def healthy(self):
        try:
            with self.lock():
                self.db.execute('SELECT 1')
            return True
        except psycopg.Error:
            return False
