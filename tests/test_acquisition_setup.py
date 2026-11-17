import http.client
import json
import os
import threading

import pytest

from memu.acquisition.adapters.base import AdapterError
from memu.acquisition.adapters.http import environment_secret
from memu.acquisition.http_api import AcquisitionHTTPServer
from memu.acquisition.pairing import BrowserPairing
from memu.acquisition.service import AcquisitionService
from memu.acquisition.setup import setup_catalog
from test_acquisition_pipeline import acquisition, create


def test_web_credentials_private_persistent_and_invalid_config_has_no_side_effects(acquisition):
    service, core, _, client = acquisition
    binding = {'adapter_id': 'notion.workspace', 'source_profile': 'api-2026-03-11',
               'source_namespace': 'Synthetic Notion', 'config': {'workspace_id': 'workspace', 'page_ids': ['page']}}
    with pytest.raises(AdapterError, match='scope_denied'):
        service.register('reader', binding, credentials={'token_ref': 'FIXTURE_SECRET'})
    with pytest.raises(AdapterError, match='source_credentials_invalid'):
        service.register('owner', binding, credentials={'PATH': 'not-allowed'})
    with pytest.raises(AdapterError, match='config_invalid'):
        service.register('owner', {**binding, 'config': {'unknown': True}}, credentials={'token_ref': 'FIXTURE_SECRET'})
    assert not service.credentials.path.exists() and not service.state['sources']
    result = service.register('owner', binding, credentials={'token_ref': 'FIXTURE_SECRET'})
    reference = service.binding('owner', result['source_id'])['config']['token_ref']
    assert environment_secret(reference) == 'FIXTURE_SECRET'
    assert 'FIXTURE_SECRET' not in service.path.read_text()
    assert 'FIXTURE_SECRET' not in json.dumps(service.inventory('owner'))
    assert 'FIXTURE_SECRET' not in json.dumps(setup_catalog())
    assert service.credentials.path.stat().st_mode & 0o777 == 0o600
    assert not client.calls and not service.state['jobs']
    # Reload a separate service root: only referenced credentials are activated.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'control.json').write_bytes(service.path.read_bytes())
        (root / 'credentials.json').write_bytes(service.credentials.path.read_bytes())
        restored = AcquisitionService(root, client, core.config)
        try:
            assert environment_secret(reference) == 'FIXTURE_SECRET'
        finally:
            restored.close()


def test_pairing_is_explicit_origin_bound_expiring_and_only_delivers_one_source(acquisition):
    service, core, model, client = acquisition
    pairing = BrowserPairing(service)
    from memu.acquisition.exchange import BrowserExchange
    exchange = BrowserExchange(service)
    origin = 'chrome-extension://' + 'a' * 32
    account = {'provider': 'chatgpt', 'account_id': 'account', 'workspace_id': 'workspace'}
    with pytest.raises(AdapterError, match='scope_denied'):
        pairing.create('reader', 'chatgpt', 'Browser', 'a' * 32)
    pending = pairing.create('owner', 'chatgpt', 'Browser', 'a' * 32)
    assert pairing.status('owner', pending['code']) == {'state': 'waiting'}
    assert not service.state['sources'] and pairing.allowed_origin(origin)
    with pytest.raises(AdapterError, match='pairing_invalid'):
        pairing.pair('chrome-extension://' + 'b' * 32, pending['code'], account)
    result = pairing.pair(origin, pending['code'], account)
    assert pairing.status('owner', pending['code']) == {'state': 'paired', 'source_id': result['source_id']}
    assert result == pairing.pair(origin, pending['code'], account)
    assert len(service.state['sources']) == 1 and not client.calls
    assert result['token'] not in pairing.path.read_text()
    assert pairing.path.stat().st_mode & 0o777 == 0o600
    request = {'jsonrpc': '2.0', 'id': 'one', 'method': 'selection.get', 'params': account}
    reply = pairing.dispatch(exchange, origin, result['token'], request)
    assert reply['result']['persistence'] == 'denied'
    assert BrowserPairing(service).authenticate(origin, result['token'])['source_id'] == result['source_id']
    with pytest.raises(AdapterError, match='source_binding_mismatch'):
        pairing.dispatch(exchange, origin, result['token'], {**request, 'params': {**account, 'account_id': 'other'}})
    with pytest.raises(AdapterError, match='scope_denied'):
        pairing.dispatch(exchange, origin, result['token'], {**request, 'method': 'memory_write'})
    with pytest.raises(AdapterError, match='scope_denied'):
        pairing.authenticate('chrome-extension://' + 'b' * 32, result['token'])
    later = pairing.create('owner', 'chatgpt', 'Expired', 'a' * 32)
    pairing.pending[later['code']]['expires'] = 0
    with pytest.raises(AdapterError, match='pairing_invalid'):
        pairing.pair(origin, later['code'], account)
    job = create(service, result['source_id'], ['chosen'])
    from uuid import uuid4
    capture = {**request, 'method': 'capture', 'params': {**account, 'import_job_id': job['job_id'],
        'batch_id': str(uuid4()), 'cursor': {}, 'records': [{'object_id': 'chosen',
        'raw': {'text': 'PAIRED_BROWSER_RAW'}, 'text': 'NOT_A_PREBUILT_MEMORY', 'revision': 'one'}]}}
    receipt = pairing.dispatch(exchange, origin, result['token'], capture)
    assert receipt['result']['durable_level'] == 'edge_durable' and not client.calls
    assert service.run_once()
    assert service.state['jobs'][job['job_id']]['state'] == 'completed'
    assert model.calls['unit'] == model.calls['relate'] == 1
    assert 'PAIRED_BROWSER_RAW' in client.calls[0]['text']
    assert 'NOT_A_PREBUILT_MEMORY' not in client.calls[0]['text']
    core.refresh_due(force=True)
    assert model.calls['rollup'] > 0
    pairing.revoke('owner', result['source_id'])
    with pytest.raises(AdapterError, match='scope_denied'):
        pairing.authenticate(origin, result['token'])
    assert not BrowserPairing(service).allowed_origin(origin)
    again = pairing.create('owner', 'chatgpt', 'Reconnected', 'a' * 32)
    renewed = pairing.pair(origin, again['code'], account)
    assert renewed['source_id'] == result['source_id'] and renewed['token'] != result['token']
    assert len(service.state['sources']) == 1


def test_uploaded_immich_schema_is_private_and_has_correct_hash(acquisition):
    import hashlib
    from pathlib import Path
    service, _, _, client = acquisition
    binding = {'adapter_id': 'immich.assets', 'source_profile': 'installed-openapi',
               'source_namespace': 'Synthetic photos', 'config': {'base_url': 'https://photos.invalid',
               'deployment_id': 'test', 'owner_id': 'owner', 'album_ids': ['album'], 'server_version': '2.0.0', 'visibilities': ['timeline']}}
    with pytest.raises(AdapterError, match='source_schema_invalid'):
        service.register('owner', binding, schema_text='{"paths": []}')
    result = service.register('owner', binding, credentials={'api_key_ref': 'FIXTURE_ONLY'},
                              schema_text='{"openapi": "3.0.0", "paths": {}}')
    config = service.binding('owner', result['source_id'])['config']
    saved = Path(config['schema_path'])
    assert saved.stat().st_mode & 0o777 == 0o600
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == config['schema_sha256']
    assert json.loads(saved.read_bytes())['paths'] == {}
    assert not client.calls and not service.state['jobs']


def test_http_pairing_cors_and_management_permissions_remain_separate(acquisition):
    service, _, _, client = acquisition
    server = AcquisitionHTTPServer(service, service.config.core.model_copy(update={'port': 0}), client.tokens)
    thread = threading.Thread(target=server.run)
    thread.start()

    def request(path, method='GET', body=None, origin=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if origin: headers['Origin'] = origin
        if token: headers['Authorization'] = 'Bearer ' + token
        conn = http.client.HTTPConnection('localhost', server.server_port, timeout=5)
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        conn.close()
        return result

    try:
        assert request('/v1/manage/imports/setup')[0] == 401
        catalog = request('/v1/manage/imports/setup', token=client.tokens['owner'])
        assert len(json.loads(catalog[2])['adapters']) == 9
        assert request('/v1/manage/imports/setup', token=client.tokens['reader'])[0] == 403
        origin = 'chrome-extension://' + 'a' * 32
        assert request('/v1/browser/pair', 'OPTIONS', origin=origin)[0] == 403
        pending = json.loads(request('/v1/manage/imports/browser-pairings', 'POST',
            {'provider': 'chatgpt', 'name': 'Browser', 'extension_id': 'a' * 32}, token=client.tokens['owner'])[2])
        preflight = request('/v1/browser/pair', 'OPTIONS', origin=origin)
        assert preflight[0] == 204 and preflight[1]['Access-Control-Allow-Origin'] == origin
        assert request('/v1/browser/pair', 'OPTIONS', origin='https://evil.invalid')[0] == 403
        paired = request('/v1/browser/pair', 'POST', {'code': pending['code'], 'account': {
            'provider': 'chatgpt', 'account_id': 'account', 'workspace_id': 'workspace'}}, origin=origin)
        assert paired[0] == 200
        token = json.loads(paired[2])['token']
        # A browser delivery key never opens management, even without an Origin header.
        assert request('/v1/manage/imports/sources', token=token)[0] == 401
        assert request('/v1/manage/imports/sources', origin=origin, token=client.tokens['owner'])[0] == 403
        # The earlier Page Assist diagnosis is unchanged: no portless Origin bypass.
        assert request('/manage/assets/imports/index.js', origin='http://127.0.0.1')[0] == 403
    finally:
        server.request_stop(); thread.join(5); server.close()
