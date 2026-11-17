import base64
from copy import deepcopy
import json
import threading
from urllib.parse import urlsplit

import pytest

from memu.core_api import CoreHTTPServer
from memu.core_client import CoreUnavailable, RemoteCore
from memu.memulife import BridgeError, MemULifeBridge, page_materials, snapshot_material
from test_service import service_fixture
from memu.acquisition.collector import SourceReader
from memu.acquisition.adapters.reader import encode_page
from memu.acquisition.adapters.base import AdapterError


def source(config=None):
    return {'adapter_id': 'ai.local-logs', 'source_profile': 'codex',
            'source_instance_id': 'fixture-source', 'source_namespace': 'fixture:account', 'config': config or {}}


def snapshot(raw_text='原始测试材料', **changes):
    return {'object_id': 'conversation/message-1', 'raw': raw_text.encode(), 'text': 'DO NOT USE A SUMMARY',
            'media_type': 'text/plain', 'revision': 'r1', 'deleted': False,
            'occurred_at': '2026-11-14T03:00:00Z', 'metadata': {'actor': {'role': 'user'}},
            'attachments': [], **changes}


def page(records=None, **changes):
    return {'records': records if records is not None else [snapshot()], 'cursor': {'offset': 1},
            'has_more': False, 'warnings': [], **changes}


class Reader:
    def __init__(self, value):
        self.value, self.calls = value, 0

    def read(self, mode, cursor):
        self.calls += 1
        return deepcopy(self.value)


class Client:
    url = urlsplit('http://fixture.test')

    def __init__(self):
        self.writes, self.fail_at, self.failed = [], 1, False

    def write(self, identity, material):
        self.writes.append(deepcopy(material))
        if len(self.writes) == self.fail_at and not self.failed:
            self.failed = True
            raise CoreUnavailable()
        return {'is_error': False, 'result': {'status': 'done', 'index_ready': False}}


def test_raw_material_time_provenance_media_and_stable_keys():
    raw = b'{"role":"user","text":"original"}'
    item = snapshot(raw=raw, media_type='application/json', attachments=[{
        'attachment_id': 'photo.png', 'data': b'original-image', 'media_type': 'image/png',
        'status': 'available', 'source_uri': None}])
    material = snapshot_material(source(), item)
    assert material['text'] == raw.decode()
    assert base64.b64decode(material['media'][0]['data_base64']) == b'original-image'
    assert material['metadata']['memulife']['object_id'] == item['object_id']
    assert material['time'] == {'precision': 'instant', 'value': item['occurred_at']}
    wire = deepcopy(item)
    wire['raw'] = base64.b64encode(raw).decode()
    wire['attachments'][0]['data'] = base64.b64encode(b'original-image').decode()
    assert snapshot_material(source(), wire) == material
    assert snapshot_material(source(), {**item, 'revision': 'r2'})['idempotency_key'] != material['idempotency_key']
    assert snapshot_material({**source(), 'source_instance_id': 'other'}, item)['idempotency_key'] != material['idempotency_key']
    assert snapshot_material(source(), snapshot(occurred_at=None))['time']['precision'] == 'unknown'


@pytest.mark.parametrize('item,code', [
    (snapshot(deleted=True), 'tombstone'),
    (snapshot(raw=b'\xff'), 'utf8'),
    (snapshot(raw=b'', text=None), 'no_supported_material'),
])
def test_reject_unsupported_material_before_staging(tmp_path, item, code):
    bridge = MemULifeBridge(Client(), 'owner', source(), {'scopes': ['selected']}, tmp_path / 'state.json')
    with pytest.raises(BridgeError, match=code):
        bridge.collect(Reader(page([snapshot(), item])))
    assert not bridge.path.exists() and not bridge.client.writes


def test_lost_response_resume_uses_frozen_material_and_commits_cursor(tmp_path):
    client = Client()
    bridge = MemULifeBridge(client, 'owner', source(), {'scopes': ['selected']}, tmp_path / 'state.json')
    reader = Reader(page())
    result = bridge.collect(reader)
    assert result['status'] == 'pending' and result['resume_required']
    saved = json.loads(bridge.path.read_text())
    assert saved['cursor'] is None and saved['pending']['next'] == 0
    assert bridge.path.stat().st_mode & 0o777 == 0o600
    reader.value = page([snapshot('changed upstream while offline')])
    with pytest.raises(BridgeError, match='requires_resume'):
        bridge.collect(reader)
    result = bridge.collect(reader, resume=True)
    assert result['status'] == 'done' and result['committed'] == 1
    assert reader.calls == 1 and client.writes[0] == client.writes[1]
    saved = json.loads(bridge.path.read_text())
    assert saved['cursor'] == {'offset': 1} and saved['pending'] is None


def test_partial_page_keeps_last_ack_and_binding_prevents_reuse(tmp_path):
    client = Client()
    client.fail_at = 2
    bridge = MemULifeBridge(client, 'owner', source(), {'scopes': ['selected']}, tmp_path / 'state.json')
    reader = Reader(page([snapshot(), snapshot('second', object_id='second')]))
    assert bridge.collect(reader)['committed'] == 1
    state = json.loads(bridge.path.read_text())
    assert state['cursor'] is None and state['pending']['next'] == 1
    assert bridge.collect(reader, resume=True)['status'] == 'done'
    assert len(client.writes) == 3 and client.writes[1] == client.writes[2]
    other = MemULifeBridge(client, 'owner', source(), {'scopes': ['other']}, bridge.path)
    with pytest.raises(BridgeError, match='binding_mismatch'):
        other.collect(reader)


def test_write_failure_never_advances_and_no_automatic_retry(tmp_path):
    client = Client()
    client.write = lambda *_: {'is_error': True, 'result': {
        'status': 'failed', 'request_id': 'task-1', 'error': 'synthetic provider failure'}}
    bridge = MemULifeBridge(client, 'owner', source(), {'scopes': ['selected']}, tmp_path / 'state.json')
    assert bridge.collect(Reader(page()))['request_id'] == 'task-1'
    assert json.loads(bridge.path.read_text())['cursor'] is None


def test_bridge_full_pipeline_and_replay_after_actual_commit(tmp_path):
    service, model, embedding, index = service_fixture(tmp_path)
    server = CoreHTTPServer(service, service.config.core.model_copy(update={'port': 0}), {'owner': 'x' * 32})
    thread = threading.Thread(target=server.run)
    thread.start()
    remote = RemoteCore(f'http://localhost:{server.server_port}', {'owner': 'x' * 32})

    class LostAcknowledgement:
        url = remote.url

        def write(self, identity, material):
            result = remote.write(identity, material)
            if not getattr(self, 'lost', False):
                self.lost = True
                raise CoreUnavailable()
            return result

    try:
        bridge = MemULifeBridge(LostAcknowledgement(), 'owner', source(), {'scopes': ['selected']},
                               tmp_path / 'state.json')
        reader = Reader(page())
        assert bridge.collect(reader)['status'] == 'pending'
        assert bridge.collect(reader, resume=True)['status'] == 'done'
        assert model.calls['unit'] == model.calls['relate'] == index.search_calls == 1
        assert embedding.calls
        stored = next(record for record in service.repo.catalog.values() if record['kind'] == 'source')
        assert stored['metadata']['memulife']['object_id'] == 'conversation/message-1'
        assert '原始测试材料' in service.repo.read(stored['alias'] + ':1')['body']
        assert 'DO NOT USE A SUMMARY' not in service.repo.read(stored['alias'] + ':1')['body']
        service.refresh_due(force=True)
        assert model.calls['rollup'] > 0
        overview = service.memory_read(from_='owner', refs=['MEMORY'])['results'][0]
        assert overview['body'] and overview['pending_update'] is False
    finally:
        server.request_stop()
        thread.join(10)
        server.close()


def test_adapter_media_uses_digest_units_embeddings_relations_and_views(tmp_path):
    from test_service import FixtureModel, SyntheticEmbedding, compact_fixture_output
    from memu.operations import CoreApplication
    from memu.tasks import TaskRunner

    class MediaModel(FixtureModel):
        def complete(self, request):
            task, material = request['task'], request['material']
            if task in {'digest', 'unit'}:
                self.calls[task] += 1
                entry = {'time': material['time'], 'body': '合成图片测试材料',
                         'facts': [item['ref'] for item in material['items']]}
                return compact_fixture_output(request, {'entries': [entry]} if task == 'digest' else {'units': [{**entry, 'topics': ['memory']}]})
            return super().complete(request)

    class MediaEmbedding(SyntheticEmbedding):
        capabilities = {**SyntheticEmbedding.capabilities, 'modalities': ['text', 'image']}

        def embed(self, items):
            self.calls.append(deepcopy(items))
            return [{'vector': [1.0, 0.0], 'space_id': self.capabilities['space_id'],
                     'model_version': 'fixture-v1', 'preprocess_version': 'fixture-v1'} for _ in items]

    service, _, _, index = service_fixture(tmp_path)
    model = MediaModel()
    service.runner, service.embedder = TaskRunner(model, service.config), MediaEmbedding()
    # Tiny image fixture; model and embedding behavior remain explicitly synthetic.
    original = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII=')
    material = snapshot_material(source(), snapshot(raw=original, text=None, media_type='image/png'))
    response = CoreApplication(service).write('owner', material)
    assert response['is_error'] is False, response
    assert response['result']['status'] == 'done'
    assert model.calls['digest'] == model.calls['unit'] == model.calls['relate'] == 1
    assert index.search_calls == 1
    assert any(item['kind'] == 'image' and base64.b64decode(item['data_base64']) == original
               for call in service.embedder.calls for item in call)
    stored = next(record for record in service.repo.catalog.values() if record['kind'] == 'media')
    assert stored['metadata']['memulife']['object_id'] == 'conversation/message-1'
    assert (service.repo.root / stored['path']).read_bytes() == original
    service.refresh_due(force=True)
    assert model.calls['rollup'] > 0


def test_migrated_selected_adapter_with_synthetic_logs(tmp_path, monkeypatch):
    logs = tmp_path / 'logs' / 'sessions'
    logs.mkdir(parents=True)
    records = [
        {'type': 'session_meta', 'payload': {'id': 'synthetic-session'}, 'timestamp': '2026-11-14T01:00:00Z'},
        {'type': 'response_item', 'payload': {'type': 'message', 'id': 'message-1', 'role': 'user',
                                           'content': [{'type': 'input_text', 'text': 'SELECTED_RAW_TEXT'}]},
         'timestamp': '2026-11-14T01:00:01Z'},
    ]
    chosen = logs / 'selected.jsonl'
    chosen.write_text(''.join(json.dumps(row) + '\n' for row in records))
    (logs / 'unselected.jsonl').write_text('INVALID_UNSELECTED_MUST_NOT_BE_OPENED\n')
    config = source({'root': str(logs.parent)})
    selection = {'scopes': ['sessions/selected.jsonl']}
    reader = SourceReader(config, selection)
    result = encode_page(reader.read('live', None))
    material = page_materials(config, result)
    assert len(material['requests']) == 2
    assert material['requests'][1]['text'] == json.dumps(records[1]) + '\n'
    assert reader.read('live', result['cursor']).records == []
    assert 'INVALID_UNSELECTED' not in json.dumps(result)
    with pytest.raises(AdapterError, match='scope_denied'):
        SourceReader(config, {'scopes': ['../outside']}).read('live', None)
    # Exercise the actual CLI -> external selected adapter -> HTTP core -> full pipeline.
    from memu import cli
    service, model, embedding, index = service_fixture(tmp_path)
    tokens = {'owner': 'integration-fixture-' + 'a' * 32}
    server = CoreHTTPServer(service, service.config.core.model_copy(update={'port': 0}), tokens)
    thread = threading.Thread(target=server.run)
    thread.start()
    source_file, selection_file = tmp_path / 'source.json', tmp_path / 'selection.json'
    source_file.write_text(json.dumps(config))
    selection_file.write_text(json.dumps(selection))
    monkeypatch.setattr(cli, 'load_environment', lambda: {'MEMU_MCP_TOKENS': json.dumps(tokens),
                        'MEMU_CORE_URL': f'http://localhost:{server.server_port}'})
    try:
        assert cli.main(['collect', '--source', str(source_file),
                         '--selection', str(selection_file), '--from', 'owner',
                         '--state', str(tmp_path / 'actual-adapter-state.json')]) == 0
        assert model.calls['unit'] == model.calls['relate'] == index.search_calls == 2
        assert embedding.calls
        stored = [record for record in service.repo.catalog.values() if record['kind'] == 'source']
        assert len(stored) == 2
        assert any('SELECTED_RAW_TEXT' in (service.repo.root / item['path']).read_text() for item in stored)
        service.refresh_due(force=True)
        assert model.calls['rollup'] > 0
    finally:
        server.request_stop()
        thread.join(10)
        server.close()
