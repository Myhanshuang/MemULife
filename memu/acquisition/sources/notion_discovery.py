"""Browse titles granted to a Notion connection before selecting any content."""
import hashlib
import json

from ..adapters.base import AdapterError
from ..adapters.notion import NotionAdapter, native_id
from .document_catalog import plain_title, restrict
from .notion_tree import candidate, cursors, _next


def discover(binding, cursor='', limit=100, query='', kind=''):
    if not 1 <= limit <= 200 or kind not in {'', 'page_ids', 'database_ids', 'data_source_ids'}:
        raise AdapterError('invalid_request')
    key = hashlib.sha256(json.dumps([binding, limit, query, kind], sort_keys=True).encode()).hexdigest()
    state = cursors.get(cursor, key) if cursor else {'upstream': None, 'seen': [], 'visited': []}
    reader = NotionAdapter(binding['config'])
    try:
        restrict(reader)
        reader.probe()
        body = {'page_size': min(limit, 100), 'sort': {'direction': 'descending', 'timestamp': 'last_edited_time'}}
        if query.strip():
            body['query'] = query.strip()
        if kind:
            body['filter'] = {'property': 'object', 'value': 'page' if kind == 'page_ids' else 'data_source'}
        if state['upstream']:
            body['start_cursor'] = state['upstream']
        value = reader.request('POST', '/search', body=body, params={'filter_properties': 'title'})
        more = _next(value, state['upstream'])
        if more in state['visited']:
            raise AdapterError('pagination_loop')
        if more:
            state['visited'].append(more)
        rows = []
        for item in value['results']:
            if item.get('archived') or item.get('in_trash'):
                continue
            if item.get('object') == 'page':
                field, identity = 'page_ids', native_id(item.get('id'))
                title = next((p.get('title', []) for p in item.get('properties', {}).values()
                              if isinstance(p, dict) and p.get('type') == 'title'), [])
            elif item.get('object') == 'data_source':
                field, identity, title = 'data_source_ids', native_id(item.get('id')), item.get('title', [])
                if kind != 'data_source_ids' and item.get('parent', {}).get('database_id'):
                    field, identity = 'database_ids', native_id(item['parent']['database_id'])
            else:
                continue
            identity = field + ':' + identity
            if identity not in state['seen']:
                state['seen'].append(identity)
                rows.append(candidate(identity, plain_title(title), item.get('last_edited_time')))
        state['upstream'] = more
        return {'items': rows, 'next_cursor': cursors.put(key, state) if more else None,
                'parent': '.', 'breadcrumbs': [{'id': '.', 'title': '可访问内容'}], 'content_persisted': False}
    finally:
        reader.close()
