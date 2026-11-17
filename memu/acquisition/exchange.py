"""Browser discovery/previews in RAM; selected pushes enter the common outbox."""
import base64
from contextlib import contextmanager
import hashlib
import json
import re
import threading
import time
from uuid import UUID

from ..memulife import page_materials
from ..persistence import digest, encoded
from .adapters.base import AdapterError
from .adapters.native import capture_page
from .sources.discovery import discovery
from .sources.message_preview import previews
from .sources.messages import restrict_page
from .sources.selection import Selection

MAX_CAPTURE = 16 * 1024 * 1024


class BrowserExchange:
    def __init__(self, service):
        self.service = service
        self.transfers, self.lock = {}, threading.RLock()

    def binding(self, identity, source):
        config = self.service.binding(identity, source, producer=True)
        if config['adapter_id'] != 'browser.ai-history':
            raise AdapterError('source_binding_mismatch')
        return config

    def offer(self, identity, source, params):
        from ..events import event

        config = self.binding(identity, source)
        try:
            result = discovery.offer(config['tenant_id'], source, params.get('items', []), provider=config['source_profile'])
        except AdapterError as error:
            if error.code != 'import_catalog_capacity' or not self.service.watches.public(source)['enabled']:
                raise
            # The finite interactive catalogue must not cap an authorized source watch.
            from .sources.discovery import Discovery
            result = Discovery().offer(config['tenant_id'], source, params.get('items', []), provider=config['source_profile'])
        self.service.watches.browser_offer(config, params.get('items', []), params.get('scan_complete') is True)
        event('browser_catalog_offered', source_id=source, count=len(params.get('items', [])))
        return result

    def preview_owner(self, config, owner):
        return owner[:3] == (config['tenant_id'], config['source_instance_id'], config['owner']) and owner[4] == digest(config)

    def selection(self, identity, source, claim_previews=False):
        config = self.binding(identity, source)
        pending = []
        if claim_previews:
            for request in previews.pending(config['tenant_id'], source):
                if not self.preview_owner(config, request['owner']):
                    previews.discard(request['id'])
                elif len(pending) < 2:
                    value = previews.claim(request['id'])
                    if value:
                        pending.append(value)
        with self.service.lock:
            job = next((row for row in self.service.state['jobs'].values() if row['source_id'] == source
                        and row['state'] in {'queued', 'running'} and row['config_hash'] == digest(config)), None)
            if job is None:
                return {'job_id': None, 'scopes': [], 'persistence': 'denied', 'previews': pending}
            received = set(job['progress'].get('received', []))
            scopes = [scope for scope in job['selection']['scopes'] if scope not in received]
            return {'job_id': job['job_id'], 'scopes': scopes,
                    'include_attachments': job['selection'].get('include_attachments', False),
                    'items': [{'id': scope, 'locator': job['selection'].get('locators', {}).get(scope, scope),
                               'revision': None} for scope in scopes],
                    'persistence': 'selected_only', 'messages': {s: v for s, v in job['selection'].get('messages', {}).items()
                                                               if s in scopes}, 'previews': pending}

    def preview(self, identity, source, params):
        config = self.binding(identity, source)
        owner = previews.owner(params.get('request_id'))
        if (not self.preview_owner(config, owner) or owner[3] != params.get('conversation_id') or
                any(params.get(k) != config['config'].get(k) for k in ('provider', 'account_id', 'workspace_id'))):
            raise AdapterError('import_preview_scope_denied')
        return previews.complete(params['request_id'], params.get('messages', []), params.get('error_code'))

    def progress(self, identity, source, params):
        from ..events import event
        config = self.binding(identity, source)
        if any(params.get(k) != config['config'].get(k) for k in ('provider', 'account_id', 'workspace_id')):
            raise AdapterError('source_binding_mismatch')
        code, phase = params.get('error_code'), params.get('phase')
        diagnostics = params.get('diagnostics', {})
        version = params.get('extension_version', 'unknown')
        retry = params.get('retry_at')
        if (not isinstance(code, str) or not re.fullmatch(r'[a-z0-9_]{1,80}', code)
                or phase not in {'detail', 'attachments', 'delivery'}
                or not isinstance(diagnostics, dict) or set(diagnostics) - {'stage', 'http_status'}
                or not isinstance(version, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,32}', version)
                or retry is not None and (type(retry) not in {int, float} or not 0 <= retry <= 253402300799000)):
            raise AdapterError('invalid_request')
        stage, status = diagnostics.get('stage'), diagnostics.get('http_status')
        if (stage is not None and (not isinstance(stage, str) or not re.fullmatch(r'[a-z_]{1,40}', stage))
                or status is not None and (type(status) is not int or not 100 <= status <= 599)):
            raise AdapterError('invalid_request')
        with self.service.lock:
            job = self.service.state['jobs'].get(params.get('import_job_id'))
            if not job or job['source_id'] != source or job['config_hash'] != digest(config):
                raise AdapterError('import_scope_denied')
            if job['state'] not in {'queued', 'running'} or job.get('delivery_complete'):
                return {'recorded': False}
            job['error_code'] = code
            job['progress']['browser'] = {'phase': phase, 'error_code': code, 'diagnostics': diagnostics,
                'extension_version': version, 'retry_at': retry}
            from .service import now
            job['updated_at'] = now()
            self.service.save()
        event('browser_import_error', job_id=job['job_id'], error_code=code, stage=stage or phase,
            **({'http_status': status} if status is not None else {}))
        return {'recorded': True}

    @contextmanager
    def admitted(self, identity, source, job_id, object_ids):
        with self.service.lock:
            config = self.binding(identity, source)
            job = self.service.state['jobs'].get(job_id)
            if (not job or job['source_id'] != source or job['config_hash'] != digest(config)
                    or job['state'] not in {'queued', 'running', 'completed'}):
                raise AdapterError('import_selection_required')
            if (not object_ids or len(object_ids) > 100 or len(object_ids) != len(set(object_ids))
                    or not set(object_ids) <= set(job['selection']['scopes'])):
                raise AdapterError('import_scope_denied')
            yield job, config

    def capture(self, identity, source, params):
        if len(encoded(params)) > MAX_CAPTURE:
            raise AdapterError('capture_too_large')
        rows = params.get('records')
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get('object_id'), str) for row in rows):
            raise AdapterError('source_record_invalid')
        ids = [row['object_id'] for row in rows]
        with self.admitted(identity, source, params.get('import_job_id'), ids) as (job, config):
            batch, checksum = str(UUID(params['batch_id'])), digest(params)
            old = job['receipts'].get(batch)
            if old:
                if old['digest'] != checksum:
                    raise AdapterError('import_capture_conflict')
                return old['receipt']
            if job['state'] == 'completed':
                raise AdapterError('import_terminal')
            received = set(job['progress'].get('received', []))
            if received.intersection(ids):
                raise AdapterError('import_already_received')
            page = restrict_page(capture_page(config, params), Selection.model_validate(job['selection']), config['source_profile'])
            bridge = self.service.bridge(job, config, batch)
            # Stage and receipt recovery also cover a crash between these two durable commits.
            with bridge.locked():
                state = bridge.load()
                if state.get('capture_digest') not in {None, checksum}:
                    raise AdapterError('import_capture_conflict')
                if state.get('capture_digest') is None:
                    state['pending'] = page_materials(config, page)
                    state['capture_digest'] = checksum
                    bridge.save(state)
            receipt = {'batch_id': batch, 'durable_level': 'edge_durable', 'accepted': len(rows),
                       'memory_status': 'queued'}
            job['receipts'][batch] = {'digest': checksum, 'receipt': receipt, 'processed': False}
            job['progress']['received'] = sorted(received.union(ids))
            if job['progress'].get('browser'):
                job['progress']['browser'] = {'phase': 'received'}
                job['error_code'] = None
            self.service.save()
            self.service.changed.set()
            return receipt

    def chunk(self, identity, source, params):
        identities = params.get('object_ids')
        if not isinstance(identities, list) or any(not isinstance(v, str) for v in identities):
            raise AdapterError('import_scope_denied')
        with self.admitted(identity, source, params.get('import_job_id'), identities):
            transfer = str(UUID(params['transfer_id']))
            index, total = params.get('index'), params.get('total')
            if type(index) is not int or type(total) is not int or not 0 <= index < total <= 86:
                raise AdapterError('transfer_index_invalid')
            data = base64.b64decode(params['data_base64'], validate=True)
            if not 0 < len(data) <= 192 * 1024:
                raise AdapterError('transfer_chunk_too_large')
            header = (params['sha256'], total, params['import_job_id'], tuple(identities))
            key = (identity, source, transfer)
            with self.lock:
                stamp = time.monotonic()
                self.transfers = {k: v for k, v in self.transfers.items() if v['expires'] > stamp}
                value = self.transfers.get(key, {'header': header, 'parts': {}, 'expires': stamp + 600})
                if value['header'] != header or index in value['parts'] and value['parts'][index] != data:
                    raise AdapterError('transfer_conflict')
                if sum(len(part) for v in self.transfers.values() for part in v['parts'].values()) + len(data) > 32 * 1024 * 1024:
                    raise AdapterError('transfer_memory_capacity')
                value['parts'][index] = data
                self.transfers[key] = value
            return {'transfer_id': transfer, 'index': index, 'stored': True, 'storage': 'volatile'}

    def commit(self, identity, source, params):
        self.binding(identity, source)
        transfer = str(UUID(params['transfer_id']))
        key = (identity, source, transfer)
        with self.lock:
            value = self.transfers.get(key)
            if not value or value['expires'] < time.monotonic():
                raise AdapterError('transfer_incomplete', True)
            checksum, total, job_id, identities = value['header']
            if len(value['parts']) != total:
                raise AdapterError('transfer_incomplete', True)
            data = b''.join(value['parts'][i] for i in range(total))
        if len(data) > MAX_CAPTURE or hashlib.sha256(data).hexdigest() != checksum or checksum != params['sha256']:
            raise AdapterError('transfer_checksum_mismatch')
        request = json.loads(data)
        capture = request.get('params', {})
        if (request.get('method') != 'capture' or capture.get('batch_id') != transfer or
                capture.get('import_job_id') != job_id or [r.get('object_id') for r in capture.get('records', [])] != list(identities)):
            raise AdapterError('transfer_identity_mismatch')
        result = self.capture(identity, source, capture)
        with self.lock:
            self.transfers.pop(key, None)
        return result
