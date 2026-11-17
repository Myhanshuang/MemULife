import hashlib
import io

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from memu.acquisition.adapters.local.common import LocalAccess
from memu.acquisition.adapters.wechat_content import project_message
from memu.acquisition.adapters.wechat_media import resolve_emoji
from memu.acquisition.sources.selection import Selection
from test_acquisition_chat_sources import create_wechat, row


def fixture():
    output = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(output, format='GIF')
    data = output.getvalue()
    md5 = hashlib.md5(data).hexdigest()
    return data, f'<msg><emoji md5="{md5}" cdnurl="http://wxapp.tc.qq.com/original?token=private"/></msg>'


def test_selected_emoji_preserves_gif_and_preview_never_downloads(tmp_path, monkeypatch):
    data, content = fixture()
    adapter, writers, _ = create_wechat(tmp_path, [[row(1, content=content, kind=47)]])
    calls = []

    def response(request):
        calls.append(request)
        assert request.url.scheme == 'https'
        return httpx.Response(200, content=data, headers={'content-type': 'image/jpg'})

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        monkeypatch.setattr('memu.acquisition.adapters.wechat_media.resolve_emoji',
                            lambda item, text, access: resolve_emoji(item, text, access, client=client))
        adapter.import_selection = Selection(scopes=['conversations:group@chatroom'], include_attachments=False)
        assert adapter.read('backfill').records[0].attachments[0].data is None
        assert calls == []
        adapter.import_selection = Selection(scopes=['conversations:group@chatroom'],
                                             start='2030-01-01T00:00:00Z', include_attachments=True)
        assert adapter.read('backfill').records == []
        assert calls == []
        adapter.import_selection = Selection(scopes=['conversations:group@chatroom'], include_attachments=True)
        image = adapter.read('backfill').records[0].attachments[0]
        assert len(calls) == 1 and image.status == 'available'
        assert image.data == data and image.media_type == 'image/gif'
        assert image.source_uri is None and 'private' not in str(image.metadata)
    for writer in writers:
        writer.close()


@pytest.mark.parametrize('failure,reason', [
    ('checksum', 'emoji_checksum_mismatch'), ('size', 'emoji_too_large'), ('redirect', 'emoji_url_not_allowed'),
])
def test_unverified_or_out_of_scope_emoji_stays_pending(tmp_path, failure, reason):
    _, content = fixture()
    image = project_message(content, 47)[1][0]
    calls = []

    def response(request):
        calls.append(request)
        if failure == 'redirect':
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        return httpx.Response(200, content=b'wrong bytes' if failure == 'checksum' else b'x' * 1025)

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        resolve_emoji(image, content, LocalAccess({'root': str(tmp_path), 'max_record_bytes': 1024}), client=client)
    assert len(calls) == 1
    assert image.data is None and image.status == 'pending' and image.metadata['reason'] == reason


def test_encrypted_original_fallback_is_checked_against_original_digest(tmp_path):
    data, content = fixture()
    key = bytes(range(16))
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    content = content.replace('/>', f' encrypturl="https://emoji.qpic.cn/encrypted" aeskey="{key.hex()}"/>')
    image = project_message(content, 47)[1][0]
    with httpx.Client(transport=httpx.MockTransport(lambda request:
            httpx.Response(404) if request.url.host == 'wxapp.tc.qq.com' else httpx.Response(200, content=encrypted))) as client:
        resolve_emoji(image, content, LocalAccess({'root': str(tmp_path)}), client=client)
    assert image.data == data and image.media_type == 'image/gif'
    assert 'aeskey' not in image.metadata
