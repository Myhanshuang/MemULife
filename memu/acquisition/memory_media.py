"""Authenticated management previews. Immich originals never enter local storage."""
from urllib.parse import quote

from .adapters.base import AdapterError
from .adapters.http import identifier
from .sources import get_source
from .sources.immich_preview import thumbnail


def memory_media(service, identity, ref):
    service.owner(identity)
    # Resolve server-side provenance; never trust asset IDs, URLs or credentials from a client.
    detail = service.client.browse(identity, {'ref': ref}, detail=True)
    item = next((value for value in detail.get('media', []) if value['ref'] == ref), None)
    if item is None:
        raise AdapterError('media_preview_unavailable')
    if item['kind'] == 'native':
        if item.get('size_bytes', 0) > 16 * 1024 * 1024 and '@' not in ref:
            raise AdapterError('media_preview_too_large')
        resource = service.client.request('POST', '/v1/memory/resource', identity, {'uri': 'memu://media/' + ref})
        content = resource['contents'][0]
        return {'mime_type': content['mimeType'], 'data_base64': content['blob'], 'kind': 'native'}
    binding = service.binding(identity, item['source_id'])
    if binding['adapter_id'] != 'immich.assets' or binding.get('deleted_at'):
        raise AdapterError('import_binding_unavailable')
    reader = get_source('immich.assets').reader_class(binding['source_profile'])(binding['config'])
    try:
        if any(reader.config.get(key) != item.get(key) for key in ('owner_id', 'deployment_id')):
            raise AdapterError('source_binding_mismatch')
        reader._verify()
        asset_id = identifier(item['asset_id'])
        details, _ = reader._get('/assets/' + quote(asset_id, safe=''))
        if details.get('id') != asset_id or details.get('visibility') not in reader.config['visibilities']:
            raise AdapterError('asset_scope_mismatch')
        reader._check_scope(details, details['visibility'])
        preview = thumbnail(reader, {'id': 'asset_ids:' + asset_id})
        return {'kind': 'immich', 'thumbnail': preview.get('thumbnail'),
                'preview_error': preview.get('preview_error'),
                'open_url': reader.config['base_url'].rstrip('/').removesuffix('/api') + '/photos/' + quote(asset_id, safe=''),
                'content_persisted': False}
    finally:
        reader.close()
