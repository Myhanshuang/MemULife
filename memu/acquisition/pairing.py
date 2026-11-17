"""Explicit, short-lived browser pairing; resulting keys only deliver one source."""
import hashlib
import json
import re
import secrets
import time

from ..persistence import digest, private_save
from .adapters.base import AdapterError
from .sources import get_source


class BrowserPairing:
    def __init__(self, service):
        self.service = service
        self.path = service.root / 'browser-access.json'
        self.sessions = json.loads(self.path.read_bytes()) if self.path.exists() else {}
        self.pending = {}

    def prune(self):
        stamp = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if v['expires'] > stamp}

    def allowed_origin(self, origin):
        if not isinstance(origin, str) or not re.fullmatch(r'chrome-extension://[a-p]{32}', origin):
            return False
        with self.service.lock:
            self.prune()
            return any(row['origin'] == origin for row in [*self.pending.values(), *self.sessions.values()])

    def create(self, identity, provider, name, extension_id):
        self.service.owner(identity)
        if provider not in get_source('browser.ai-history').profiles or not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise AdapterError('source_config_invalid')
        if not isinstance(extension_id, str) or not re.fullmatch('[a-p]{32}', extension_id):
            raise AdapterError('extension_id_invalid')
        with self.service.lock:
            self.prune()
            if len(self.pending) >= 100:
                raise AdapterError('pairing_capacity')
            code = secrets.token_urlsafe(24)
            self.pending[code] = {'owner': identity, 'provider': provider, 'name': name.strip(),
                                  'origin': 'chrome-extension://' + extension_id, 'expires': time.monotonic() + 600}
            return {'code': code, 'expires_in': 600, 'provider': provider}

    def authorize(self, origin, code, provider):
        with self.service.lock:
            self.prune()
            row = self.pending.get(code) if isinstance(code, str) else None
            if not row or row['origin'] != origin or row['provider'] != provider:
                raise AdapterError('pairing_invalid')
            return {'authorized': True, 'expires_in': max(0, int(row['expires'] - time.monotonic()))}

    def pair(self, origin, code, account):
        with self.service.lock:
            self.prune()
            pending = self.pending.get(code) if isinstance(code, str) else None
            if not pending or pending['origin'] != origin:
                raise AdapterError('pairing_invalid')
            if not isinstance(account, dict) or account.get('provider') != pending['provider']:
                raise AdapterError('source_binding_mismatch')
            config = {key: account.get(key) for key in ('provider', 'account_id', 'workspace_id')}
            if any(not isinstance(v, str) or not 1 <= len(v) <= 2048 for v in config.values()):
                raise AdapterError('source_binding_mismatch')
            if 'result' in pending:
                if pending['account_hash'] != digest(config):
                    raise AdapterError('pairing_invalid')
                return pending['result']  # Lost acknowledgement: same code, origin and exact account only.
            existing = next((row for row in self.service.state['sources'].values() if row['owner'] == pending['owner']
                and row['adapter_id'] == 'browser.ai-history' and row['config'] == config and row.get('enabled', True)), None)
            source = ({'source_id': existing['source_instance_id']} if existing else self.service.register(pending['owner'],
                {'adapter_id': 'browser.ai-history', 'source_profile': config['provider'],
                 'source_namespace': pending['name'], 'config': config}))
            token = secrets.token_urlsafe(32)
            checksum = hashlib.sha256(token.encode()).hexdigest()
            self.sessions[checksum] = {'source_id': source['source_id'], 'owner': pending['owner'], 'origin': origin}
            private_save(self.path, self.sessions)
            result = {'source_id': source['source_id'], 'token': token, **config}
            pending.update(result=result, account_hash=digest(config))
            return result

    def status(self, identity, code):
        self.service.owner(identity)
        with self.service.lock:
            self.prune()
            row = self.pending.get(code) if isinstance(code, str) else None
            if not row or row['owner'] != identity:
                raise AdapterError('pairing_invalid')
            if 'result' not in row:
                return {'state': 'waiting'}
            return {'state': 'paired', 'source_id': row['result']['source_id']}

    def revoke(self, identity, source):
        self.service.owner(identity)
        with self.service.lock:
            binding = self.service.state['sources'].get(source)
            if not binding or binding['owner'] != identity:
                raise AdapterError('scope_denied')
            self.sessions = {k: v for k, v in self.sessions.items() if v['source_id'] != source}
            self.pending = {k: v for k, v in self.pending.items() if v.get('result', {}).get('source_id') != source}
            private_save(self.path, self.sessions)
        return {'revoked': True}

    def authenticate(self, origin, token):
        with self.service.lock:
            row = self.sessions.get(hashlib.sha256(token.encode()).hexdigest())
            if not row or row['origin'] != origin:
                raise AdapterError('scope_denied')
            self.service.binding(row['owner'], row['source_id'])
            return row

    def dispatch(self, exchange, origin, token, request):
        row = self.authenticate(origin, token)
        if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('params'), dict):
            raise AdapterError('invalid_request')
        params, source = request['params'], row['source_id']
        config = self.service.binding(row['owner'], source)
        if any(params.get(key) != config['config'][key] for key in ('provider', 'account_id', 'workspace_id')):
            raise AdapterError('source_binding_mismatch')
        routes = {'selection.get': 'selection', 'catalog.offer': 'offer', 'preview.offer': 'preview', 'progress.offer': 'progress',
                  'capture': 'capture', 'capture.chunk': 'chunk', 'capture.commit': 'commit'}
        operation = routes.get(request.get('method'))
        if not operation:
            raise AdapterError('scope_denied')
        result = (exchange.selection(config['source_identity'], source, params.get('claim_previews') is True)
                  if operation == 'selection' else getattr(exchange, operation)(config['source_identity'], source, params))
        return {'jsonrpc': '2.0', 'id': request.get('id'), 'result': result}
