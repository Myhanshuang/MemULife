"""Necessary connection and selection checks using synthetic upstream responses."""
import json
import os

import httpx
import pytest

from memu.acquisition.adapters.base import AdapterError
from memu.acquisition.collector import validate_binding
from memu.acquisition.connection_setup import complete_connection
from memu.acquisition.setup import SourceCredentials, setup_catalog
from memu.acquisition.sources import get_source
from memu.acquisition.sources.selection import Selection

HTTPClient = httpx.Client

def transport(monkeypatch, handler):
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: HTTPClient(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.parametrize('adapter,profile,field,config,identity', [
    ('qq.napcat', 'onebot11-napcat', 'token_env', {'base_url': 'http://localhost:3000'}, 'account_id'),
    ('notion.workspace', 'api-2026-03-11', 'token_ref', {}, 'bot_id'),
    ('feishu.documents', 'docx-v1', 'access_token_ref', {}, 'principal_id'),
])
def test_simple_connection_detects_identity_and_does_not_import(monkeypatch, tmp_path, adapter, profile, field, config, identity):
    calls = []
    def handle(request):
        calls.append(request.url.path)
        assert request.headers['Authorization'] == 'Bearer FIXTURE_ONLY'
        values = {'/get_login_info': {'status': 'ok', 'retcode': 0, 'data': {'user_id': 123}},
                  '/get_status': {'status': 'ok', 'retcode': 0, 'data': {'online': True}},
                  '/v1/users/me': {'object': 'user', 'id': '00000000-0000-4000-8000-000000000001'},
                  '/open-apis/authen/v1/user_info': {'code': 0, 'data': {'open_id': 'user-one'}}}
        assert request.url.path in values
        return httpx.Response(200, json=values[request.url.path])
    transport(monkeypatch, handle)
    store = SourceCredentials(tmp_path)
    binding, secrets = store.prepare('fixture', {'adapter_id': adapter, 'source_profile': profile,
        'source_namespace': 'test', 'config': config}, {field: 'FIXTURE_ONLY'})
    with store.temporary(secrets):
        connected = complete_connection(binding)
    validate_binding(connected)
    assert connected['config'][identity] and connected['config']['browse_accessible']
    assert all(name not in os.environ for name in secrets)
    assert not store.path.exists() and calls
    assert 'FIXTURE_ONLY' not in json.dumps(connected)
    schema = next(a for a in setup_catalog()['adapters'] if a['adapter_id'] == adapter)['profiles'][0]['schema']
    assert not set(schema['properties']) & {'account_id', 'workspace_id', 'principal_id', 'conversations',
                                           'page_ids', 'database_ids', 'document_ids', 'folder_tokens', 'wiki_spaces'}
    transport(monkeypatch, lambda _: httpx.Response(401))
    with pytest.raises(AdapterError), store.temporary(secrets):
        complete_connection(binding)
    assert all(name not in os.environ for name in secrets) and not store.path.exists()


def test_qq_catalog_discovers_peers_and_narrows_the_import(monkeypatch):
    monkeypatch.setenv('QQ_SELECT_FIXTURE', 'fixture-token')
    calls = []
    def handle(request):
        calls.append(request.url.path)
        values = {'/get_login_info': {'user_id': 123}, '/get_friend_list': [{'user_id': 456, 'remark': '好友'}],
                  '/get_group_list': [{'group_id': 789, 'group_name': '群聊'}]}
        assert request.url.path in values  # Browsing cannot read history.
        return httpx.Response(200, json={'status': 'ok', 'retcode': 0, 'data': values[request.url.path]})
    transport(monkeypatch, handle)
    source = get_source('qq.napcat')
    config = {'base_url': 'http://localhost:3000', 'token_env': 'QQ_SELECT_FIXTURE', 'account_id': '123',
              'browse_accessible': True, 'history_order': 'oldest_first'}
    binding = {'adapter_id': source.adapter_id, 'config': config}
    rows = source.candidates(binding, kind='group')['items']
    assert [(r['id'], r['name']) for r in rows] == [('group:789', '群聊')]
    selected = source.narrow_config(config, Selection(scopes=['private:456']))
    assert selected['conversations'] == [{'type': 'private', 'id': '456'}]
    assert not selected.get('browse_accessible')
    with pytest.raises(AdapterError, match='import_scope_denied'):
        source.narrow_config(config, Selection(scopes=['private:unknown']))


def test_napcat_webui_connects_and_reads_without_onebot_configuration(monkeypatch):
    import hashlib
    from memu.acquisition.adapters.qq import QQAdapter
    from memu.acquisition.adapters.napcat_webui import _sessions, reader_environment, _SESSION_ENV
    _sessions.clear()
    monkeypatch.setenv('NAPCAT_WEBUI_FIXTURE', 'webui-fixture-password')
    calls = []
    def handle(request):
        calls.append(request.url.path)
        body = json.loads(request.content)
        if request.url.path == '/api/auth/login':
            assert body == {'hash': hashlib.sha256(b'webui-fixture-password.napcat').hexdigest()}
            return httpx.Response(200, json={'code': 0, 'data': {'Credential': 'session-fixture-only'}})
        assert request.url.path == '/api/Debug/call'
        assert request.headers['Authorization'] == 'Bearer session-fixture-only'
        values = {'get_login_info': {'user_id': 123}, 'get_status': {'online': True},
            'get_friend_list': [{'user_id': 456, 'nickname': '好友'}], 'get_group_list': [{'group_id': 789, 'group_name': '群聊'}],
            'get_friend_msg_history': {'messages': []}}
        assert body['action'] in values
        return httpx.Response(200, json={'code': 0, 'data': {'status': 'ok', 'retcode': 0, 'data': values[body['action']]}})
    transport(monkeypatch, handle)
    binding = complete_connection({'adapter_id': 'qq.napcat', 'source_profile': 'napcat-webui', 'source_namespace': 'QQ',
        'config': {'base_url': 'http://localhost:16099/webui/', 'webui_token_ref': 'env:NAPCAT_WEBUI_FIXTURE'}})
    source = validate_binding(binding)
    assert binding['config']['base_url'] == 'http://localhost:16099'
    assert {r['id'] for r in source.candidates(binding)['items']} == {'private:456', 'group:789'}
    inherited = reader_environment(binding['config'])
    assert _SESSION_ENV not in os.environ
    _sessions.clear()  # Simulate an isolated reader inheriting the session.
    monkeypatch.setenv(_SESSION_ENV, inherited[_SESSION_ENV])
    reader = QQAdapter(source.narrow_config(binding['config'], Selection(scopes=['private:456'])))
    try:
        assert reader.read('backfill').records == []
        with pytest.raises(AdapterError, match='action_not_allowed'):
            reader.rpc('send_private_msg', {'user_id': 456, 'message': 'must not be sent'})
    finally:
        reader.close()
        _sessions.clear()
    assert calls.count('/api/auth/login') == 1
    assert set(calls) == {'/api/auth/login', '/api/Debug/call'}


def test_notion_discovery_and_selected_database_keep_content_boundaries(monkeypatch):
    from test_acquisition_notion_tree import Fixture, ROOT, DATABASE, DATA_SOURCE, ENTRY, SIBLING, NESTED, paged
    from memu.acquisition.sources.notion import source
    fixture = Fixture(monkeypatch, page_ids=[], browse_accessible=True)
    # The simplified form binds a bot, without asking for a workspace ID.
    fixture.config.pop('workspace_id')
    fixture.config['bot_id'] = ROOT
    original = fixture.dispatch
    def dispatch(request):
        if request.url.path == '/v1/users/me':
            return httpx.Response(200, json={'object': 'user', 'id': ROOT})
        if request.url.path == '/v1/search':
            fixture.calls.append((request.method, request.url.path, dict(request.url.params)))
            assert request.url.params['filter_properties'] == 'title'
            return httpx.Response(200, json=paged([fixture.page(ROOT), {'object': 'data_source', 'id': DATA_SOURCE,
                'parent': {'database_id': DATABASE}, 'title': [{'plain_text': '资料库'}]}]))
        return original(request)
    fixture.dispatch = dispatch
    # The module may already have been imported by another setup check.
    monkeypatch.setattr('memu.acquisition.sources.notion_discovery.NotionAdapter', fixture.reader)
    result = source.candidates(fixture.binding)
    assert {r['id'] for r in result['items']} == {'page_ids:' + ROOT, 'database_ids:' + DATABASE}
    assert not any('/blocks/' in call[1] for call in fixture.calls)
    assert 'UNSELECTED_PROPERTY' not in json.dumps(result)
    chosen = source.prepare_plan(fixture.binding, Selection(scopes=['database_ids:' + DATABASE]))
    reader = fixture.reader()
    try:
        source.prepare_reader(reader, chosen)
        cursor, records = None, []
        for _ in range(12):
            page = reader.read('backfill', cursor)
            records.extend(page.records)
            cursor = page.cursor
            if not page.has_more:
                break
        assert {r.object_id for r in records} == {ENTRY, SIBLING}
        assert cursor['bot_id'] == ROOT and 'workspace_id' not in cursor
        assert all(r.metadata['bot_id'] == ROOT for r in records)
        with pytest.raises(AdapterError, match='account_binding_mismatch'):
            reader.read('backfill', {**cursor, 'bot_id': NESTED})
        assert not any(f'/blocks/{NESTED}/children' in call[1] for call in fixture.calls)
    finally:
        reader.close()


def test_feishu_folder_and_wiki_catalog_only_returns_metadata_and_narrows_selection(monkeypatch):
    monkeypatch.setenv('FEISHU_SELECT_FIXTURE', 'fixture-token')
    calls = []
    def handle(request):
        path = request.url.path.removeprefix('/open-apis')
        calls.append(path)
        if path == '/authen/v1/user_info':
            data = {'open_id': 'user-one'}
        elif path == '/drive/v1/files':
            assert request.url.params.get('folder_token') in (None, 'folder-one')
            data = {'files': [{'type': 'folder', 'token': 'folder-one', 'name': '笔记'},
                              {'type': 'docx', 'token': 'doc-one', 'name': '文档'}], 'has_more': False}
        elif path == '/wiki/v2/spaces':
            data = {'items': [{'space_id': 'space-one', 'name': '知识库'}], 'has_more': False}
        elif path == '/wiki/v2/spaces/space-one/nodes':
            data = {'items': [{'obj_type': 'docx', 'obj_token': 'wiki-doc', 'title': '知识库文档'}], 'has_more': False}
        elif path == '/docx/v1/documents/doc-one':
            data = {'document': {'document_id': 'doc-one', 'title': '文档'}}
        elif path == '/docx/v1/documents/unknown':
            return httpx.Response(403)
        else:
            raise AssertionError(path)  # No document blocks or media are read.
        return httpx.Response(200, json={'code': 0, 'data': data})
    transport(monkeypatch, handle)
    source = get_source('feishu.documents')
    config = {'principal_id': 'user-one', 'access_token_ref': 'env:FEISHU_SELECT_FIXTURE', 'browse_accessible': True}
    binding = {'config': config}
    first = source.candidates(binding)
    assert {r['id'] for r in first['items']} == {'folder_tokens:folder-one', 'document_ids:doc-one'}
    second = source.candidates(binding, cursor=first['next_cursor'])
    assert [r['id'] for r in second['items']] == ['wiki_spaces:space-one'] and second['next_cursor'] is None
    source.candidates(binding, parent='folder_tokens:folder-one')
    expanded = source.candidates(binding, parent='wiki_spaces:space-one')
    assert [r['id'] for r in expanded['items']] == ['document_ids:wiki-doc']
    selection = Selection(scopes=['document_ids:doc-one'])
    source.validate(binding, selection)
    narrowed = source.narrow_config(config, selection)
    assert narrowed['document_ids'] == ['doc-one'] and not narrowed['folder_tokens'] and not narrowed['wiki_spaces']
    assert not narrowed.get('browse_accessible')
    with pytest.raises(AdapterError):
        source.validate(binding, Selection(scopes=['document_ids:unknown']))
