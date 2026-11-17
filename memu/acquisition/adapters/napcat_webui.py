"""Read-only calls through NapCat's existing authenticated WebUI interface."""

import hashlib
import json
import os
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from .base import AdapterError
from .http import SourceHTTP, environment_secret, json_bytes

_sessions = {}
_lock = threading.Lock()
_SESSION_ENV = 'MEMU_NAPCAT_READER_SESSION'


def endpoint(value):
    from .qq import safe_endpoint
    parsed = urlsplit(safe_endpoint(value))
    path = parsed.path.rstrip('/')
    if path.endswith('/webui'):
        path = path[:-6]
    return urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))


class NapCatWebUI:
    def __init__(self, config, http):
        self.config, self.http = config, http

    def _credential(self):
        secret = environment_secret(self.config['webui_token_ref'])
        key = hashlib.sha256((self.http.base_url + '\0' + secret).encode()).hexdigest()
        with _lock:
            now = time.time()
            for old in list(_sessions):
                if _sessions[old][0] <= now:
                    del _sessions[old]
            if key in _sessions:
                return key, _sessions[key][1]
            try:
                inherited = json.loads(os.environ.get(_SESSION_ENV, '{}'))
                credential = inherited.get('credential')
                if (inherited.get('key') == key and type(inherited.get('expires_at')) in {int, float}
                        and now < inherited['expires_at'] <= now + 3600
                        and isinstance(credential, str) and 0 < len(credential) <= 16384
                        and not any(c in credential for c in '\r\n')):
                    _sessions[key] = (inherited['expires_at'], credential)
                    return key, credential
            except (ValueError, AttributeError):
                pass
            value, _ = self.http.json('POST', '/api/auth/login', headers={}, body={
                'hash': hashlib.sha256((secret + '.napcat').encode()).hexdigest()}, max_bytes=65536)
            data = value.get('data')
            if isinstance(data, dict) and data.get('require2FA'):
                raise AdapterError('napcat_2fa_required')
            if value.get('code') != 0 or not isinstance(data, dict) or not isinstance(data.get('Credential'), str):
                raise AdapterError('napcat_webui_auth_failed')
            credential = data['Credential']
            if not credential or len(credential) > 16384 or any(c in credential for c in '\r\n'):
                raise AdapterError('incompatible_schema')
            if len(_sessions) >= 16:
                del _sessions[next(iter(_sessions))]
            _sessions[key] = (now + 3500, credential)
            return key, credential

    def call(self, action, parameters=None):
        from .qq import READ_ACTIONS
        if action not in READ_ACTIONS:
            raise AdapterError('action_not_allowed')
        key, credential = self._credential()
        try:
            value, _ = self.http.json('POST', '/api/Debug/call',
                headers={'Authorization': 'Bearer ' + credential}, body={'action': action, 'params': parameters or {}})
        except AdapterError as error:
            if error.code == 'source_unavailable':
                raise AdapterError('napcat_webui_unsupported') from None
            raise
        if value.get('code') != 0:
            if value.get('message') == 'Unauthorized':
                with _lock:
                    _sessions.pop(key, None)
                raise AdapterError('napcat_webui_auth_failed')
            raise AdapterError('upstream_read_failed')
        data = value.get('data')
        if not isinstance(data, dict) or data.get('status') != 'ok' or data.get('retcode') != 0:
            raise AdapterError('upstream_read_failed')
        return data.get('data'), json_bytes(data)


def connect(config):
    from .local.common import native_id
    http = SourceHTTP(endpoint(config['base_url']))
    try:
        api = NapCatWebUI(config, http)
        login, _ = api.call('get_login_info')
        status, _ = api.call('get_status')
        if not isinstance(login, dict) or not native_id(login.get('user_id')):
            raise AdapterError('incompatible_schema')
        if not isinstance(status, dict) or status.get('online') is not True:
            raise AdapterError('napcat_qq_offline')
        return {**config, 'base_url': http.base_url, 'account_id': native_id(login['user_id']),
                'browse_accessible': True, 'history_order': 'oldest_first'}
    finally:
        http.close()


def reader_environment(config):
    """Pass only this connection's volatile session to isolated read workers.

    Reuse avoids logging into WebUI again for every history page. The session
    is inherited by the child only; it is never added to stored source config.
    """
    http = SourceHTTP(endpoint(config['base_url']))
    try:
        key, credential = NapCatWebUI(config, http)._credential()
        with _lock:
            expires = _sessions[key][0]
        return {**os.environ, _SESSION_ENV: json.dumps({'key': key, 'credential': credential, 'expires_at': expires})}
    finally:
        http.close()
