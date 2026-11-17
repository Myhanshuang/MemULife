"""Bounded retrieval of selected WeChat emoji originals; credentials stay transient."""

import hashlib
import io
import re
from urllib.parse import urlsplit, urlunsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from .base import AdapterError
from .local.common import LocalAccess
from .wechat_content import xml


def _cdn_url(value):
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443} or parsed.fragment
                or not (host == 'wxapp.tc.qq.com' or host.endswith('.qpic.cn'))):
            raise ValueError()
        # Old message records use HTTP URLs for the same HTTPS-capable CDN.
        return urlunsplit(('https', host, parsed.path, parsed.query, ''))
    except (TypeError, ValueError):
        raise AdapterError('emoji_url_not_allowed') from None


def _download(client, url, budget):
    url = _cdn_url(url)
    for _ in range(3):
        try:
            with client.stream('GET', url, follow_redirects=False, timeout=15) as response:
                if response.is_redirect:
                    # Each redirect must independently stay on the supported CDN.
                    url = _cdn_url(str(response.url.join(response.headers.get('location', ''))))
                    continue
                if response.status_code != 200:
                    raise AdapterError('emoji_download_unavailable')
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    data.extend(chunk)
                    if len(data) > budget:
                        raise AdapterError('emoji_too_large')
                return bytes(data)
        except httpx.HTTPError:
            raise AdapterError('emoji_download_unavailable') from None
    raise AdapterError('emoji_redirect_limit')


def _decrypt(data, key, length):
    if not re.fullmatch('[0-9a-fA-F]{32}', key) or len(data) % 16:
        raise AdapterError('emoji_encryption_invalid')
    raw_key = bytes.fromhex(key)
    # WeChat's encrypted emoji payload uses CBC with the key as IV. Every
    # candidate still has to match the original MD5 before it can be accepted.
    # Format investigation: https://blog.seeflower.dev/archives/182/
    decryptor = Cipher(algorithms.AES(raw_key), modes.CBC(raw_key)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    try:
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(plain) + unpadder.finalize()
    except ValueError:
        return plain[:length] if length and length <= len(plain) else plain


def _validate(data, expected):
    if hashlib.md5(data).hexdigest() != expected:
        raise AdapterError('emoji_checksum_mismatch')
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {'GIF', 'PNG', 'JPEG', 'WEBP'} or image.width * image.height > 40_000_000:
                raise ValueError()
            result = (Image.MIME[image.format], {'width': image.width, 'height': image.height})
            image.verify()
            return result
    except (OSError, ValueError, Image.DecompressionBombError):
        raise AdapterError('emoji_image_invalid') from None


def _local(access, expected, budget):
    # Only the configured source and explicitly approved attachment roots are
    # readable. Never infer an account directory or substitute a thumbnail.
    for root in [access.root, *access.attachment_roots]:
        scoped = LocalAccess({'root': str(root), 'max_record_bytes': budget})
        for base in (root, root / 'business' / 'emoticon'):
            for relative in (expected, f'Persist/{expected[:2]}/{expected}',
                             f'PersistStore/{expected[:2]}/{expected}'):
                path = scoped.checked(base / relative)
                if path.is_file():
                    yield scoped.read_file(path)


def resolve_emoji(attachment, content, access, *, client=None):
    """Called only after conversation/date selection and include_attachments=True."""
    expected = attachment.metadata.get('content_md5')
    if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{32}', expected):
        attachment.metadata['reason'] = 'emoji_identity_missing'
        return attachment
    root = xml(content)
    emoji = root.find('.//emoji') if root is not None else None
    if emoji is None:
        attachment.metadata['reason'] = 'emoji_reference_missing'
        return attachment
    budget = min(access.max_bytes, 16 * 1024 * 1024)
    error_code = 'emoji_original_unavailable'

    def accept(data, origin):
        mime, dimensions = _validate(data, expected)
        attachment.data, attachment.media_type, attachment.status = data, mime, 'available'
        attachment.metadata.update(dimensions, resolved_from=origin)
        attachment.metadata.pop('reason', None)

    try:
        for data in _local(access, expected, budget):
            try:
                accept(data, 'local_emoji_cache')
                return attachment
            except AdapterError as error:
                error_code = error.code
    except (AdapterError, OSError) as error:
        error_code = error.code if isinstance(error, AdapterError) else 'emoji_local_unavailable'
    owned = client is None
    client = client or httpx.Client(trust_env=False, follow_redirects=False, timeout=15)
    try:
        # Do not use thumburl/externurl: they may be a thumbnail or another format.
        for field in ('cdnurl', 'encrypturl'):
            url = emoji.get(field)
            if not url:
                continue
            try:
                data = _download(client, url, budget)
                if field == 'encrypturl':
                    length = int(emoji.get('len', '0')) if emoji.get('len', '').isdigit() else 0
                    data = _decrypt(data, emoji.get('aeskey', ''), length)
                accept(data, 'wechat_emoji_cdn')
                return attachment
            except AdapterError as error:
                error_code = error.code
    finally:
        if owned:
            client.close()
    attachment.metadata['reason'] = error_code
    return attachment
