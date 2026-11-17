"""Evidence-backed topic discovery; Unit tags never create Runtime files by themselves."""
from copy import deepcopy
import hashlib
import json
import os
import time

from .refs import Ref
from .types import UnitDraft, time_scopes
from .retry import failure, error_code
from .events import event


def fingerprint(unit):
    value = {key: unit.get(key) for key in ('ref', 'body', 'time', 'facts')}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class TopicMaintenance:
    def _init_topics(self):
        # An operator may prioritize one explicit recovery set for this process.
        # The ordinary scheduler remains the default for all future starts.
        recovery = os.environ.get('MEMU_TEMP_VIEW_PRIORITY_REFS', '')
        self._view_recovery_refs = frozenset(filter(None, recovery.split(',')))
        if 'topic_registry' not in self.repo.state:
            self.repo.state['topic_registry'] = {'definitions': {}, 'seen': {},
                'job': {'state': 'done', 'generation': 0}}
            if self.repo.iter_blocks(['unit', 'runtime']):
                self._queue_topics(immediate=True)
        job = self.repo.state['topic_registry']['job']
        if job['state'] == 'running':
            job['state'] = 'queued'

    def _queue_topics(self, *, immediate=False, rescan=False):
        registry = self.repo.state['topic_registry']
        if rescan:
            registry['seen'] = {}
        job = registry['job']
        job.update(state='queued', generation=job['generation'] + 1,
                   attempts=0, due=time.time() + (0 if immediate else self.config.views.refresh_delay_seconds))
        job.pop('error', None)

    def _topics(self):
        return [{'key': key, 'title': key, 'aliases': [], 'description': value['description'],
                 'origin': value['origin']}
                for key, value in self.repo.state['topic_registry']['definitions'].items() if value.get('active')]

    def _topic_units(self, key, units):
        definition = self.repo.state['topic_registry']['definitions'].get(key, {})
        return [unit for unit in units if unit['ref'] in definition.get('members', [])]

    def _topic_origins(self, units):
        origins, dates, visited = set(), set(), set()
        pending = []
        for unit in units:
            dates.update(time_scopes(unit['time'], self.config.repository.timezone)['days'])
            pending.extend(unit['facts'])
        while pending:
            ref = pending.pop()
            if ref in visited:
                continue
            visited.add(ref)
            record = self.repo.read(ref)
            if record['kind'] == 'detail':
                pending.extend(record['facts'])
            else:
                alias = Ref.parse(ref).alias
                imported = self.repo.catalog[alias]['metadata'].get('memulife', {})
                origins.add(json.dumps([imported.get('source_instance_id'), imported['object_id']])
                            if imported.get('object_id') else alias)
        return len(origins), len(dates)

    def _reconcile_topics(self):
        """Run under the repository transaction; retire derived views, never edit Units."""
        registry = self.repo.state['topic_registry']
        units = {unit['ref']: unit for unit in self.repo.iter_blocks(['unit'])}
        registry['seen'] = {ref: value for ref, value in registry['seen'].items() if ref in units}
        definitions = registry['definitions']
        for key, topic in list(definitions.items()):
            topic['pinned_members'] = sorted(set(topic.get('pinned_members', [])) & units.keys())
            topic['members'] = sorted(set(topic['members']) & units.keys())
            if not topic['members'] and topic['origin'] != 'user':
                del definitions[key]
                continue
            sources, days = self._topic_origins([units[ref] for ref in topic['members']])
            topic.update(source_count=sources, day_count=days,
                active=topic['origin'] == 'user' or (sources >= self.config.views.topic_min_sources
                                                   and days >= self.config.views.topic_min_days))
            if topic['active']:
                signature = hashlib.sha256(json.dumps([topic['description'], topic['origin'],
                    [(ref, fingerprint(units[ref])) for ref in topic['members']]], ensure_ascii=False).encode()).hexdigest()
                ref = self.repo.view_ref('runtime', key)
                if topic.get('scheduled_signature') != signature or not ref or self.repo.read(ref).get('active') is False:
                    self._queue_view('runtime', key, rebuild=True, immediate=True)
                    self.repo.state['runtime_changed'][key] = time.time()
                    topic['scheduled_signature'] = signature
        active = {key for key, value in definitions.items() if value.get('active')}
        for row in self.repo.iter_blocks(['runtime']):
            if row['scope'] not in active:
                if row.get('active', True) or row['body']:
                    self.repo.write_view('runtime', row['scope'], [], {'active': False})
                self.repo.state['pending_views'].pop(json.dumps(['runtime', row['scope']], separators=(',', ':'), ensure_ascii=False), None)
                self.repo.state['runtime_changed'].pop(row['scope'], None)
        self._queue_view('memory', 'global', rebuild=True, immediate=True, blocked=True)
        self.repo.state['index_dirty'] = True

    def _discover_topics(self, *, force=False):
        with self.repo.transaction():
            registry = self.repo.state['topic_registry']
            job = registry['job']
            if job['state'] == 'waiting' and (job.get('retry_at') or 0) <= time.time():
                job['state'] = 'queued'
            if job['state'] != 'queued' or (not force and job['due'] > time.time()):
                return
            priority = getattr(self, '_view_recovery_refs', frozenset())
            if priority and not force:
                if any(view['ref'] in priority and view['state'] in {'queued', 'running', 'waiting'}
                       for view in self.repo.state['pending_views'].values()):
                    job['due'] = time.time() + 150
                    self.repo.save_state()
                    return
                self._view_recovery_refs = frozenset()
            generation, epoch = job['generation'], self.repo.state['deletion_epoch']
            job['state'] = 'running'
            job['attempts'] = job.get('attempts', 0) + 1
            units = self.repo.iter_blocks(['unit'])
            pending = [unit for unit in units if registry['seen'].get(unit['ref']) != fingerprint(unit)]
            self.repo.save_state()
        try:
            # Each successful batch has its own checkpoint. A retry resumes only unseen Units.
            while pending:
                with self.repo.lock():
                    definitions = self.repo.state['topic_registry']['definitions']
                    context = [{'key': key, 'description': value['description'], 'origin': value['origin']}
                               for key, value in definitions.items()]
                context_chars = len(json.dumps(context, ensure_ascii=False))
                limit = self.config.build.task_batch_chars - context_chars - 512
                if limit < 100:
                    raise ValueError('Topic definitions exceed the task context budget')
                batch = next(self._batches(pending, limit=limit, max_items=32))
                # Whole Unit entries are the evidence boundary; never classify a body fragment as a full Unit.
                refs = [item['ref'] for item in batch]
                result = self._task('topics', {'items': batch, 'topics': context}, {'refs': refs}, refs)
                with self.repo.transaction():
                    registry = self.repo.state['topic_registry']
                    if registry['job']['generation'] != generation or self.repo.state['deletion_epoch'] != epoch:
                        registry['job']['state'] = 'queued'
                        self.repo.save_state()
                        return
                    for topic in registry['definitions'].values():
                        topic['members'] = [ref for ref in topic['members']
                                            if ref not in refs or ref in topic.get('pinned_members', [])]
                    for proposal in result['topics']:
                        key = proposal['key']
                        topic = registry['definitions'].setdefault(key, {'origin': 'auto',
                            'description': proposal['description'], 'members': [], 'active': False})
                        topic['members'] = sorted(set(topic['members']) | set(proposal['facts']))
                    registry['seen'].update({item['ref']: fingerprint(item) for item in batch})
                    self.repo.save_state()
                pending = [unit for unit in pending if unit['ref'] not in refs]
            with self.repo.transaction():
                registry = self.repo.state['topic_registry']
                if registry['job']['generation'] != generation or self.repo.state['deletion_epoch'] != epoch:
                    registry['job']['state'] = 'queued'
                else:
                    self._reconcile_topics()
                    registry['job'].update(state='done')
                    registry['job'].pop('error', None)
                self.repo.save_state()
        except Exception as error:
            with self.repo.transaction():
                job = self.repo.state['topic_registry']['job']
                job.update(failure(error, job.get('attempts', 1), time.time()))
                if job['generation'] != generation:
                    job.update(state='queued', attempts=0)
                event('topics_deferred', error_code=error_code(error), status=job['state'])
                self.repo.save_state()

    def _set_topic(self, key, description):
        if not isinstance(key, str) or len(key.strip()) > 80 or not isinstance(description, str) or len(description) > 1200:
            raise ValueError('A topic needs a name (up to 80 characters) and scope (up to 1200 characters)')
        key = UnitDraft.safe_topics([key.strip()])[0]
        with self.repo.transaction():
            definitions = self.repo.state['topic_registry']['definitions']
            topic = definitions.setdefault(key, {'members': []})
            topic.update(description=description.strip() or key, origin='user', active=True)
            self._queue_view('runtime', key, rebuild=True, immediate=True, blocked=True)
            self._queue_view('memory', 'global', rebuild=True, immediate=True, blocked=True)
            self._queue_topics(immediate=True, rescan=True)
            self.repo.save_state()
        self.changed.set()
        return {'key': key, 'status': 'queued'}

    def _list_topics(self):
        with self.repo.lock():
            registry = self.repo.state['topic_registry']
            rows = []
            for key, topic in registry['definitions'].items():
                if not topic.get('active'):
                    continue
                ref = self.repo.view_ref('runtime', key)
                row = self.repo.read(ref) if ref else {}
                pending = self._pending(ref) if ref else None
                from .revision_storage import visible_record
                row = visible_record(row, pending) or {}
                rows.append({'key': key, 'description': topic['description'], 'origin': topic['origin'],
                    'unit_count': len(topic['members']), 'ref': ref,
                    'body': row.get('body', ''), 'protected_count': row.get('protected_count', 0),
                    'state': pending['state'] if pending else 'done'})
            return {'topics': rows, 'discovery': deepcopy(registry['job'])}
