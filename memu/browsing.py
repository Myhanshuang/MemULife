"""Owner-only, literal browsing of committed memory; never invokes models."""
import base64
import calendar
from datetime import date, datetime
import hashlib
import json
from zoneinfo import ZoneInfo

from .refs import Ref
from .media_browsing import browse_media, media_index
from .source_display import source_display
from .file_browsing import read_original_file
from .source_policy import audit_without_content, empty_audit_body


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _dates(value, timezone):
    """Inclusive local calendar bounds, including imprecise/range event times."""
    if not value:
        return None, None
    if isinstance(value, dict):
        return _dates(value.get('from'), timezone)[0], _dates(value.get('to'), timezone)[1]
    if 'T' in value:
        day = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(ZoneInfo(timezone)).date().isoformat()
        return day, day
    if len(value) == 4:
        return value + '-01-01', value + '-12-31'
    if len(value) == 7:
        year, month = map(int, value.split('-'))
        return value + '-01', value + f'-{calendar.monthrange(year, month)[1]:02}'
    date.fromisoformat(value)
    return value, value


class MemoryBrowsing:
    def _browse_visible(self, row):
        metadata = row.get('metadata', {})
        content = metadata.get('content', {})
        if metadata.get('active') is False or content.get('superseded_by') or content.get('pending_projection'):
            return False
        if row['kind'] == 'source' and content.get('audit_only'):
            full = self.repo.full_metadata(metadata) if hasattr(self.repo, 'full_metadata') else metadata
            return not audit_without_content(full)
        return True

    def memory_browse_cleanup_audits(self, identity, *, revision=None):
        """Explicit owner maintenance: preview first, then erase only confirmed empty envelopes."""
        self._owner(identity)
        with self._write_lock:
            targets, counts, empty = [], {}, 0
            with self.repo.lock():
                catalog = self.repo.browse_catalog() if hasattr(self.repo, 'browse_catalog') else list(self.repo.catalog.values())
                for row in catalog:
                    if row['kind'] != 'source' or not row['metadata'].get('content', {}).get('audit_only'):
                        continue
                    metadata = self.repo.full_metadata(row['metadata']) if hasattr(self.repo, 'full_metadata') else row['metadata']
                    if not audit_without_content(metadata) or metadata.get('memulife', {}).get('attachments'):
                        continue
                    blocks = self.repo.iter_blocks(['source'], aliases=[row['alias']])
                    if not blocks:
                        empty += 1
                    for block in blocks:
                        if empty_audit_body(block['body']):
                            targets.append(block['ref'])
                            origin = metadata.get('memulife', {})
                            key = origin.get('adapter_id') or 'unknown'
                            counts[key] = counts.get(key, 0) + 1
                plan = self._deletion_plan([], with_source=True, source_refs=targets, allow_sources=True) if targets else None
            if revision is None:
                return {'revision': plan['revision'] if plan else None, 'source_count': len(targets),
                        'by_adapter': counts, 'already_empty': empty,
                        'affected_units': len(plan['affected_units']) if plan else 0,
                        'affected_views': len(plan['affected_views']) if plan else 0}
            if not plan or revision != plan['revision']:
                from .errors import RevisionConflict
                raise RevisionConflict('Audit cleanup changed; review again')
            result = self._delete([], with_source=True, source_refs=targets, allow_sources=True, expected_revision=revision)
            return {**result, 'source_count': len(targets), 'by_adapter': counts, 'already_empty': empty}

    def memory_browse_file(self, identity, *, ref, file_id='original'):
        self._owner(identity)
        if not isinstance(file_id, str):
            raise ValueError('Invalid attachment')
        with self.repo.lock():
            record = self.repo.read(ref)
            row = self.repo.catalog[Ref.parse(ref).alias]
            if row['metadata'].get('active') is False:
                raise ValueError('Memory unavailable')
            return read_original_file(self.repo, row, record['ref'], file_id)

    def _browse_delete_args(self, ref):
        parsed = Ref.parse(ref)
        row = self.repo.catalog.get(parsed.alias)
        if not row or row['metadata'].get('active') is False:
            raise ValueError('Memory unavailable')
        record = self.repo.read(ref)
        if record['kind'] == 'unit':
            return {'refs': [ref], 'with_source': True}
        if record['kind'] in {'source', 'media'}:
            return {'refs': [], 'with_source': True, 'source_refs': [ref], 'allow_sources': True}
        raise ValueError('Only Units and Sources can be deleted')

    def memory_browse_delete_preview(self, identity, *, ref):
        self._owner(identity)
        with self._write_lock, self.repo.lock():
            plan = self._deletion_plan(**self._browse_delete_args(ref))
            counts = {kind: sum(Ref.parse(target).kind == prefix for target in plan['erased'])
                      for kind, prefix in [('unit', 'u'), ('source', 'c'), ('media', 'a'), ('detail', 's')]}
            return {'ref': ref, 'revision': plan['revision'], 'counts': counts,
                    'source_refs': plan['sources'], 'views_to_rebuild': len(plan['affected_views'])}

    def memory_browse_delete(self, identity, *, ref, revision):
        self._owner(identity)
        if not isinstance(revision, str) or len(revision) != 64:
            raise ValueError('Review deletion before committing')
        with self._write_lock:
            return self._delete(**self._browse_delete_args(ref), expected_revision=revision, defer_index=True)

    def _browse_origins(self, block, cache, catalog=None):
        result, seen, pending = {}, set(), [block['ref']]
        while pending:
            ref = pending.pop()
            if ref in seen:
                continue
            seen.add(ref)
            alias = Ref.parse(ref).alias
            row = (catalog if catalog is not None else self.repo.catalog).get(alias)
            if not row:
                continue
            origin = row.get('metadata', {}).get('memulife', {})
            if origin.get('source_instance_id'):
                value = {key: origin.get(key, '') for key in ('source_instance_id', 'source_namespace',
                         'source_profile', 'adapter_id', 'object_id')}
                result[(value['source_instance_id'], value['object_id'])] = value
            elif row['kind'] in {'unit', 'detail'}:
                if ref not in cache:
                    try:
                        cache[ref] = self.repo.read(ref).get('facts', [])
                    except ValueError:
                        cache[ref] = []
                pending.extend(cache[ref])
        return list(result.values())

    def _browse_public(self, block, cache, *, pending=False, catalog=None):
        kind = block.get('view') if block['kind'] == 'view' else block['kind']
        value = {key: block.get(key) for key in ('ref', 'body', 'time', 'source', 'scope', 'facts', 'topics',
                                               'edit_revision', 'protected', 'protected_count', 'invalid_refs')}
        value.update(kind=kind, origins=self._browse_origins(block, cache, catalog), pending_update=bool(pending))
        event_time = block.get('scope') if kind in {'day', 'month'} else block.get('time', {}).get('value')
        if isinstance(event_time, dict) and kind in {'day', 'month'}:
            event_time = event_time.get('key') or event_time.get('date') or event_time.get('month')
        value['date_start'], value['date_end'] = _dates(event_time, self.config.repository.timezone)
        return value

    def memory_browse(self, identity, *, layers=None, query='', adapter='', profile='', source_id='',
                      start='', end='', cursor='', limit=30):
        self._owner(identity)
        layers = ['unit', 'source'] if layers is None else layers
        if (not isinstance(layers, list) or not layers or any(layer not in {'unit', 'source', 'summary', 'day', 'month', 'runtime'} for layer in layers)
                or type(limit) is not int or not 1 <= limit <= 100
                or any(not isinstance(v, str) for v in (query, adapter, profile, source_id, start, end, cursor))
                or len(query) > 1000 or len(cursor) > 2000):
            raise ValueError('Invalid browsing filters')
        for value in (start, end):
            if value and (len(value) != 10 or date.fromisoformat(value).isoformat() != value):
                raise ValueError('Invalid date')
        if start and end and start > end:
            raise ValueError('Reversed dates')
        kinds = set(layers) - {'summary'}
        if 'source' in layers:
            kinds.add('media')
        if 'summary' in layers:
            kinds.update(('day', 'month'))
        with self.repo.lock():
            catalog = (self.repo.browse_catalog() if hasattr(self.repo, 'browse_catalog')
                       else list(self.repo.catalog.values()))
            by_alias = {row['alias']: row for row in catalog}
            siblings = media_index(by_alias)
            pending_views = {job['ref']: job for job in self.repo.state['pending_views'].values()
                             if job['state'] != 'done'}
            signature = _digest(['visible-memory-v3', identity, sorted(kinds), query, adapter, profile, source_id, start, end,
                                 [(row['alias'], row.get('revision')) for row in catalog],
                                 list(self.repo.state.get('deleted_refs', [])),
                                 [(v.get('ref'), v.get('blocked')) for v in pending_views.values()]])
            offset = self._cursor_offset(cursor, signature)
            if offset < 0:
                raise ValueError('Invalid cursor')
            rows = [row for row in catalog if self._browse_visible(row)
                    and (row['metadata'].get('view') if row['kind'] == 'view' else row['kind']) in kinds]
            # File dates establish a stable newest-first order; refs break ties.
            rows.sort(key=lambda row: (row['path'], row['alias']), reverse=True)
            items, matched, cache = [], 0, {}
            formats = sorted({row['metadata'].get('memulife', {}).get('source_profile', '') for row in catalog
                              if not adapter or row['metadata'].get('memulife', {}).get('adapter_id') == adapter} - {''})
            deleted = set(self.repo.state['deleted_refs'])
            for row in rows:
                if row['kind'] in {'source', 'media'}:
                    origin = row['metadata'].get('memulife', {})
                    if ((adapter and origin.get('adapter_id') != adapter)
                            or (profile and origin.get('source_profile') != profile)
                            or (source_id and origin.get('source_instance_id') != source_id)):
                        continue
                for block in reversed(self.repo.iter_blocks(kinds, aliases=[row['alias']])):
                    pending = pending_views.get(block['ref'])
                    if block['ref'] in deleted:
                        continue
                    from .revision_storage import visible_record
                    block = visible_record(block, pending)
                    if block is None:
                        continue
                    item = self._browse_public(block, cache, pending=pending, catalog=by_alias)
                    if adapter or profile or source_id:
                        if not any((not adapter or origin['adapter_id'] == adapter)
                                   and (not profile or origin['source_profile'] == profile)
                                   and (not source_id or origin['source_instance_id'] == source_id) for origin in item['origins']):
                            continue
                    if (start or end) and (not item['date_start'] or (start and item['date_end'] < start) or (end and item['date_start'] > end)):
                        continue
                    # Literal and case-sensitive: no regex, tokenization, or embeddings.
                    if query and query not in (block['body'] or block.get('original_filename') or ''):
                        continue
                    matched += 1
                    if matched <= offset:
                        continue
                    if len(items) == limit:
                        return {'items': items, 'next_cursor': base64.urlsafe_b64encode(json.dumps(
                            {'offset': offset + len(items), 'signature': signature}).encode()).decode(),
                            'formats': formats, 'timezone': self.config.repository.timezone}
                    item['preview_truncated'] = len(item['body']) > 1500
                    item['body'] = item['body'][:1500]
                    item['media'] = browse_media(self, block, by_alias, siblings)
                    item['display'] = source_display(self.repo, block, limit=1500)
                    items.append(item)
            return {'items': items, 'next_cursor': None, 'formats': formats, 'timezone': self.config.repository.timezone}

    def memory_browse_detail(self, identity, *, ref, original=False, offset=0, revision=None):
        self._owner(identity)
        if type(original) is not bool or type(offset) is not int or offset < 0:
            raise ValueError('Invalid detail request')
        with self.repo.lock():
            parsed = Ref.parse(ref)
            pending = self._pending(ref)
            row = self.repo.catalog.get(parsed.alias)
            if (not row or ref in self.repo.state['deleted_refs']
                    or not self._browse_visible(row)):
                raise ValueError('Memory unavailable')
            block = self.repo.read(ref)
            from .revision_storage import visible_record
            block = visible_record(block, pending)
            if block is None:
                raise ValueError('Memory unavailable')
            item = self._browse_public(block, {}, pending=pending)
            item['media'] = browse_media(self, block)
            text, available = block['body'], False
            if original and block['kind'] == 'source':
                # Whole originals cannot re-expose partially erased source material.
                redacted = any(r['alias'] == parsed.alias for r in self.repo.state.get('blanked_ranges', []))
                if not redacted:
                    try:
                        audit = (self.repo.original_audit(row['metadata']) if hasattr(self.repo, 'original_audit')
                                 else row['metadata'].get('memulife', {}).get('transport_audit', {}))
                    except UnicodeDecodeError:
                        audit = {'format': 'binary', 'original': '[二进制原件，无法作为文字显示]'}
                    if audit:
                        text = self._browse_original_text(audit)
                        available = True
            version = _digest([block['revision'], original, text])
            if revision is not None and revision != version:
                raise ValueError('Memory changed; reopen detail')
            if offset > len(text):
                raise ValueError('Invalid detail offset')
            item.update(body=text[offset:offset + 20000], original_available=available, revision=version,
                        next_offset=offset + 20000 if offset + 20000 < len(text) else None)
            if not original:
                item['display'] = source_display(self.repo, block, offset=offset)
            return item

    def memory_browse_edit(self, identity, *, ref):
        """Bounded full-entry snapshots for the shared revision protocol."""
        self._owner(identity)
        from .revision_storage import EDITABLE_VIEWS, visible_record
        with self.repo.lock():
            record = visible_record(self.repo.read(ref), self._pending(ref))
            if record is None:
                raise ValueError('Memory is awaiting reconstruction')
            view = record.get('view')
            if record['kind'] != 'unit' and view not in EDITABLE_VIEWS:
                raise ValueError('This memory is not editable')
            entries = record['entries'] if view else [record]
            if not entries or sum(len(e['body']) for e in entries) > 1_000_000:
                raise ValueError('No editable content or edit payload too large')
            return {'ref': record['ref'], 'kind': view or 'unit', 'scope': record.get('scope'),
                'entries': [{key: entry.get(key) for key in ('entry_id', 'body', 'edit_revision', 'protected', 'invalid_refs')}
                            for entry in entries]}

    @staticmethod
    def _browse_original_text(audit):
        def decode(value):
            if isinstance(value, list):
                return [decode(v) for v in value]
            if not isinstance(value, dict):
                return value
            result = {k: decode(v) for k, v in value.items() if k not in {'raw_ref', 'raw', 'raw_utf8'}}
            raw = value.get('raw_utf8')
            if raw is None and isinstance(value.get('raw'), str):
                try:
                    raw = base64.b64decode(value['raw'], validate=True).decode('utf-8')
                except (ValueError, UnicodeDecodeError):
                    raw = '[二进制原件，无法作为文字显示]'
            if raw is not None:
                try:
                    raw = json.loads(raw)
                except ValueError:
                    pass
                result['original'] = raw
            return result
        value = decode(audit)
        if set(value) <= {'format', 'original'} and isinstance(value.get('original'), str):
            return value['original']
        return json.dumps(value, ensure_ascii=False, indent=2)
