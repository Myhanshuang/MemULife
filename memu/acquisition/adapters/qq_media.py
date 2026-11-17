"""Retrieve selected QQ attachments through NapCat, including remote containers."""
import base64
import binascii
import io
import mimetypes
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

import httpx
from PIL import Image

from .base import AdapterError, Attachment

MAX_BYTES = 16 * 1024 * 1024


def media_type(data, name, kind, supplied='application/octet-stream'):
    if kind == 'image':
        try:
            with Image.open(io.BytesIO(data)) as image:
                mime = Image.MIME.get(image.format)
                image.verify()
            if mime:
                return mime
        except (ValueError, OSError, Image.DecompressionBombError):
            pass
        raise AdapterError('attachment_invalid_image')
    if kind == 'record':
        return 'audio/mpeg'  # get_record explicitly requests MP3 conversion.
    mime = mimetypes.guess_type(name)[0]
    return mime or (supplied if supplied != 'application/json' else 'application/octet-stream')


def fetch(reader, ref):
    kind = ref.get('kind', 'file')
    native = ref.get('native', ref)
    identity = native.get('file_id') or native.get('file')
    if not isinstance(identity, str) or not identity or len(identity) > 8192:
        raise AdapterError('attachment_identity_required')
    action = {'image': 'get_image', 'record': 'get_record'}.get(kind, 'get_file')
    params = {'file': identity}
    if kind == 'record':
        params['out_format'] = 'mp3'
    value, _ = reader.rpc(action, params)
    if not isinstance(value, dict):
        raise AdapterError('incompatible_schema')
    size = value.get('file_size')
    if str(size).isdigit() and int(size) > MAX_BYTES:
        raise AdapterError('attachment_too_large')
    name = value.get('file_name') or native.get('name') or native.get('file_name') or ''
    name = PureWindowsPath(str(name)).name[:255]
    path, encoded = value.get('file'), value.get('base64')
    mime = 'application/octet-stream'
    if isinstance(encoded, str) and encoded:
        if len(encoded) > (MAX_BYTES + 2) // 3 * 4:
            raise AdapterError('attachment_too_large')
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise AdapterError('attachment_invalid_data') from None
    elif reader.webui and isinstance(path, str) and path:
        # Only paths returned by the selected attachment's RPC may be read.
        # A container path must never be opened on the collector's filesystem.
        if (len(path) > 8192 or '\x00' in path or
                not (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute())):
            raise AdapterError('attachment_path_invalid')
        _, credential = reader.webui._credential()
        data, mime = reader.http.request('POST', '/api/File/download',
            headers={'Authorization': 'Bearer ' + credential}, params={'path': path}, max_bytes=MAX_BYTES)
        if mime == 'application/json':
            raise AdapterError('attachment_unavailable')
        name = name or PureWindowsPath(path).name
    else:
        url = value.get('url')
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (not parsed or parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.hostname not in reader.config.get('media_hosts', [])):
            raise AdapterError('attachment_not_downloaded_on_source')
        try:
            # No NapCat credential may be forwarded to the media origin.
            with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
                with client.stream('GET', url) as response:
                    if response.status_code != 200:
                        raise AdapterError('attachment_unavailable')
                    data = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise AdapterError('attachment_too_large')
                    data = bytes(data)
                    mime = response.headers.get('content-type', mime).split(';', 1)[0]
        except httpx.HTTPError:
            raise AdapterError('attachment_unavailable', retryable=True) from None
    if not data:
        raise AdapterError('attachment_empty')
    return Attachment(identity, data, media_type(data, name, kind, mime), status='available',
                      metadata={'name': name, 'resolved_from': 'napcat'})


def resolve(reader, records):
    remaining = 24 * 1024 * 1024
    for record in records:
        for attachment in record.attachments:
            if attachment.data is not None:
                continue
            try:
                if remaining <= 0:
                    raise AdapterError('attachment_page_budget_exceeded')
                result = fetch(reader, attachment.metadata)
                if len(result.data) > remaining:
                    raise AdapterError('attachment_page_budget_exceeded')
                remaining -= len(result.data)
                attachment.data, attachment.media_type, attachment.status = result.data, result.media_type, 'available'
                attachment.metadata.update(result.metadata)
                attachment.metadata.pop('reason', None)
            except AdapterError as error:
                attachment.metadata['reason'] = error.code
