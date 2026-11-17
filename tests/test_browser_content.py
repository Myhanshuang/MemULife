import json
import pytest
from memu.memulife import snapshot_material
from memu.acquisition.sources.browser_content import material_content
from memu.acquisition.adapters.base import AdapterError
from test_acquisition_pipeline import acquisition, files_source, create


def envelope():
    return {'format': 'memulife.browser-conversation.v2', 'provider': 'gemini', 'native_conversation_id': 'c_test',
            'messages': [{'id': 'turn:r:user', 'role': 'user', 'text': 'Quoted third-party narrative. What does it mean?', 'occurred_at': '2026-10-13T01:00:00.000Z'},
                         {'id': 'candidate:rc', 'role': 'assistant', 'text': 'This is a hypothesis.', 'occurred_at': '2026-10-13T01:00:40.000Z'}],
            'time_basis': {'user': 'native_turn_time', 'assistant': 'native_candidate_time'},
            'responses': ['OPAQUE_TRANSPORT_AND_RENDER_DUPLICATION']}


def test_browser_material_uses_original_messages_and_keeps_wire_only_in_audit():
    value = envelope()
    binding = {'adapter_id': 'browser.ai-history', 'source_profile': 'gemini', 'source_instance_id': 'synthetic', 'source_namespace': 'fixture'}
    result = snapshot_material(binding, {'object_id': 'c_test', 'raw': json.dumps(value).encode()})
    assert '用户' in result['text'] and 'AI 助手' in result['text']
    assert 'OPAQUE_TRANSPORT' not in result['text']
    assert result['metadata']['memulife']['transport_audit']['responses'] == value['responses']
    assert result['time'] == {'precision': 'range', 'value': {'from': '2026-10-13T01:00:00.000Z', 'to': '2026-10-13T01:00:40.000Z'}}
    value['messages'][1]['occurred_at'] = None
    assert material_content(json.dumps(value).encode(), 'gemini', 'c_test')['time']['precision'] == 'unknown'
    with pytest.raises(AdapterError):
        material_content(json.dumps(value).encode(), 'claude', 'c_test')


def test_import_result_and_job_detail_include_only_matching_source_evidence(acquisition, tmp_path):
    service, core, _, client = acquisition
    notes = tmp_path / 'notes'; notes.mkdir(); (notes / 'selected.md').write_text('SELECTED_FIXTURE')
    source = files_source(service, notes); job = create(service, source, ['selected.md'])
    assert service.run_once()
    details = service.job_detail('owner', job['job_id'])
    assert details['memory']['is_error'] is False
    records = details['memory']['result']['results']
    assert {r['kind'] for r in records} == {'source', 'unit'}
    assert any('SELECTED_FIXTURE' in r['body'] for r in records)
    assert core.import_result('owner', source, ['unselected.md'])['refs'] == []
    assert core.import_result('owner', source, ['selected.md'])['refs']
    with pytest.raises(PermissionError): core.import_result('reader', source, ['selected.md'])
    with pytest.raises(AdapterError): service.job_detail('reader', job['job_id'])


def test_legacy_gemini_wire_decode_agrees_with_explicit_message_times_and_preserves_evidence():
    turn = [['c_test', 'r_one'], None, [['Quoted question']], [[['rc_one', ['Answer only']]], None,
            [[1, None, '', 'rc_one', [1791873040, 123456789]]]], [1791873000, 987654321]]
    # Render tree duplication and telemetry are deliberately outside reviewed message paths.
    turn[3].append({'render_tree': 'DO_NOT_DUPLICATE'})
    wire = ")]}'\n123\n" + json.dumps([['wrb.fr', 'hNvQHb', json.dumps([[turn], None])]]) + '\n[["di",123]]\n'
    value = {'format': 'memulife.browser-response-set.v1', 'provider': 'gemini', 'native_conversation_id': 'c_test', 'responses': [wire]}
    normalized = material_content(json.dumps(value).encode(), 'gemini', 'c_test')
    assert normalized['message_count'] == 2
    assert normalized['text'].count('Answer only') == 1
    assert 'DO_NOT_DUPLICATE' not in normalized['text']
    assert normalized['audit']['responses'] == [wire]
    assert normalized['time']['precision'] == 'range'


def test_import_lookup_http_is_owner_only_and_never_writes(acquisition, tmp_path):
    import http.client
    import threading
    from memu.core_api import CoreHTTPServer
    service, core, _, client = acquisition
    server = CoreHTTPServer(core, core.config.core.model_copy(update={'port': 0}), client.tokens)
    thread = threading.Thread(target=server.run); thread.start()
    def request(identity, value):
        conn = http.client.HTTPConnection('localhost', server.server_port, timeout=5)
        conn.request('POST', '/v1/memory/import-result', json.dumps(value),
                     {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + client.tokens[identity]})
        response = conn.getresponse(); result = response.status, response.read(); conn.close(); return result
    try:
        value = {'source_instance_id': 'not-a-source', 'object_ids': ['one']}
        assert request('reader', value)[0] == 403
        response = request('owner', value)
        assert response[0] == 200 and json.loads(response[1]) == {'refs': []}
        assert request('owner', {'source_instance_id': [], 'object_ids': []})[0] == 422
        assert not core.repo.state['writes']
    finally:
        server.request_stop(); thread.join(5); server.close()
