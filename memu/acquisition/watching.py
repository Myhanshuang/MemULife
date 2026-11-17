"""Source-wide opt-in import of existing content and subsequent changes."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import threading
import time
from uuid import uuid4

from ..persistence import digest
from .adapters.base import AdapterError
from .collector import SourceReader, prepare
from .sources import get_source
from .sources.selection import Selection


def stamp():
    return datetime.now(timezone.utc).isoformat()


class WatchReader:
    def __init__(self, watches, binding, job):
        self.watches, self.job = watches, deepcopy(job)
        self.reader = SourceReader(binding, job['selection'])

    def close(self):
        self.reader.close()

    def read(self, mode, cursor=None):
        page = self.reader.read(mode, cursor)
        sid = self.job['subscription_id']
        seen = self.watches.seen(sid, [r.object_id for r in page.records])
        kept, checkpoint = [], {}
        for record in page.records:
            fingerprint = hashlib.sha256(record.raw).hexdigest()
            if seen.get(record.object_id) == fingerprint:
                continue
            fresh = False
            if record.occurred_at:
                try:
                    fresh = datetime.fromisoformat(record.occurred_at.replace('Z', '+00:00')) >= datetime.fromisoformat(self.job['watch_started_at'])
                except (ValueError, TypeError):
                    pass
            if not self.job['watch_baseline'] or fresh:
                kept.append(record)
            # Failed attachments are retried on a later source scan, not silently checkpointed.
            if self.job['watch_baseline'] and not fresh or all(
                    a.data is not None or a.status == 'excluded' for a in record.attachments):
                checkpoint[record.object_id] = fingerprint
        page.records, page.watch_checkpoint = kept, checkpoint
        return page


class SourceWatches:
    def __init__(self, service):
        self.service = service
        self.schedule_lock = threading.Lock()
        self.fallback_seen = service.state.setdefault('watch_seen', {}) if not service.store else {}

    def row(self, source_id):
        return next((r for r in self.service.state['subscriptions'].values()
                     if r.get('scope') == 'source' and r['source_id'] == source_id and r['state'] != 'cancelled'), None)

    def public(self, source_id):
        with self.service.lock:
            row = self.row(source_id)
            if not row:
                return {'enabled': False, 'state': 'off', 'interval_seconds': 900, 'include_attachments': True}
            return {**{k: deepcopy(row.get(k)) for k in ('subscription_id', 'state', 'interval_seconds',
                    'baseline', 'last_success_at', 'error_code', 'cycles', 'started_at')},
                    'enabled': row['state'] == 'active', 'include_attachments': True}

    def configure(self, identity, source_id, enabled, interval=900):
        service = self.service
        binding = service.binding(identity, source_id)
        if type(enabled) is not bool or type(interval) is not int or not 150 <= interval <= 86400:
            raise AdapterError('invalid_request')
        if not get_source(binding['adapter_id']).continuous_updates:
            raise AdapterError('capability_unavailable')
        with service.lock:
            row = self.row(source_id)
            if row and enabled and row.get('initial_import') != 'all':
                # Expanding from the old new-only mode is an explicit enable action.
                service.control_subscription(identity, row['subscription_id'], 'cancel')
                row = None
            if row:
                if enabled:
                    if row['config_hash'] != digest(binding):
                        raise AdapterError('import_plan_stale')
                    service.control_subscription(identity, row['subscription_id'], 'resume')
                    row.update(interval_seconds=interval, next_run_at=time.time(), error_code=None)
                else:
                    service.control_subscription(identity, row['subscription_id'], 'pause')
            elif enabled:
                sid, started = str(uuid4()), stamp()
                service.state['subscriptions'][sid] = dict(subscription_id=sid, seed_job_id=None, scope='source',
                    source_id=source_id, owner=identity, state='active', interval_seconds=interval,
                    selection={}, config_hash=digest(binding), current_job_id=None, next_run_at=time.time(),
                    started_at=started, baseline=False, initial_import='all', pending={}, cycle_scopes=None,
                    last_success_at=None, cycles=0, error_code=None)
            service.save()
            service.changed.set()
            return self.public(source_id)

    def seen(self, sid, identities):
        if self.service.store:
            return self.service.store.watch_items(sid, identities)
        rows = self.fallback_seen.get(sid, {})
        return {key: rows[key] for key in identities if key in rows}

    def remember(self, sid, values):
        if self.service.store:
            self.service.store.save_watch_items(sid, values)
        else:
            self.fallback_seen.setdefault(sid, {}).update(values)

    def acknowledge(self, job, result):
        if job.get('watch_started_at'):
            sid = job['subscription_id']
            staged = sid + ':' + job['job_id']
            self.remember(staged, result.get('watch_checkpoint', {}))
            if not result.get('has_more'):
                if self.service.store:
                    self.service.store.commit_watch_items(staged, sid)
                else:
                    self.remember(sid, self.fallback_seen.pop(staged, {}))

    def browser_offer(self, binding, items, complete=False):
        service = self.service
        with service.lock:
            row = self.row(binding['source_instance_id'])
            if not row or row['state'] != 'active':
                return
            if row['config_hash'] != digest(binding):
                row.update(state='paused', error_code='import_plan_stale')
                service.save()
                return
            seen = self.seen(row['subscription_id'], [item['id'] for item in items])
            baseline = {}
            child = service.state['jobs'].get(row['current_job_id'], {})
            inflight = child.get('watch_versions', {})
            for item in items:
                key = item['id']
                fingerprint = digest([item.get('revision'), item.get('updated_at'), item.get('message_count')])
                if fingerprint == seen.get(key) or fingerprint == inflight.get(key):
                    continue
                fresh = False
                date = item.get('updated_at') or item.get('created_at')
                if date:
                    fresh = datetime.fromisoformat(date.replace('Z', '+00:00')) >= datetime.fromisoformat(row['started_at'])
                if row['baseline'] and key not in seen and not fresh:
                    baseline[key] = fingerprint
                else:
                    row['pending'][key] = {'fingerprint': fingerprint, 'locator': item['locator']}
            self.remember(row['subscription_id'], baseline)
            if complete:
                row['baseline'] = False
            service.save()
            service.changed.set()

    def scopes(self, binding):
        module = get_source(binding['adapter_id'])
        if module.import_driver != 'files':
            return [row['id'] for row in module.scopes(binding)]
        # The directory binding authorizes future Markdown files in its tree.
        result, directories = [], ['.']
        while directories:
            parent, cursor = directories.pop(), ''
            while True:
                page = module.candidates(binding, parent=parent, cursor=cursor, limit=200)
                for item in page['items']:
                    (directories if item['kind'] == 'directory' else result).append(item['id'])
                if len(result) + len(directories) > 10000:
                    raise AdapterError('import_catalog_too_large')
                cursor = page['next_cursor']
                if not cursor:
                    break
        return result

    def scope_versions(self, binding, scopes):
        if binding['adapter_id'] not in {'filesystem.typora', 'ai.local-logs'}:
            return {}
        root = Path(binding['config']['root']).resolve()
        versions = {}
        for scope in scopes:
            path = root / scope
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise AdapterError('import_scope_denied')
            try:
                value = path.stat()
            except FileNotFoundError:
                continue
            versions[scope] = f'{value.st_dev}:{value.st_ino}:{value.st_size}:{value.st_mtime_ns}'
        return versions

    def schedule(self):
        if not self.schedule_lock.acquire(blocking=False):
            return None
        try:
            service, deadlines = self.service, []
            with service.lock:
                ids = [r['subscription_id'] for r in service.state['subscriptions'].values()
                       if r.get('scope') == 'source' and r['state'] == 'active']
            for sid in ids:
                with service.lock:
                    row = service.state['subscriptions'][sid]
                    if row['state'] != 'active':
                        continue
                    child = service.state['jobs'].get(row['current_job_id'])
                    if child:
                        if child['state'] == 'completed' or child.get('delivery_complete'):
                            if child.get('watch_versions') and not child['progress'].get('attachments', {}).get('pending'):
                                self.remember(sid, child['watch_versions'])
                            if child.get('watch_scope_versions') and not child['progress'].get('attachments', {}).get('pending'):
                                self.remember(sid + '/scopes', child['watch_scope_versions'])
                            row.update(current_job_id=None, last_success_at=child['updated_at'], error_code=None)
                        elif child['state'] in {'paused', 'cancelled'}:
                            row.update(state='paused', error_code=child.get('error_code'))
                            service.save()
                            continue
                        else:
                            continue
                    if row['next_run_at'] > time.time():
                        deadlines.append(row['next_run_at'])
                        service.save()
                        continue
                    snapshot = deepcopy(row)
                try:
                    binding = service.binding(snapshot['owner'], snapshot['source_id'])
                    if digest(binding) != snapshot['config_hash']:
                        raise AdapterError('import_plan_stale')
                    browser = binding['adapter_id'] == 'browser.ai-history'
                    scopes = list(snapshot['pending']) if browser else snapshot['cycle_scopes']
                    if scopes is None:
                        scopes = self.scopes(binding)
                    versions = self.scope_versions(binding, scopes)
                    if binding['adapter_id'] in {'filesystem.typora', 'ai.local-logs'}:
                        previous = self.seen(sid + '/scopes', list(versions))
                        scopes = [scope for scope in scopes if scope in versions and versions[scope] != previous.get(scope)]
                    chosen, remaining = scopes[:200], scopes[200:]
                    if chosen:
                        selected = prepare(binding, Selection(scopes=chosen, include_attachments=True,
                            locators={key: snapshot['pending'][key]['locator'] for key in chosen} if browser else {}))
                    with service.lock:
                        row = service.state['subscriptions'][sid]
                        if row['state'] != 'active':
                            continue
                        service.recheck(row['owner'], row['source_id'], binding)
                        if chosen:
                            child = service.new_job(row['owner'], row['source_id'], selected.model_dump(mode='json'),
                                row['config_hash'], sid)
                            child.update(watch_started_at=row['started_at'], watch_baseline=row['baseline'])
                            child['watch_scope_versions'] = {key: versions[key] for key in chosen if key in versions}
                            row['current_job_id'] = child['job_id']
                            if browser:
                                child['watch_versions'] = {key: snapshot['pending'][key]['fingerprint'] for key in chosen}
                                for key in chosen:
                                    if row['pending'].get(key) == snapshot['pending'][key]:
                                        del row['pending'][key]
                            else:
                                row['cycle_scopes'] = remaining
                            row['next_run_at'] = time.time()
                        else:
                            row.update(next_run_at=time.time() + row['interval_seconds'])
                            if not browser:
                                row.update(baseline=False, cycle_scopes=None, cycles=row['cycles'] + 1)
                            deadlines.append(row['next_run_at'])
                        service.save()
                except Exception as error:
                    with service.lock:
                        service.state['subscriptions'][sid].update(state='paused',
                            error_code=getattr(error, 'code', 'acquisition_failed'))
                        service.save()
            return max(0, min(deadlines) - time.time()) if deadlines else None
        finally:
            self.schedule_lock.release()
