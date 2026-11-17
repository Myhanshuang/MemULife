"""Original imported attachments, addressed by evidence rather than filesystem paths."""
import base64
from pathlib import PurePosixPath
import re


def original_files(metadata):
    origin = metadata.get('memulife', {})
    snapshot = origin.get('snapshot_metadata', {})
    audit = origin.get('transport_audit', {})
    result = []
    if (metadata.get('content', {}).get('material_kind') == 'local_file'
            and audit.get('format') == 'file-original-v1'
            and any(key in audit for key in ('raw_ref', 'raw', 'raw_utf8'))):
        result.append({'file_id': 'original', 'name': snapshot.get('path') or origin.get('object_id') or 'attachment',
                       'mime_type': origin.get('raw_media_type') or 'application/octet-stream',
                       'size_bytes': origin.get('raw_size_bytes'), '_storage': audit})
    descriptors = {item['attachment_id']: item for item in origin.get('attachments', [])}
    for item in origin.get('attachment_originals', []):
        descriptor = descriptors.get(item['attachment_id'], {})
        info = descriptor.get('metadata', {})
        encoded = item.get('data_base64', '')
        result.append({'file_id': 'attachment:' + item['attachment_id'],
                       'name': info.get('name') or info.get('file_name') or item['attachment_id'],
                       'mime_type': item.get('media_type') or 'application/octet-stream',
                       'size_bytes': len(encoded) // 4 * 3 - (len(encoded) - len(encoded.rstrip('='))),
                       '_storage': {'raw': encoded}})
    for item in result:
        item['name'] = PurePosixPath(str(item['name']).replace('\\', '/')).name or 'attachment'
    return result


def file_descriptors(repo, row, ref, metadata=None):
    if row['kind'] != 'source' or any(r['alias'] == row['alias'] for r in repo.state.get('blanked_ranges', [])):
        return []
    metadata = metadata if metadata is not None else (repo.full_metadata(row['metadata']) if hasattr(repo, 'full_metadata') else row['metadata'])
    return [{**{k: v for k, v in item.items() if not k.startswith('_')}, 'kind': 'file', 'ref': ref}
            for item in original_files(metadata)]


def read_original_file(repo, row, ref, file_id):
    metadata = repo.full_metadata(row['metadata']) if hasattr(repo, 'full_metadata') else row['metadata']
    descriptor = next((item for item in file_descriptors(repo, row, ref, metadata) if item['file_id'] == file_id), None)
    if descriptor is None:
        raise ValueError('Original attachment unavailable')
    if (descriptor.get('size_bytes') or 0) > 32 * 1024 * 1024:
        raise ValueError('Original attachment exceeds 32 MiB')
    item = next(item for item in original_files(metadata) if item['file_id'] == file_id)
    storage = item['_storage']
    if storage.get('raw_ref'):
        path = storage['raw_ref']
        if not re.fullmatch(r'\.meta/source-originals/[a-f0-9]{64}\.bin', path):
            raise ValueError('Invalid original attachment')
        data = repo._bytes(path)
    elif isinstance(storage.get('raw_utf8'), str):
        data = storage['raw_utf8'].encode('utf-8')
    else:
        data = base64.b64decode(storage['raw'], validate=True)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError('Original attachment exceeds 32 MiB')
    return {**descriptor, 'data_base64': base64.b64encode(data).decode('ascii')}
