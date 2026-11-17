"""Selected acquisition jobs and control state; all memory work goes to the core.

PostgreSQL control records contain bindings, selections and receipts, never unselected
content. Candidate/preview pools are volatile. Selected pages use the durable
common-ingress outbox, including browser pushes acknowledged before model work.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

from ..memulife import MemULifeBridge, BridgeError
from ..persistence import digest, private_save
from .adapters.base import AdapterError
from .collector import SourceReader, prepare, validate_binding
from .sources import get_source, sources
from .sources.selection import Selection
from .setup import SourceCredentials


def now():
    return datetime.now(timezone.utc).isoformat()


class AcquisitionService:
    def __init__(self, root, client, config, *, workers=2, database_url=None):
        if type(workers) is not int or not 1 <= workers <= 8:
            raise ValueError('Acquisition workers must be between 1 and 8')
        self.workers, self.threads, self.inflight, self.readers = workers, [], set(), {}
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'control.json'
        if not database_url and (self.root / 'control.postgres.json').exists():
            raise ValueError('PostgreSQL is required for acquisition control')
        self.process_lock = (self.root / '.service.lock').open('a+b')
        try:
            fcntl.flock(self.process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.process_lock.close()
            raise AdapterError('acquisition_already_running') from None
        self.client, self.config = client, config
        self.lock = threading.RLock()
        self.changed, self.stopped = threading.Event(), threading.Event()
        self.store = None
        if database_url:
            from .control_store import AcquisitionStore
            self.store = AcquisitionStore(self.root, database_url)
        self.state = self.store.state if self.store else json.loads(self.path.read_bytes()) if self.path.exists() else {
            'version': 1, 'sources': {}, 'jobs': {}, 'subscriptions': {},
        }
        if self.state.get('version') != 1:
            raise AdapterError('acquisition_state_incompatible')
        self.credentials = SourceCredentials(self.root)
        from .feishu_authorization import FeishuAuthorization
        self.feishu_authorization = FeishuAuthorization(self)
        from .watching import SourceWatches
        self.watches = SourceWatches(self)
        for source_id in self.state['sources']:
            self.credentials.install(self.credentials.values.get(source_id, {}))
        for job in self.state['jobs'].values():
            if job['state'] == 'running':
                job.update(state='paused', error_code='interrupted_resume_required')
        self.save()
        self.thread = None

    def save(self):
        if self.store is not None:
            self.store.save()
        else:
            private_save(self.path, self.state)

    def owner(self, identity):
        if identity not in self.config.access.owners or 'write' not in self.config.access.whitelist.get(identity, []):
            raise AdapterError('scope_denied')

    def binding(self, identity, source_id, *, producer=False):
        with self.lock:
            value = self.state['sources'].get(source_id)
            if not value or not value.get('enabled', True):
                raise AdapterError('import_binding_unavailable')
            if producer:
                if identity != value['source_identity']:
                    raise AdapterError('scope_denied')
            else:
                self.owner(identity)
                if identity != value['owner']:
                    raise AdapterError('scope_denied')
            binding = deepcopy(value)
            # Display-only edits must not invalidate frozen import plans.
            binding.pop('display_name', None)
            return binding

    def rename(self, identity, source_id, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200 or any(ord(c) < 32 for c in name):
            raise AdapterError('source_namespace_required')
        with self.lock:
            self.binding(identity, source_id)
            self.state['sources'][source_id]['display_name'] = name.strip()
            self.save()
        return {'source_id': source_id, 'namespace': name.strip()}

    def delete_source(self, identity, source_id):
        self.owner(identity)
        with self.lock:
            source = self.state['sources'].get(source_id)
            if not source or source['owner'] != identity:
                raise AdapterError('scope_denied')
            # Retain the identity for historical receipts, but revoke all new
            # admissions before touching credentials or browser delivery keys.
            source.update(enabled=False, deleted_at=source.get('deleted_at') or now())
            for job in self.state['jobs'].values():
                if (job['source_id'] == source_id and not job.get('delivery_complete')
                        and job['state'] not in {'completed', 'cancelled'}):
                    job.update(state='cancelled', updated_at=now())
            for subscription in self.state['subscriptions'].values():
                if subscription['source_id'] == source_id:
                    subscription.update(state='cancelled', updated_at=now())
            self.save()
            self.credentials.remove(source_id)
            self.changed.set()
        return {'source_id': source_id, 'deleted': True}

    def register(self, identity, binding, source_identity=None, credentials=None, schema_text=None):
        self.owner(identity)
        source_id = str(uuid4())
        schema_file = None
        if schema_text is not None:
            if (binding.get('adapter_id') != 'immich.assets' or not isinstance(schema_text, str)
                    or len(schema_text.encode()) > 8 * 1024 * 1024):
                raise AdapterError('source_schema_invalid')
            try:
                schema = json.loads(schema_text)
                if not isinstance(schema, dict) or not isinstance(schema.get('paths'), dict):
                    raise ValueError
            except ValueError:
                raise AdapterError('source_schema_invalid') from None
            schema_file = self.root / 'schemas' / (source_id + '.json')
            binding = {**binding, 'config': {**binding['config'], 'schema_path': str(schema_file),
                       'schema_sha256': digest(schema)}}
        binding, private = self.credentials.prepare(source_id, binding, {} if credentials is None else credentials)
        from .connection_setup import complete_connection
        with self.credentials.temporary(private):
            binding = complete_connection(binding)
        validate_binding(binding)
        writer = source_identity or identity
        if writer not in self.client.tokens or 'write' not in self.config.access.whitelist.get(writer, []):
            raise AdapterError('source_identity_requires_write')
        value = {key: binding[key] for key in ('adapter_id', 'source_profile', 'source_namespace', 'config')}
        value.update(source_instance_id=source_id, owner=identity, source_identity=writer,
                     tenant_id=identity, device_id=str(uuid4()), enabled=True)
        if not isinstance(value['source_namespace'], str) or not value['source_namespace'].strip():
            raise AdapterError('source_namespace_required')
        with self.lock:
            if schema_file:
                schema_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                private_save(schema_file, schema)
            self.credentials.save(source_id, private)
            self.state['sources'][source_id] = value
            self.save()
        return {'source_id': source_id, 'status': 'configured', 'content_persisted': False}

    def inventory(self, identity):
        self.owner(identity)
        with self.lock:
            values = deepcopy(list(self.state['sources'].values()))
        return {'profiles': [{'adapter_id': module.adapter_id, **module.profile()} for module in sources()],
                'sources': [{'source_id': row['source_instance_id'], 'adapter_id': row['adapter_id'],
                             'namespace': row.get('display_name', row['source_namespace']), 'enabled': row['enabled'],
                             'deleted_at': row.get('deleted_at'),
                             'profile': row['source_profile'], 'page_kind': get_source(row['adapter_id']).page_kind,
                             'slug': get_source(row['adapter_id']).slug}
                            for row in values if row['owner'] == identity]}

    def describe(self, identity, source_id):
        binding = self.binding(identity, source_id)
        module = get_source(binding['adapter_id'])
        with self.lock:
            name = self.state['sources'][source_id].get('display_name', binding['source_namespace'])
        return {'source_id': source_id, 'adapter_id': module.adapter_id, 'namespace': name,
                'profile': binding['source_profile'], **module.profile(), 'enabled': True,
                'time_filter': module.time_filter, 'scopes': module.scopes(binding), 'binding_available': True,
                'continuous_updates': {'supported': module.continuous_updates, 'minimum_interval_seconds': 150}}

    def recheck(self, identity, source_id, value):
        if digest(self.binding(identity, source_id)) != digest(value):
            raise AdapterError('import_plan_stale')

    def candidates(self, identity, source_id, **query):
        binding = self.binding(identity, source_id)
        result = get_source(binding['adapter_id']).candidates(binding, **query)
        self.recheck(identity, source_id, binding)
        return result

    def preview(self, identity, source_id, selection, cursor='', limit=30):
        binding = self.binding(identity, source_id)
        module = get_source(binding['adapter_id'])
        selected = Selection.model_validate(selection)
        module.validate(binding, selected)
        result = module.preview_page({**binding, '_preview_key': identity}, selected, cursor, limit)
        self.recheck(identity, source_id, binding)
        return result

    def plan(self, identity, source_id, selection):
        binding = self.binding(identity, source_id)
        selected = prepare(binding, selection)
        self.recheck(identity, source_id, binding)
        return {'source_id': source_id, 'selection': selected.model_dump(mode='json'),
                'config_hash': digest(binding), 'scope_count': len(selected.scopes),
                'persistence': 'after_selection', 'publication': 'memory_pipeline',
                'attachments': 'included' if selected.include_attachments else 'references_only',
                'time_interval': 'start_inclusive_end_exclusive',
                'message': '只采集所选原始素材，并经完整记忆流程生成可检索的记忆。'}

    @staticmethod
    def check_batch(selections):
        scopes = [scope for selection in selections for scope in selection.scopes]
        if not selections or len(selections) > 200 or len(scopes) > 200:
            raise AdapterError('import_selection_too_large')
        if len(scopes) != len(set(scopes)):
            raise AdapterError('import_duplicate_scope')
        if sum(len(v) for selection in selections for v in selection.messages.values()) > 500:
            raise AdapterError('import_selection_too_large')

    def plan_batch(self, identity, source_id, selections):
        self.check_batch([Selection.model_validate(value) for value in selections])
        plans = [self.plan(identity, source_id, value) for value in selections]
        self.check_batch([Selection.model_validate(plan['selection']) for plan in plans])
        return {'plans': plans}

    def create_batch(self, identity, source_id, plans):
        binding = self.binding(identity, source_id)
        module = get_source(binding['adapter_id'])
        selections = [Selection.model_validate(plan['selection']) for plan in plans]
        self.check_batch(selections)
        for selection, plan in zip(selections, plans):
            if plan['config_hash'] != digest(binding):
                raise AdapterError('import_plan_stale')
            module.validate(binding, selection)
            module.verify_plan(binding, selection)
        with self.lock:
            self.recheck(identity, source_id, binding)
            if self.watches.public(source_id)['enabled']:
                raise AdapterError('source_watch_active')
            for selection in selections:
                if any(job['source_id'] == source_id and job['selection'] == selection.model_dump(mode='json')
                       and job['state'] in {'queued', 'running', 'paused'} for job in self.state['jobs'].values()):
                    raise AdapterError('import_already_active')
            jobs = [self.new_job(identity, source_id, selection.model_dump(mode='json'), digest(binding))
                    for selection in selections]
            self.save()
            self.changed.set()
            return {'jobs': deepcopy(jobs)}

    def new_job(self, owner, source_id, selection, config_hash, subscription_id=None):
        identity = str(uuid4())
        job = {'job_id': identity, 'owner': owner, 'source_id': source_id, 'selection': selection,
               'config_hash': config_hash, 'state': 'queued', 'progress': {}, 'receipts': {},
               'subscription_id': subscription_id, 'created_at': now(), 'updated_at': now(),
               'error_code': None, 'error_retryable': False}
        binding = self.state['sources'][source_id]
        if binding['adapter_id'] == 'browser.ai-history':
            known = {item['id']: item for item in get_source(binding['adapter_id']).scopes(binding)}
            job['selected_items'] = [{key: value for key, value in known.get(scope, {'id': scope}).items()
                                      if key in {'id', 'title', 'name', 'provider', 'locator'}} for scope in selection['scopes']]
        self.state['jobs'][identity] = job
        return self.state['jobs'][identity]

    def jobs(self, identity):
        self.owner(identity)
        with self.lock:
            rows = sorted(self.state['jobs'].values(), key=lambda item: item['created_at'], reverse=True)
            return {'jobs': [{**{k: deepcopy(v) for k, v in row.items() if k not in {'receipts', 'owner', 'config_hash', 'progress'}},
                             'progress': {k: deepcopy(v) for k, v in row.get('progress', {}).items() if k != 'write_results'},
                             'stage': ('browser_error' if row.get('error_code') else 'awaiting_browser') if row['state'] == 'queued' and not row['receipts'] and self.state['sources'][row['source_id']]['adapter_id'] == 'browser.ai-history' else row['state']}
                             for row in rows if row['owner'] == identity][:100]}

    def job_detail(self, identity, job_id):
        self.owner(identity)
        with self.lock:
            job = self.state['jobs'].get(job_id)
            if not job or job['owner'] != identity:
                raise AdapterError('scope_denied')
            result = deepcopy({key: value for key, value in job.items() if key not in {'receipts', 'owner', 'config_hash'}})
            # Deleted connections still own their historical import receipts.
            binding = deepcopy(self.state['sources'][job['source_id']])
        result['provider'] = binding['source_profile']
        result['items'] = result.get('selected_items', [{'id': scope} for scope in result['selection']['scopes']])
        refs = []
        for write in result['progress'].get('write_results', []):
            refs.extend(write.get('sources', [])); refs.extend(write.get('units', []))
        if not refs and binding['adapter_id'] == 'browser.ai-history':
            try:
                lookup = self.client.request('POST', '/v1/memory/import-result', identity, {
                    'source_instance_id': binding['source_instance_id'], 'object_ids': result['selection']['scopes']})
                refs = lookup.get('refs', [])
            except Exception:
                result['memory_unavailable'] = True
        if refs:
            response = self.client.call(identity, 'memory_read', {'from': identity, 'refs': list(dict.fromkeys(refs))[:100], 'budget_chars': 24000})
            from .sources.browser_content import material_content
            for record in response.get('result', {}).get('results', []):
                if record.get('kind') == 'source' and binding['adapter_id'] == 'browser.ai-history' and not record.get('truncated'):
                    try:
                        value = json.loads(record['body'])
                        object_id = value.get('native_conversation_id') if isinstance(value, dict) else None
                        if object_id and object_id in result['selection']['scopes']:
                            normalized = material_content(record['body'].encode(), binding['source_profile'], object_id)
                            if normalized:
                                record['body'] = normalized['text']
                                record['display_format'] = 'readable_original_messages'
                    except (ValueError, TypeError, AdapterError):
                        pass
            result['memory'] = response
        return result

    def control(self, identity, job_id, action):
        self.owner(identity)
        with self.lock:
            job = self.state['jobs'].get(job_id)
            if not job or job['owner'] != identity:
                raise AdapterError('scope_denied')
            if job['subscription_id']:
                raise AdapterError('import_subscription_control_required')
            self._control(job, action)
            self.save()
            self.changed.set()
            return {'job_id': job_id, 'state': job['state']}

    def _control(self, job, action):
        if action not in {'pause', 'resume', 'cancel'}:
            raise AdapterError('invalid_request')
        if job['state'] in {'completed', 'cancelled'}:
            raise AdapterError('import_terminal')
        job.update(state={'pause': 'paused', 'resume': 'queued', 'cancel': 'cancelled'}[action], updated_at=now())
        if action == 'resume':
            job['error_code'] = None
            job['next_check_at'] = 0

    @contextmanager
    def admitted(self, job_id):
        with self.lock:
            job = self.state['jobs'][job_id]
            if job['state'] not in {'queued', 'running'}:
                raise AdapterError('import_not_active')
            binding = self.binding(job['owner'], job['source_id'])
            if digest(binding) != job['config_hash']:
                raise AdapterError('import_plan_stale')
            yield job, binding

    def bridge(self, job, binding, batch=None):
        path = self.root / 'outbox' / (job['job_id'] + ('-' + batch if batch else '') + '.json')
        return MemULifeBridge(self.client, binding['owner'], binding, job['selection'], path,
                              mode='backfill', admission=lambda: self.admitted(job['job_id']),
                              checkpoint_store=self.store)

    def run_once(self):
        with self.lock:
            eligible = []
            for job in self.state['jobs'].values():
                if job['job_id'] in self.inflight:
                    continue
                if job['state'] not in {'queued', 'running', 'processing'}:
                    continue
                if job.get('next_check_at', 0) > time.time():
                    continue
                binding = self.state['sources'].get(job['source_id'], {})
                if not job.get('delivery_complete') and get_source(binding['adapter_id']).import_driver == 'push' and not any(
                        not row.get('processed') for row in job['receipts'].values()):
                    continue
                eligible.append(job)
            if not eligible:
                return False
            job = min(eligible, key=lambda item: (item['job_id'] not in self.readers, item['updated_at']))
            job_id = job['job_id']
            self.inflight.add(job_id)
        try:
            return self._run_job(job_id)
        finally:
            with self.lock:
                self.inflight.discard(job_id)
                job = self.state['jobs'][job_id]
                if job.get('delivery_complete') or job['state'] in {'paused', 'cancelled', 'completed'}:
                    reader = self.readers.pop(job_id, None)
                    if reader is not None:
                        reader.close()
            self.changed.set()

    def _run_job(self, job_id):
        with self.lock:
            job = self.state['jobs'][job_id]
            if job['state'] not in {'queued', 'running', 'processing'}:
                return False
            delivered = job.get('delivery_complete')
            if not delivered:
                job.update(state='running', updated_at=now())
                self.save()
        if delivered:
            self.check_writes(job)
            return True
        try:
            with self.admitted(job_id) as (job, binding):
                selected_job = {key: deepcopy(job[key]) for key in ('job_id', 'owner', 'selection')}
                selected_binding = deepcopy(binding)
                push = get_source(binding['adapter_id']).import_driver == 'push'
                batch = next((key for key, row in job['receipts'].items() if not row.get('processed')), None) if push else None
            bridge = self.bridge(selected_job, selected_binding, batch)
            if push:
                with bridge.locked():
                    state = bridge.load()
                    result = bridge.deliver(state) if state['pending'] else state['last_result']
            else:
                reader = self.readers.get(job_id)
                if reader is None:
                    if job.get('watch_started_at'):
                        from .watching import WatchReader
                        reader = WatchReader(self.watches, selected_binding, job)
                    else:
                        reader = SourceReader(selected_binding, selected_job['selection'])
                    self.readers[job_id] = reader
                result = bridge.collect(reader, resume=True, max_pages=1)
            with self.lock:
                job = self.state['jobs'][job_id]
                if result['status'] == 'processing':
                    job['next_check_at'] = result['check_at']
                    job['progress']['request_id'] = result['request_id']
                elif result['status'] == 'pending':
                    if job['state'] not in {'cancelled', 'paused'}:
                        job.update(state='paused', error_code=result['error_code'])
                    job['progress']['request_id'] = result.get('request_id')
                else:
                    self.watches.acknowledge(job, result)
                    progress = job['progress']
                    progress['selected'] = progress.get('selected', 0) + result['committed']
                    progress['pages'] = progress.get('pages', 0) + 1
                    if 'pending_writes' not in progress:
                        progress['pending_writes'] = sum(row.get('status') != 'done'
                                                         for row in progress.get('write_results', []))
                    progress['pending_writes'] += sum(row.get('status') != 'done' for row in result.get('write_results', []))
                    progress.setdefault('write_results', []).extend(result.get('write_results', []))
                    progress['warnings'] = sorted(set(progress.get('warnings', []) + result.get('warnings', [])))
                    totals = progress.setdefault('attachments', {})
                    for name, count in result.get('attachment_summary', {}).items():
                        totals[name] = totals.get(name, 0) + count
                    if push:
                        job['receipts'][batch]['processed'] = True
                        complete = (set(progress.get('received', [])) == set(job['selection']['scopes']) and
                                    all(row.get('processed') for row in job['receipts'].values()))
                    else:
                        complete = not result.get('has_more', False)
                    if job['state'] == 'running':
                        pending = progress['pending_writes'] > 0
                        job['delivery_complete'] = complete
                        job['state'] = ('processing' if pending else 'completed') if complete else 'queued'
                        if complete and pending:
                            job['next_check_at'] = time.time() + 150
                job['updated_at'] = now()
                self.save()
        except Exception as error:
            from ..events import event
            with self.lock:
                job = self.state['jobs'][job_id]
                if job['state'] not in {'cancelled', 'paused'}:
                    code = error.code if isinstance(error, AdapterError) else str(error) if isinstance(error, BridgeError) else 'acquisition_failed'
                    job.update(state='paused', error_code=code, error_retryable=getattr(error, 'retryable', False), updated_at=now())
                    self.save()
                    event('acquisition_paused', job_id=job_id, error_code=code, error_type=type(error).__name__)
        return True

    def check_writes(self, job):
        """Only state queries after durable admission, at most once per 150 seconds."""
        from ..core_client import CoreUnavailable
        with self.lock:
            rows = [row for row in job['progress'].get('write_results', []) if row.get('status') != 'done']
        try:
            for start in range(0, len(rows), 200):
                batch = rows[start:start + 200]
                if hasattr(self.client, 'statuses'):
                    response = self.client.statuses(job['owner'], [row['request_id'] for row in batch])
                    results = response.get('results') if isinstance(response, dict) else None
                    if not isinstance(results, list):
                        raise CoreUnavailable('Invalid receipt batch')
                else:
                    results = [self.client.status(job['owner'], row['request_id']).get('result', {}) for row in batch]
                with self.lock:
                    if job['state'] in {'paused', 'cancelled'}:
                        return
                    if len(results) != len(batch):
                        raise CoreUnavailable('Incomplete receipt batch')
                    for row, result in zip(batch, results):
                        if result.get('request_id') != row['request_id'] or result.get('status') not in {
                                'done', 'queued', 'running', 'waiting', 'failed', 'superseded'}:
                            raise CoreUnavailable('Invalid receipt batch')
                        if any(row.get(key) != value for key, value in result.items()):
                            row.update(result)
                        if 'error' not in result:
                            row.pop('error', None)
                        if result['status'] == 'failed':
                            job.update(state='paused', error_code=result.get('error', {}).get('code', 'core_task_failed'))
                            self.save()
                            return
            with self.lock:
                if job['state'] in {'paused', 'cancelled'}:
                    return
                pending = any(row.get('status') != 'done' for row in job['progress'].get('write_results', []))
                job['progress']['pending_writes'] = sum(row.get('status') != 'done'
                                                       for row in job['progress'].get('write_results', []))
                job.update(state='processing' if pending else 'completed', next_check_at=time.time() + 150,
                           updated_at=now())
                if job.get('error_code') == 'core_status_unavailable':
                    job['error_code'] = None
                self.save()
        except CoreUnavailable:
            with self.lock:
                if job['state'] not in {'paused', 'cancelled'}:
                    job.update(state='processing', next_check_at=time.time() + 150,
                               error_code='core_status_unavailable', updated_at=now())
                    self.save()

    def subscriptions(self, identity):
        self.owner(identity)
        with self.lock:
            return {'subscriptions': [{k: deepcopy(v) for k, v in row.items() if k != 'owner'}
                                      for row in self.state['subscriptions'].values() if row['owner'] == identity]}

    def subscribe(self, identity, job_id, interval=900):
        self.owner(identity)
        if type(interval) is not int or not 150 <= interval <= 86400:
            raise AdapterError('invalid_interval')
        with self.lock:
            job = self.state['jobs'].get(job_id)
            if not job or job['owner'] != identity or job['state'] != 'completed':
                raise AdapterError('completed_import_required')
            binding = self.binding(identity, job['source_id'])
            if not get_source(binding['adapter_id']).continuous_updates:
                raise AdapterError('capability_unavailable')
            if any(row['seed_job_id'] == job_id and row['state'] != 'cancelled' for row in self.state['subscriptions'].values()):
                raise AdapterError('sync_already_exists')
            sid = str(uuid4())
            self.state['subscriptions'][sid] = {
                'subscription_id': sid, 'seed_job_id': job_id, 'source_id': job['source_id'], 'owner': identity,
                'state': 'active', 'interval_seconds': interval, 'selection': deepcopy(job['selection']),
                'config_hash': job['config_hash'], 'current_job_id': None, 'next_run_at': time.time() + interval,
                'last_success_at': job['updated_at'], 'cycles': 0, 'error_code': None,
                'watermark': job['created_at'], 'reconciled_at': job['created_at'],
            }
            self.save()
            self.changed.set()
            return {'subscription_id': sid, 'state': 'active'}

    def control_subscription(self, identity, sid, action):
        self.owner(identity)
        with self.lock:
            row = self.state['subscriptions'].get(sid)
            if not row or row['owner'] != identity:
                raise AdapterError('scope_denied')
            if row['state'] == 'cancelled':
                raise AdapterError('sync_terminal')
            if action not in {'pause', 'resume', 'cancel'}:
                raise AdapterError('invalid_request')
            row['state'] = {'pause': 'paused', 'resume': 'active', 'cancel': 'cancelled'}[action]
            child = self.state['jobs'].get(row['current_job_id'])
            if child and child['state'] not in {'completed', 'cancelled'}:
                self._control(child, action)
            row['next_run_at'] = time.time() + row['interval_seconds']
            self.save()
            self.changed.set()
            return {'subscription_id': sid, 'state': row['state']}

    def schedule(self):
        watch_delay = self.watches.schedule()
        with self.lock:
            deadlines = [time.time() + watch_delay] if watch_delay is not None else []
            for row in self.state['subscriptions'].values():
                if row['state'] != 'active' or row.get('scope') == 'source':
                    continue
                child = self.state['jobs'].get(row['current_job_id'])
                if child:
                    if child['state'] == 'completed':
                        if child['selection'].get('start') == row['selection'].get('start'):
                            row['reconciled_at'] = child['created_at']
                        row.update(current_job_id=None, last_success_at=child['updated_at'],
                                   watermark=child['created_at'],
                                   cycles=row['cycles'] + 1, next_run_at=time.time() + row['interval_seconds'])
                        self.save()
                    elif child['state'] in {'paused', 'cancelled'}:
                        row.update(state='paused', error_code=child['error_code'])
                        self.save()
                        continue
                    else:
                        continue
                if row['next_run_at'] > time.time():
                    deadlines.append(row['next_run_at'])
                    continue
                try:
                    binding = self.binding(row['owner'], row['source_id'])
                    if digest(binding) != row['config_hash']:
                        raise AdapterError('import_plan_stale')
                    module = get_source(binding['adapter_id'])
                    selected = Selection.model_validate(row['selection'])
                    # Refresh only the already frozen scopes; new files/conversations remain excluded.
                    subscription = {**row, 'watermark': datetime.fromisoformat(row['watermark']),
                                    'reconciled_at': datetime.fromisoformat(row['reconciled_at'])}
                    selected = module.refresh_selection(binding, selected, subscription, datetime.now(timezone.utc))
                    module.validate(binding, selected)
                    module.verify_plan(binding, selected)
                    child = self.new_job(row['owner'], row['source_id'], selected.model_dump(mode='json'),
                                         row['config_hash'], row['subscription_id'])
                    row['current_job_id'] = child['job_id']
                except Exception as error:
                    row.update(state='paused', error_code=getattr(error, 'code', 'acquisition_failed'))
                self.save()
            return max(0, min(deadlines) - time.time()) if deadlines else None

    def start(self):
        def work():
            while not self.stopped.is_set():
                self.changed.clear()
                delay = self.schedule()
                if not self.run_once():
                    with self.lock:
                        deadlines = [job['next_check_at'] for job in self.state['jobs'].values()
                                     if job['job_id'] not in self.inflight
                                     and job['state'] in {'running', 'queued', 'processing'} and job.get('next_check_at')]
                    if deadlines:
                        pending_delay = max(0, min(deadlines) - time.time())
                        delay = pending_delay if delay is None else min(delay, pending_delay)
                    self.changed.wait(delay)
        self.threads = [threading.Thread(target=work, name=f'memu-acquisition-{i}', daemon=True)
                        for i in range(self.workers)]
        self.thread = self.threads[0]
        for thread in self.threads:
            thread.start()
        return self

    def close(self):
        self.stopped.set()
        self.changed.set()
        for thread in self.threads:
            thread.join()
        for reader in self.readers.values():
            reader.close()
        self.readers.clear()
        self.process_lock.close()
        self.credentials.close()
        if self.store is not None:
            self.store.close()
