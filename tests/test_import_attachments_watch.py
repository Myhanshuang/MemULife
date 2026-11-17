import base64
from copy import deepcopy
import hashlib
import json
from urllib.parse import urlsplit

import pytest

from memu.config import Config
from memu.acquisition.service import AcquisitionService
from memu.acquisition.adapters.native import capture_page
from memu.acquisition.sources.messages import restrict_page
from memu.acquisition.sources.selection import Selection
from memu.acquisition.adapters.base import AdapterError
from memu.memulife import page_materials


def browser_capture():
    envelope = {'format': 'memulife.browser-conversation.v2', 'provider': 'chatgpt',
        'native_conversation_id': 'conversation', 'messages': [
            {'id': 'question', 'role': 'user', 'text': '请看附件', 'occurred_at': None}],
        'responses': ['{}']}
    original = json.dumps(envelope).encode()
    binding = {'adapter_id': 'browser.ai-history', 'source_profile': 'chatgpt',
        'source_instance_id': 'fixture', 'source_namespace': 'fixture',
        'config': {'provider': 'chatgpt', 'account_id': 'account', 'workspace_id': 'workspace'}}
    records = [{'object_id': 'conversation', 'raw': original.decode(), 'text': '',
        'metadata': {'original_raw_sha256': hashlib.sha256(original).hexdigest(),
            'attachments': 'originals', 'attachment_coverage': 'native_references'},
        'attachments': [{'attachment_id': name, 'media_type': mime, 'status': 'available',
            'data': base64.b64encode(data).decode(), 'metadata': {'name': name, 'message_ids': ['question']}}
            for name, mime, data in [('photo.jpg', 'image/jpeg', b'original-image'),
                ('source.py', 'text/plain', b'print("attachment")'), ('paper.pdf', 'application/pdf', b'%PDF-original')]]}]
    return binding, {**binding['config'], 'batch_id': '00000000-0000-4000-8000-000000000001', 'records': records, 'cursor': {}}


def test_native_attachments_reach_material_without_text_normalization_loss():
    binding, params = browser_capture()
    page = restrict_page(capture_page(binding, params), Selection(scopes=['conversation'], include_attachments=True), 'chatgpt')
    result = page_materials(binding, page)
    material = result['requests'][0]
    assert base64.b64decode(material['media'][0]['data_base64']) == b'original-image'
    assert '请看附件' in material['text'] and 'print("attachment")' in material['text']
    retained = material['metadata']['memulife']['attachment_originals'][0]
    assert base64.b64decode(retained['data_base64']) == b'%PDF-original'
    assert result['attachment_summary'] == dict(available=3, pending=0, excluded=0, unparsed=1)
    assert result['warnings'] == ['attachment_unparsed']
    with pytest.raises(AdapterError, match='import_attachment_scope_denied'):
        restrict_page(capture_page(binding, params), Selection(scopes=['conversation']), 'chatgpt')
    params['records'][0]['attachments'][0]['data'] = 'broken base64'
    with pytest.raises(AdapterError, match='source_attachment_invalid'):
        capture_page(binding, params)


def test_pending_attachments_visible_and_completed_projection_can_replace_partial():
    binding, params = browser_capture()
    complete = page_materials(binding, capture_page(binding, params))['requests'][0]
    for item in params['records'][0]['attachments']:
        item.update(status='pending', data=None)
    result = page_materials(binding, capture_page(binding, params))
    assert result['attachment_summary']['pending'] == 3 and 'attachment_pending' in result['warnings']
    partial = result['requests'][0]
    assert partial['idempotency_key'] == complete['idempotency_key']
    assert partial['metadata']['projection']['generation'] < complete['metadata']['projection']['generation']


class AdmissionClient:
    url = urlsplit('http://no-model.invalid')
    tokens = {'owner': 'synthetic-local-test-token'}

    def __init__(self):
        self.calls = []

    def write(self, identity, material):
        self.calls.append(deepcopy(material))
        return {'is_error': False, 'result': {'status': 'done', 'request_id': str(len(self.calls)),
            'sources': [], 'units': [], 'views': [], 'index_ready': True}}


def make_service(tmp_path):
    config = Config.model_validate({'access': {'owners': ['owner'], 'whitelist': {'owner': ['write', 'read']}}})
    return AcquisitionService(tmp_path / 'acquisition', AdmissionClient(), config)


def test_source_watch_imports_history_discovers_new_files_and_pauses(tmp_path):
    notes = tmp_path / 'notes'; notes.mkdir()
    (notes / 'old.md').write_text('history')
    service = make_service(tmp_path)
    try:
        source = service.register('owner', {'adapter_id': 'filesystem.typora', 'source_profile': 'linux-markdown-v1',
            'source_namespace': 'fixture', 'config': {'root': str(notes)}})['source_id']
        service.watches.configure('owner', source, True)
        service.schedule(); assert service.run_once()
        service.schedule()
        assert [x['text'] for x in service.client.calls] == ['history']
        row = service.watches.row(source)
        assert not row['baseline']
        (notes / 'old.md').write_text('updated')
        (notes / 'new.md').write_text('new content')
        row['next_run_at'] = 0
        service.schedule(); assert service.run_once(); service.schedule()
        assert {x['text'] for x in service.client.calls} == {'history', 'updated', 'new content'}
        row['next_run_at'] = 0
        service.schedule(); assert not service.run_once()
        assert len(service.client.calls) == 3
        service.watches.configure('owner', source, False)
        (notes / 'newer.md').write_text('must not import while paused')
        row['next_run_at'] = 0; service.schedule()
        assert not service.run_once() and len(service.client.calls) == 3
    finally:
        service.close()


def test_browser_watch_history_new_and_changed_conversations_include_attachments(tmp_path):
    service = make_service(tmp_path)
    try:
        source = service.register('owner', {'adapter_id': 'browser.ai-history', 'source_profile': 'chatgpt',
            'source_namespace': 'fixture', 'config': {'provider': 'chatgpt', 'account_id': 'a', 'workspace_id': 'w'}})['source_id']
        service.watches.configure('owner', source, True)
        binding = service.binding('owner', source)
        old = {'id': 'old', 'locator': 'old', 'revision': '1', 'updated_at': '2020-01-01T00:00:00Z'}
        service.watches.browser_offer(binding, [old], True)
        service.schedule()
        row = service.watches.row(source)
        first = service.state['jobs'][row['current_job_id']]
        assert first['selection']['scopes'] == ['old'] and first['selection']['include_attachments']
        first['state'] = 'completed'
        service.schedule()
        service.watches.browser_offer(binding, [{**old, 'revision': '2'}, {'id': 'new', 'locator': 'new', 'revision': '1'}])
        row = service.watches.row(source); row['next_run_at'] = 0
        service.schedule()
        job = service.state['jobs'][row['current_job_id']]
        assert set(job['selection']['scopes']) == {'old', 'new'} and job['selection']['include_attachments']
        service.watches.configure('owner', source, False)
        assert job['state'] == 'paused'
    finally:
        service.close()


def test_browser_errors_are_scoped_to_pending_import_and_reject_unsafe_diagnostics(tmp_path):
    from memu.acquisition.exchange import BrowserExchange
    from memu.persistence import digest
    service = make_service(tmp_path)
    try:
        source = service.register('owner', {'adapter_id': 'browser.ai-history', 'source_profile': 'chatgpt',
            'source_namespace': 'fixture', 'config': {'provider': 'chatgpt', 'account_id': 'a', 'workspace_id': 'w'}})['source_id']
        binding = service.binding('owner', source)
        job = service.new_job('owner', source, {'scopes': ['conversation']}, digest(binding))
        exchange = BrowserExchange(service)
        params = {**binding['config'], 'import_job_id': job['job_id'], 'error_code': 'attachment_access_denied',
            'phase': 'attachments', 'extension_version': '0.6.1',
            'diagnostics': {'stage': 'resolve_url', 'http_status': 403}}
        producer = binding['source_identity']
        assert exchange.progress(producer, source, params) == {'recorded': True}
        assert service.jobs('owner')['jobs'][0]['stage'] == 'browser_error'
        assert job['progress']['browser']['diagnostics']['http_status'] == 403
        with pytest.raises(AdapterError, match='source_binding_mismatch'):
            exchange.progress(producer, source, {**params, 'account_id': 'another'})
        with pytest.raises(AdapterError, match='invalid_request'):
            exchange.progress(producer, source, {**params, 'diagnostics': {'url': 'private'}})
        job['state'] = 'completed'
        assert exchange.progress(producer, source, params) == {'recorded': False}
    finally:
        service.close()
