"""Resolve displayable media from committed evidence, without fetching remote assets."""
from .refs import Ref
from .file_browsing import file_descriptors


def origin_key(row):
    origin = row.get('metadata', {}).get('memulife', {})
    values = tuple(origin.get(key) for key in ('source_instance_id', 'object_id', 'raw_sha256'))
    return values if all(values) else None


def media_index(catalog):
    result = {}
    for row in catalog.values():
        key = origin_key(row)
        if row['kind'] == 'media' and key:
            result.setdefault(key, []).append(row['alias'])
    return result


def browse_media(service, block, catalog=None, siblings=None):
    repo = service.repo
    if catalog is None:
        catalog = ({row['alias']: row for row in repo.browse_catalog()} if hasattr(repo, 'browse_catalog')
                   else repo.catalog)
    siblings = media_index(catalog) if siblings is None else siblings
    queue, seen, result = [block], set(), {}
    redacted = {r['alias'] for r in repo.state.get('blanked_ranges', [])}
    while queue:
        current = queue.pop()
        ref = current['ref']
        if ref in seen:
            continue
        seen.add(ref)
        parsed = Ref.parse(ref)
        row = catalog.get(parsed.alias)
        pending = service._pending(ref)
        if (not row or ref in repo.state['deleted_refs'] or (pending and pending.get('blocked'))
                or row['metadata'].get('active') is False):
            continue
        origin = row['metadata'].get('memulife', {})
        if current['kind'] == 'source' and origin.get('adapter_id') != 'ai.local-logs':
            for attachment in file_descriptors(repo, row, ref):
                result[('file', row['alias'], attachment['file_id'])] = attachment
        if origin.get('adapter_id') == 'immich.assets' and current.get('redacted_ranges'):
            continue  # A remote original cannot enforce locally erased time ranges.
        if parsed.alias not in redacted and origin.get('adapter_id') == 'immich.assets':
            meta = repo.full_metadata(row['metadata']) if hasattr(repo, 'full_metadata') else row['metadata']
            full_origin = meta.get('memulife', {})
            snapshot = full_origin.get('snapshot_metadata', {})
            if snapshot.get('asset_id'):
                attachment = next(iter(full_origin.get('attachments', [])), {})
                name = snapshot.get('file_name') or attachment.get('metadata', {}).get('file_name') or snapshot['asset_id']
                mime = current.get('mime_type') or attachment.get('media_type') or ('video/*' if snapshot.get('media_type') == 'VIDEO' else 'image/*')
                # Even legacy copied Immich files are viewed through their remote reference.
                result[('immich', origin['source_instance_id'], snapshot['asset_id'])] = {
                    'ref': ref, 'kind': 'immich', 'name': name, 'mime_type': mime,
                    'source_id': origin['source_instance_id'], 'asset_id': snapshot['asset_id'],
                    'owner_id': snapshot.get('owner_id'), 'deployment_id': snapshot.get('deployment_id'),
                }
                continue
        if current['kind'] == 'media':
            if current.get('redacted_ranges') and parsed.start_seconds is None:
                continue
            result[('native', ref)] = {'ref': ref, 'kind': 'native',
                'name': current.get('original_filename') or ref, 'mime_type': current['mime_type'],
                'size_bytes': current['size_bytes'], 'duration': current.get('duration')}
        refs = current.get('facts', []) if current['kind'] in {'unit', 'detail'} else []
        # Attachments belong to this exact imported revision, never to a newer object revision.
        if current['kind'] == 'source' and parsed.alias not in redacted:
            refs = siblings.get(origin_key(row), [])
        for target in refs:
            if target not in seen:
                try:
                    queue.append(repo.read(target))
                except ValueError:
                    pass
    return list(result.values())
