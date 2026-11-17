from copy import deepcopy
import http.client
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from memu.acquisition.adapters.base import AdapterError
from memu.acquisition.collector import SourceReader
from memu.acquisition.exchange import BrowserExchange
from memu.acquisition.http_api import AcquisitionHTTPServer
from memu.acquisition.service import AcquisitionService
from memu.operations import CoreApplication
from test_service import service_fixture


class CoreClient:
    url = urlsplit('http://synthetic-core.invalid')
    tokens = {'owner': 'fixture-owner-token-' + 'x' * 24, 'reader': 'fixture-reader-token-' + 'y' * 24}

    def __init__(self, service):
        self.application, self.calls = CoreApplication(service), []

    def call(self, identity, name, arguments):
        return self.application.call(identity, name, arguments)

    def write(self, identity, material):
        self.calls.append(deepcopy(material))
        return self.application.write(identity, material)


@pytest.fixture
def acquisition(tmp_path):
    core, model, embedding, index = service_fixture(tmp_path)
    client = CoreClient(core)
    service = AcquisitionService(tmp_path / 'acquisition', client, core.config)
    yield service, core, model, client
    service.close()


def files_source(service, path):
    return service.register('owner', {'adapter_id': 'filesystem.typora', 'source_profile': 'linux-markdown-v1',
                                      'source_namespace': 'synthetic:notes', 'config': {'root': str(path)}})['source_id']


def create(service, source, scopes):
    plan = service.plan('owner', source, {'scopes': scopes})
    return service.create_batch('owner', source, [plan])['jobs'][0]


def test_files_selection_to_full_pipeline_and_durable_job(acquisition, tmp_path, monkeypatch):
    service, core, model, client = acquisition
    notes = tmp_path / 'notes'
    notes.mkdir()
    chosen = notes / 'chosen.md'
    chosen.write_text('选中的原始笔记')
    (notes / 'unselected.md').write_text('UNSELECTED_NEVER_PERSIST')
    source = files_source(service, notes)
    from memu.acquisition.adapters.local.common import LocalAccess
    original = LocalAccess.read_file
    with monkeypatch.context() as patch:
        patch.setattr(LocalAccess, 'read_file', lambda *_: pytest.fail('Discovery/plan read original content'))
        assert len(service.candidates('owner', source)['items']) == 2
        job = create(service, source, ['chosen.md'])
    assert not client.calls and not list((service.root / 'outbox').glob('*'))
    assert '选中的原始笔记' not in service.path.read_text()
    assert service.run_once()
    final = service.jobs('owner')['jobs'][0]
    assert final['job_id'] == job['job_id'] and final['state'] == 'completed'
    assert client.calls[0]['text'] == '选中的原始笔记'
    assert model.calls['unit'] == model.calls['relate'] == 1
    core.refresh_due(force=True)
    assert model.calls['rollup'] > 0
    assert all('UNSELECTED_NEVER_PERSIST' not in p.read_text() for p in core.repo.root.rglob('*.md'))


def test_frozen_file_revision_change_and_late_cancel_discard_before_persistence(acquisition, tmp_path, monkeypatch):
    service, core, _, client = acquisition
    notes = tmp_path / 'notes'
    notes.mkdir()
    path = notes / 'chosen.md'
    path.write_text('first')
    source = files_source(service, notes)
    job = create(service, source, ['chosen.md'])
    path.write_text('changed after selection')
    service.run_once()
    assert service.state['jobs'][job['job_id']]['error_code'] == 'import_candidate_changed'
    assert not client.calls and not list((service.root / 'outbox').glob('*.json'))
    service.control('owner', job['job_id'], 'cancel')
    second = create(service, source, ['chosen.md'])
    original = SourceReader.read

    def cancel_after_read(reader, *args):
        page = original(reader, *args)
        service.control('owner', second['job_id'], 'cancel')
        return page

    monkeypatch.setattr(SourceReader, 'read', cancel_after_read)
    service.run_once()
    assert service.state['jobs'][second['job_id']]['state'] == 'cancelled'
    assert not client.calls and not list((service.root / 'outbox').glob('*.json'))


def test_browser_selected_push_durable_ack_then_pipeline_and_exact_replay(acquisition):
    service, core, model, client = acquisition
    source = service.register('owner', {'adapter_id': 'browser.ai-history', 'source_profile': 'chatgpt',
        'source_namespace': 'synthetic:browser',
        'config': {'provider': 'chatgpt', 'account_id': 'account', 'workspace_id': 'workspace'}})['source_id']
    exchange = BrowserExchange(service)
    assert exchange.selection('owner', source)['persistence'] == 'denied'
    job = create(service, source, ['conversation-1'])
    selected = exchange.selection('owner', source)
    assert selected['job_id'] == job['job_id'] and selected['scopes'] == ['conversation-1']
    params = {'provider': 'chatgpt', 'account_id': 'account', 'workspace_id': 'workspace',
              'import_job_id': job['job_id'], 'batch_id': str(uuid4()), 'cursor': {},
              'records': [{'object_id': 'conversation-1', 'raw': {'role': 'user', 'text': '原始网页会话'},
                           'text': 'not prebuilt memory', 'revision': 'native-r1'}]}
    outside = deepcopy(params)
    outside['records'][0]['object_id'] = 'conversation-outside'
    with pytest.raises(AdapterError, match='scope_denied'):
        exchange.capture('owner', source, outside)
    assert not list((service.root / 'outbox').glob('*.json'))
    receipt = exchange.capture('owner', source, params)
    assert receipt['durable_level'] == 'edge_durable' and not client.calls
    assert receipt == exchange.capture('owner', source, params)
    assert service.run_once()
    assert service.state['jobs'][job['job_id']]['state'] == 'completed'
    assert model.calls['unit'] == model.calls['relate'] == 1
    assert '原始网页会话' in client.calls[0]['text'] and 'not prebuilt memory' not in client.calls[0]['text']
    assert exchange.capture('owner', source, params) == receipt
    changed = deepcopy(params)
    changed['records'][0]['raw']['text'] = 'changed'
    with pytest.raises(AdapterError, match='conflict'):
        exchange.capture('owner', source, changed)
    core.refresh_due(force=True)
    assert model.calls['rollup'] > 0


def test_batch_is_atomic_and_identity_permissions_hold(acquisition, tmp_path):
    service, _, _, client = acquisition
    notes = tmp_path / 'notes'
    notes.mkdir()
    (notes / 'a.md').write_text('a')
    (notes / 'b.md').write_text('b')
    source = files_source(service, notes)
    with pytest.raises(AdapterError, match='scope_denied'):
        service.inventory('reader')
    plans = service.plan_batch('owner', source, [{'scopes': ['a.md']}, {'scopes': ['b.md']}])['plans']
    plans[1]['config_hash'] = 'invalid'
    with pytest.raises(AdapterError, match='stale'):
        service.create_batch('owner', source, plans)
    assert not service.state['jobs']


def test_subscription_preserves_frozen_scopes_and_requires_explicit_opt_in(acquisition, tmp_path):
    service, _, _, client = acquisition
    notes = tmp_path / 'notes'
    notes.mkdir()
    (notes / 'a.md').write_text('first')
    source = files_source(service, notes)
    job = create(service, source, ['.'])
    service.run_once()
    assert not service.state['subscriptions']
    with pytest.raises(AdapterError, match='interval'):
        service.subscribe('owner', job['job_id'], 149)
    sid = service.subscribe('owner', job['job_id'], 150)['subscription_id']
    (notes / 'a.md').write_text('updated')
    (notes / 'future.md').write_text('NEW_UNSELECTED')
    service.state['subscriptions'][sid]['next_run_at'] = 0
    service.schedule()
    service.run_once()
    service.schedule()
    assert len(client.calls) == 2 and client.calls[-1]['text'] == 'updated'
    assert service.state['subscriptions'][sid]['cycles'] == 1
    assert service.state['subscriptions'][sid]['selection']['scopes'] == ['a.md']


def test_http_ui_and_source_registration_to_pipeline(acquisition, tmp_path):
    service, _, model, client = acquisition
    server = AcquisitionHTTPServer(service, service.config.core.model_copy(update={'port': 0}), client.tokens)
    thread = threading.Thread(target=server.run)
    thread.start()

    def request(path, method='GET', data=None, identity='owner'):
        conn = http.client.HTTPConnection('localhost', server.server_port, timeout=5)
        headers = {'Content-Type': 'application/json'}
        if identity:
            headers['Authorization'] = 'Bearer ' + client.tokens[identity]
        conn.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers)
        response = conn.getresponse()
        status, value = response.status, response.read()
        conn.close()
        return status, value

    try:
        assert request('/manage/imports', identity=None)[0] == 200
        assert b'MemULife' in request('/manage/imports/files')[1]
        assert request('/manage/assets/imports/page.js', identity=None)[0] == 200
        assert request('/manage/assets/imports/../control.json')[0] == 404
        assert request('/v1/manage/imports/sources', identity=None)[0] == 401
        assert request('/v1/manage/imports/sources', identity='reader')[0] == 403
        assert request('/manage/imports/topics', identity=None)[0] == 200
        assert request('/v1/manage/topics', identity=None)[0] == 401
        assert request('/v1/manage/topics', identity='reader')[0] == 403
        assert request('/v1/manage/topics/retry', 'POST', {}, identity='reader')[0] == 403
        assert request('/v1/manage/topics', 'POST', {'key': '本地部署', 'description': '部署进展与性能测试'})[0] == 200
        topics = json.loads(request('/v1/manage/topics')[1])['topics']
        assert any(row['key'] == '本地部署' and row['origin'] == 'user' for row in topics)
        assert request('/v1/manage/topics', 'POST', {'key': '无效\n主题'})[0] == 422
        notes = tmp_path / 'notes'
        notes.mkdir()
        (notes / 'selected.md').write_text('HTTP selected original')
        source = files_source(service, notes)
        plan = json.loads(request('/v1/manage/imports/plan', 'POST', {'source_id': source, 'selection': {'scopes': ['selected.md']}})[1])
        submitted = json.loads(request('/v1/manage/imports/jobs', 'POST', plan)[1])
        service.run_once()
        jobs = json.loads(request('/v1/manage/imports/jobs')[1])['jobs']
        assert jobs[0]['job_id'] == submitted['job_id'] and jobs[0]['state'] == 'completed'
        assert model.calls['unit'] == 1
    finally:
        server.request_stop()
        thread.join(5)
        server.close()


def test_extension_native_transport_reaches_selected_outbox_without_old_checkout(acquisition, tmp_path):
    import os
    import subprocess
    import sys
    service, _, _, client = acquisition
    source = service.register('owner', {'adapter_id': 'browser.ai-history', 'source_profile': 'chatgpt',
        'source_namespace': 'synthetic:native',
        'config': {'provider': 'chatgpt', 'account_id': 'account', 'workspace_id': 'workspace'}})['source_id']
    job = create(service, source, ['conversation'])
    server = AcquisitionHTTPServer(service, service.config.core.model_copy(update={'port': 0}), client.tokens)
    thread = threading.Thread(target=server.run)
    thread.start()
    origin = 'chrome-extension://' + 'a' * 32 + '/'
    source_config = tmp_path / 'native-source.json'
    source_config.write_text(json.dumps({**service.binding('owner', source), 'extension_origin': origin,
                                        'center_url': f'http://localhost:{server.server_port}'}))
    env_file = tmp_path / '.env'
    env_file.write_text('')
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('MEMU_', 'MEMORY_'))}
    environment.update(MEMU_ENV_FILE=str(env_file), MEMU_MCP_TOKENS=json.dumps(client.tokens))
    try:
        # Native harness imports the extension's generated transport; build is local and dependency-free.
        root = Path(__file__).resolve().parents[1]
        subprocess.run(['node', 'browser-extension/tools/build.mjs'], cwd=root, check=True,
                       capture_output=True, timeout=15)
        result = subprocess.run(['node', 'browser-extension/tools/native-harness.mjs', sys.executable,
                                 str(source_config), origin, job['job_id']], cwd=root, env=environment,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['status'] == 'passed'
        assert len(service.state['jobs'][job['job_id']]['receipts']) == 1
        assert not client.calls  # Durable capture ACK precedes model processing.
        assert list((service.root / 'outbox').glob('*.json'))
    finally:
        server.request_stop()
        thread.join(5)
        server.close()
