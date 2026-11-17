"""Metadata-only browsing of the bound user's cloud folders and Wiki spaces."""
from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import quote

from ..adapters.base import AdapterError
from ..adapters.feishu import FeishuAdapter
from ..adapters.http import identifier
from .document_catalog import restrict
from .notion_tree import CursorStore

cursors = CursorStore()
FIELDS = {'document_ids', 'folder_tokens', 'wiki_spaces'}


def scope(value):
    try:
        field, identity = value.split(':', 1)
        if field not in FIELDS:
            raise ValueError()
        return field, identifier(identity, 'import_scope_denied')
    except (AttributeError, ValueError):
        raise AdapterError('import_scope_denied') from None


def verify(config):
    reader = FeishuAdapter(config)
    try:
        restrict(reader)
        reader._verify_identity()
        for identity in config.get('document_ids', []):
            value, _ = reader._get('/docx/v1/documents/' + quote(identity, safe=''))
            if value.get('document', {}).get('document_id') != identity:
                raise AdapterError('source_binding_mismatch')
        for identity in config.get('folder_tokens', []):
            reader._get('/drive/v1/files', {'folder_token': identity, 'page_size': 1})
        for identity in config.get('wiki_spaces', []):
            reader._get('/wiki/v2/spaces/' + quote(identity, safe=''))
    finally:
        reader.close()


def _row(field, identity, name, stamp=None):
    updated = None
    if isinstance(stamp, (int, str)):
        try:
            updated = datetime.fromtimestamp(int(stamp), timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            pass
    identity = identifier(identity)
    return {'id': field + ':' + identity, 'name': (name if isinstance(name, str) and name else identity)[:300],
            'kind': field, 'updated_at': updated, 'can_expand': field != 'document_ids'}


def _next(value, previous):
    if not value.get('has_more'):
        return None
    token = value.get('next_page_token') or value.get('page_token')
    if not isinstance(token, str) or not token or token == previous or len(token) > 2048:
        raise AdapterError('pagination_loop')
    return token


def discover(binding, parent='.', cursor='', limit=100, query='', kind=''):
    if not 1 <= limit <= 200 or kind not in FIELDS | {''}:
        raise AdapterError('invalid_request')
    field, identity = scope(parent) if parent != '.' else ('', '')
    if field == 'document_ids':
        raise AdapterError('import_scope_denied')
    key = hashlib.sha256(json.dumps([binding, parent, limit, query, kind], sort_keys=True).encode()).hexdigest()
    state = cursors.get(cursor, key) if cursor else {
        'stage': 'wiki' if kind == 'wiki_spaces' and not field else 'nodes' if field == 'wiki_spaces' else 'drive',
        'upstream': None, 'visited': [], 'nodes': [], 'node': None, 'folder': identity if field == 'folder_tokens' else None}
    reader = FeishuAdapter(binding['config'])
    try:
        restrict(reader)
        reader._verify_identity()
        params = {'page_size': min(limit, 50)}
        if state['upstream']:
            params['page_token'] = state['upstream']
        rows, warnings = [], []
        previous = state['upstream']
        marker = [state['stage'], state.get('node'), previous]
        if marker in state['visited']:
            raise AdapterError('pagination_loop')
        state['visited'].append(marker)
        try:
            if state['stage'] == 'drive':
                # The list endpoint accepts an omitted folder for the user's
                # root. The legacy root metadata endpoint requires additional
                # permissions even when listing files is already authorized.
                if state['folder']:
                    params['folder_token'] = state['folder']
                value, _ = reader._get('/drive/v1/files', params)
                if not isinstance(value.get('files'), list):
                    raise AdapterError('incompatible_schema')
                for item in value['files']:
                    selected_field = {'docx': 'document_ids', 'folder': 'folder_tokens'}.get(item.get('type'))
                    if selected_field:
                        rows.append(_row(selected_field, item.get('token'), item.get('name'), item.get('modified_time')))
            elif state['stage'] == 'wiki':
                value, _ = reader._get('/wiki/v2/spaces', params)
                if not isinstance(value.get('items'), list):
                    raise AdapterError('incompatible_schema')
                rows = [_row('wiki_spaces', item.get('space_id'), item.get('name')) for item in value['items']]
            else:
                if state['node']:
                    params['parent_node_token'] = state['node']
                value, _ = reader._get('/wiki/v2/spaces/' + quote(identity, safe='') + '/nodes', params)
                if not isinstance(value.get('items'), list):
                    raise AdapterError('incompatible_schema')
                for item in value['items']:
                    if item.get('obj_type') == 'docx':
                        rows.append(_row('document_ids', item.get('obj_token'), item.get('title')))
                    if item.get('has_child'):
                        node = identifier(item.get('node_token'))
                        if node not in state['nodes']:
                            state['nodes'].append(node)
            state['upstream'] = _next(value, previous)
        except AdapterError as error:
            if parent != '.' or kind or error.code != 'source_access_denied':
                raise
            warnings.append(error.code)
            state['upstream'] = None
        more = bool(state['upstream'])
        if not more and state['stage'] == 'drive' and parent == '.' and not kind:
            state.update(stage='wiki', upstream=None)
            more = True
        elif not more and state['stage'] == 'nodes' and state['nodes']:
            state.update(node=state['nodes'].pop(0), upstream=None)
            more = True
        needle = query.strip().casefold()
        return {'items': [row for row in rows if (not kind or row['kind'] == kind) and
                         (not needle or needle in (row['name'] + ' ' + row['id']).casefold())],
                'next_cursor': cursors.put(key, state) if more else None, 'warnings': warnings,
                'parent': parent, 'breadcrumbs': [{'id': '.', 'title': '云文档'}] +
                ([{'id': parent, 'title': '文件夹' if field == 'folder_tokens' else '知识库'}] if field else []),
                'content_persisted': False}
    finally:
        reader.close()
