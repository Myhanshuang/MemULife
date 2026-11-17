"""QQ attachments are fetched only after selection, through the bound WebUI."""
import base64
import io
import json

import httpx
from PIL import Image

from memu.acquisition.adapters.qq import QQAdapter
from memu.acquisition.adapters.napcat_webui import _sessions
from memu.acquisition.sources.selection import Selection
from test_acquisition_chat_sources import message


def test_selected_qq_attachments_cross_container_boundary(monkeypatch):
    monkeypatch.setenv('QQ_MEDIA_WEBUI', 'fixture-password')
    _sessions.clear()
    out = io.BytesIO()
    Image.new('RGB', (2, 2)).save(out, format='PNG')
    png, pdf, mp3 = out.getvalue(), b'%PDF-fixture', b'fixture-mp3'
    calls = []
    parts = [{'type': kind, 'data': {'file': kind + '-id'}} for kind in ('image', 'file', 'record')]
    selected = message('selected', message=parts)
    excluded = message('excluded', time=1, message=parts)

    def handler(request):
        calls.append(request.url.path)
        body = json.loads(request.content) if request.content else {}
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'code': 0, 'data': {'Credential': 'session-only'}})
        assert request.headers['Authorization'] == 'Bearer session-only'
        if request.url.path == '/api/File/download':
            path = request.url.params['path']
            assert path in ('/container/media/image.png', '/container/media/file.pdf')
            return httpx.Response(200, content=png if path.endswith('.png') else pdf,
                                  headers={'Content-Type': 'application/octet-stream'})
        assert request.url.path == '/api/Debug/call'
        action, params = body['action'], body['params']
        if action == 'get_friend_msg_history':
            data = {'messages': [excluded, selected]}
        elif action == 'get_login_info':
            data = {'user_id': '123'}
        elif action == 'get_record':
            assert params == {'file': 'record-id', 'out_format': 'mp3'}
            data = {'file': '/container/media/voice.mp3', 'base64': base64.b64encode(mp3).decode()}
        else:
            assert action in {'get_image', 'get_file'}
            kind = 'image' if action == 'get_image' else 'file'
            assert params == {'file': kind + '-id'}
            name = 'image.png' if kind == 'image' else 'file.pdf'
            data = {'file': '/container/media/' + name, 'file_name': name}
        return httpx.Response(200, json={'code': 0, 'data': {'status': 'ok', 'retcode': 0, 'data': data}})

    reader = QQAdapter({'base_url': 'http://localhost:16099/webui/', 'webui_token_ref': 'env:QQ_MEDIA_WEBUI',
        'account_id': '123', 'conversations': [{'type': 'private', 'id': '456'}], 'history_order': 'oldest_first'},
        client=httpx.Client(transport=httpx.MockTransport(handler)))
    reader.import_selection = Selection(scopes=['private:456'], start='2020-01-01T00:00:00Z')
    before = reader.read('backfill')
    assert len(before.records) == 1 and all(a.data is None for a in before.records[0].attachments)
    assert '/api/File/download' not in calls
    reader.import_selection = reader.import_selection.model_copy(update={'include_attachments': True})
    page = reader.read('backfill')
    assert len(page.records) == 1 and page.records[0].raw == before.records[0].raw
    assert [(a.data, a.media_type, a.status) for a in page.records[0].attachments] == [
        (png, 'image/png', 'available'), (pdf, 'application/pdf', 'available'), (mp3, 'audio/mpeg', 'available')]
    assert [a.attachment_id for a in page.records[0].attachments] == ['part:0', 'part:1', 'part:2']
    assert calls.count('/api/File/download') == 2
    reader.close()
    _sessions.clear()


def test_failed_qq_attachment_does_not_discard_message():
    from memu.acquisition.adapters.qq_media import resolve
    from memu.acquisition.adapters.base import AdapterError, Attachment, Snapshot
    class Reader:
        def rpc(self, *args):
            raise AdapterError('upstream_read_failed')
    original = b'unchanged raw message'
    record = Snapshot('message', original, '正文', attachments=[
        Attachment('part:0', metadata={'kind': 'image', 'native': {'file': 'fixture'}})])
    resolve(Reader(), [record])
    assert record.raw == original and record.text == '正文'
    assert record.attachments[0].data is None
    assert record.attachments[0].metadata['reason'] == 'upstream_read_failed'
