"""Compare-and-swap revisions submitted by an authorized external model.

No inference runs here. Evidence, history, pins and maintenance admission share
the repository's journalled transaction with the authoritative content.
"""
from copy import deepcopy
import hashlib
import json
import time
import uuid

from .refs import Ref
from .revision_storage import entry_line, EDITABLE_VIEWS
from .types import UnitDraft


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def snapshot(value):
    return {key: deepcopy(value[key]) for key in ('body', 'facts', 'relations', 'topics', 'time',
        'source', 'entry_id', 'protected', 'edit_revision') if key in value}


class MemoryRevisions:
    def _revise(self, actor, arguments):
        payload = arguments.model_dump(by_alias=True)
        fingerprint = digest(payload)
        key = digest([actor, arguments.idempotency_key])
        with self._write_lock, self.repo.transaction():
            batches = self.repo.state.setdefault('edit_batches', {})
            previous = batches.get(key)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    return {'status': 'conflict', 'code': 'idempotency_conflict',
                            'message': 'This key already belongs to a different revision'}
                return deepcopy(previous['result'])
            prepared, conflicts, seen = [], [], set()
            for change in arguments.changes:
                record = self.repo.read(change.ref)
                view = record['kind'] == 'view' and record.get('view') in EDITABLE_VIEWS
                if record['kind'] != 'unit' and not view:
                    raise ValueError('Only Unit, day/month Summary and Runtime entries can be revised')
                ref = record['ref']
                identity = (ref, change.entry_id)
                if identity in seen:
                    raise ValueError('A batch may revise each target only once')
                seen.add(identity)
                append = view and change.entry_id is None
                if view:
                    if append and record['view'] != 'runtime':
                        raise ValueError('Summary revisions require entry_id')
                    if change.add_relations or change.add_topics:
                        raise ValueError('Summary and Runtime keep their scope; use add_refs for Unit evidence')
                    before = (record if append else next((entry for entry in record['entries']
                              if entry['entry_id'] == change.entry_id), None))
                    if before is None:
                        conflicts.append({'ref': ref, 'entry_id': change.entry_id,
                            'code': 'entry_missing', 'current_entry_ids': [e['entry_id'] for e in record['entries']]})
                        continue
                else:
                    if change.entry_id is not None:
                        raise ValueError('entry_id applies only to Summary and Runtime')
                    before = record
                code = None
                if before['edit_revision'] != change.expected_revision:
                    code = 'revision_changed'
                elif not append and before.get('protected') and change.protect is not False and not change.allow_protected:
                    code = 'entry_protected'
                elif append:
                    if change.old_text:
                        code = 'append_requires_empty_old_text'
                else:
                    start = before['body'].find(change.old_text) if change.old_text else -1
                    if start < 0:
                        code = 'old_text_missing'
                    elif before['body'].find(change.old_text, start + 1) >= 0:
                        code = 'old_text_ambiguous'
                if code:
                    conflicts.append({'ref': ref, 'entry_id': change.entry_id, 'code': code,
                                      'current': snapshot(before)})
                    continue
                after = ({'entry_id': uuid.uuid4().hex, 'body': change.new_text, 'facts': [],
                          'time': {'precision': 'unknown', 'value': None}} if append else snapshot(before))
                if not append:
                    after['body'] = before['body'][:start] + change.new_text + before['body'][start + len(change.old_text):]
                if not after['body'].strip() or len(after['body']) > 100_000 or '\x00' in after['body']:
                    raise ValueError('Resulting body must be nonblank, contain no NUL and be at most 100000 characters')
                for target in change.add_refs:
                    evidence = self.repo.read(target)
                    allowed = {'unit'} if view else {'source', 'detail', 'media'}
                    if evidence['kind'] not in allowed:
                        raise ValueError('Invalid evidence type for this target')
                    canonical = evidence['ref']
                    if canonical not in after['facts']:
                        after['facts'].append(canonical)
                after['protected'] = change.protect is not False
                if view:
                    if not after['facts']:
                        raise ValueError('Summary and Runtime need at least one Unit reference')
                else:
                    for relation in change.add_relations:
                        target = self.repo.read(relation.target)
                        if (target['kind'] != 'unit' or target['ref'] == ref or
                                relation.rel not in self.config.build.relation_vocab):
                            raise ValueError('Invalid Unit relation or relation vocabulary')
                        value = {'rel': relation.rel, 'target': target['ref']}
                        if value not in after['relations']:
                            after['relations'].append(value)
                    topics = UnitDraft.safe_topics(change.add_topics)
                    if any(len(topic) > 80 or topic != topic.strip() for topic in topics):
                        raise ValueError('Topic names must be trimmed and at most 80 characters')
                    after['topics'] = list(dict.fromkeys(after['topics'] + topics))
                prepared.append((change, record, None if append else before, after))
            if conflicts:
                return {'status': 'conflict', 'conflicts': conflicts}
            transaction_id = uuid.uuid4().hex
            heads = self.repo.state.setdefault('edit_heads', {})
            audit, view_updates = [], {}
            for change, record, before, after in prepared:
                ref = record['ref']
                target = ref if record['kind'] == 'unit' else ref + '/' + after['entry_id']
                prior = deepcopy(heads.get(target))
                heads[target] = {'batch_key': key, 'transaction_id': transaction_id}
                audit.append({'ref': ref, 'entry_id': after.get('entry_id'), 'previous': prior,
                              'before': snapshot(before) if before else None, 'after': snapshot(after)})
                if record['kind'] == 'unit':
                    pins = self.repo.state.setdefault('unit_pins', {})
                    if after['protected']:
                        pins[ref] = snapshot(after)
                    else:
                        pins.pop(ref, None)
                    lines = self.repo._bytes(record['path']).decode('utf-8').splitlines(keepends=True)
                    start, end = record['start_line'] - 1, record['end_line']
                    lines[start:end] = [entry_line(after)] + ['\n'] * (end - start - 1)
                    self.repo._transaction({record['path']: ''.join(lines)})
                    self.repo.state.setdefault('unit_index_invalidated', {})[ref] = self.repo.revision(ref)
                    self.repo.state['staged_vectors'].pop(ref, None)
                    for topic in change.add_topics:
                        self._pin_topic(topic, [ref])
                    self._mark_views(dict(after, ref=ref), rebuild=True, immediate=True, blocked=True)
                else:
                    entries = view_updates.setdefault(ref, deepcopy(record['entries']))
                    entries[:] = [entry for entry in entries if entry['entry_id'] != after['entry_id']]
                    entries.append(after)
                    namespace = 'runtime_pins' if record['view'] == 'runtime' else 'view_pins'
                    by_view = self.repo.state.setdefault(namespace, {})
                    by_view.setdefault(ref, {})
                    # SQL RowMap copies assigned values; mutate its tracked
                    # row, not the detached default returned by setdefault.
                    pins = by_view[ref]
                    if after['protected']:
                        pins[after['entry_id']] = deepcopy(after)
                    else:
                        pins.pop(after['entry_id'], None)
                    # An explicitly curated Runtime keeps its topic entrance.
                    if record['view'] == 'runtime':
                        self._pin_topic(record['scope'], [fact for fact in after['facts']
                                                         if self.repo._is_live_unit(fact)])
            for ref, entries in view_updates.items():
                record = self.repo.read(ref)
                self.repo.write_view(record['view'], record['scope'], entries, {'active': True})
                # In-flight rollups must not publish over this revision. A new
                # rebuild operates only on the unprotected part.
                self._queue_view(record['view'], record['scope'], rebuild=True, immediate=True)
                if record['view'] == 'day':
                    self._queue_view('month', str(record['scope'])[:7], rebuild=True, immediate=True, blocked=True)
            if any(record['kind'] == 'unit' for _, record, _, _ in prepared):
                self._queue_topics(immediate=True)
            self._queue_view('memory', 'global', rebuild=True, immediate=True, blocked=True)
            self.repo.state['index_dirty'] = True
            self.repo.state.pop('index_retry_at', None)
            changed = []
            for item in audit:
                current = self.repo.read(item['ref'])
                if item['entry_id']:
                    current = next(entry for entry in current['entries'] if entry['entry_id'] == item['entry_id'])
                item['after'] = snapshot(current)
                changed.append({'ref': item['ref'], 'entry_id': item['entry_id'],
                                'edit_revision': current['edit_revision']})
            result = {'status': 'done', 'transaction_id': transaction_id, 'changed': changed,
                      'index': 'queued', 'views': 'queued'}
            batches[key] = {'transaction_id': transaction_id, 'fingerprint': fingerprint, 'actor': actor,
                            'reason': arguments.reason, 'at': time.time(), 'changes': audit, 'result': result}
            self.repo.save_state()
        self.changed.set()
        return result

    def _pin_topic(self, name, refs):
        topic = self.repo.state['topic_registry']['definitions'].setdefault(name,
                    {'description': name, 'members': []})
        topic.update(origin='user', active=True)
        topic['pinned_members'] = sorted(set(topic.get('pinned_members', [])) | set(refs))
        topic['members'] = sorted(set(topic['members']) | set(refs))

    def _revision_history(self, refs, entry_id, budget, cursor, actor):
        records, version = [], []
        if entry_id and len(refs) != 1:
            raise ValueError('entry_id requires exactly one Summary or Runtime reference')
        for ref in refs:
            ref = Ref.parse(ref).canonical
            targets = [ref + '/' + entry_id] if entry_id else [target for target in self.repo.state.get('edit_heads', {})
                                                             if target == ref or target.startswith(ref + '/')]
            for target in targets:
                head = self.repo.state.get('edit_heads', {}).get(target)
                version.append(head)
                while head:
                    batch = self.repo.state['edit_batches'][head['batch_key']]
                    change = next(item for item in batch['changes']
                                  if item['ref'] + ('/' + item['entry_id'] if item['entry_id'] else '') == target)
                    value = {field: batch[field] for field in ('transaction_id', 'actor', 'reason', 'at')}
                    value.update({field: change[field] for field in ('ref', 'entry_id', 'before', 'after')})
                    records.append({'ref': ref, 'entry_id': change['entry_id'], 'kind': 'revision',
                                    'body': json.dumps(value, ensure_ascii=False)})
                    head = change['previous']
        return self._page(records, limit=100, budget=budget, cursor=cursor,
                          signature=digest([actor, refs, entry_id, version]))
