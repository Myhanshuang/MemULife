"""Durable source admission and checkpointed model work."""
import hashlib
import json
import re
import time as clock
import uuid
from copy import deepcopy

from . import compaction
from .batching import text_portions, split_batch
from .errors import IndexNotReady, RequestConflict, RevisionConflict
from .events import event, event_context
from .media import decode_media, probe_duration
from .models import OutputTruncated
from .refs import Ref
from .retry import failure
from .types import Time
from .projections import projection_identity
from .source_policy import audit_without_content, has_preserved_attachment


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class Ingestion:
    def memory_write(self, *, from_, text="", time=None, media=None, metadata=None, idempotency_key=None,
                     mode='ingest', changes=None, reason=''):
        self._authorize(from_, 'write')
        if mode == 'revise':
            from .operations import WriteInput
            arguments = WriteInput.model_validate({'from': from_, 'mode': mode, 'changes': changes or [],
                'reason': reason, 'idempotency_key': idempotency_key, 'text': text,
                'time': time, 'media': media or [], 'metadata': metadata or {}})
            return self._revise(from_, arguments)
        if mode != 'ingest' or changes or reason:
            raise ValueError('Invalid memory_write mode or revision arguments')
        media = media or []
        source_time = Time.model_validate(time or {'precision': 'unknown', 'value': None}).model_dump(by_alias=True)
        payload = {'source': from_, 'text': text, 'time': source_time, 'media': media, 'metadata': metadata or {}}
        fingerprint = _hash(payload)
        projection_key = projection_identity(from_, metadata or {})
        key = projection_key or (_hash([from_, idempotency_key]) if idempotency_key else uuid.uuid4().hex)
        filtered = '\n'.join('' if any(re.search(term, line) for term in self.config.build.blacklist) else line
                             for line in text.splitlines()).replace('\x00', '\\u0000')
        content = (metadata or {}).get('content', {})
        if audit_without_content(metadata or {}, media=bool(media)):
            # Retain the audit receipt, without creating a meaningless Source.
            filtered = ''
        elif content.get('audit_only') and not filtered.strip() and not media and has_preserved_attachment(metadata or {}):
            filtered = '# 附件'
        decoded = [(item, decode_media(item)) for item in media]
        prepared_media = [(item, data, probe_duration(data, item['mime_type'])) for item, data in decoded]
        # Admission does not wait for inference or the worker's write lock.
        with self.repo.transaction():
            existing = self.repo.state['writes'].get(key)
            if existing:
                if projection_key:
                    active = self.repo.state.get('projection_updates', {}).get(key, existing)
                    previous = active.get('metadata', {}).get('projection', {})
                    incoming = metadata['projection']
                    if incoming['generation'] < previous.get('generation', 0) or incoming['version'] == previous.get('version'):
                        return self._current_result(existing)
                    if incoming['generation'] <= previous.get('generation', 0):
                        raise RequestConflict('Projection version changes must increase generation')
                    if key in self.repo.state.get('projection_updates', {}):
                        raise RequestConflict('Another projection replacement is pending')
                elif existing['fingerprint'] != fingerprint:
                    raise RequestConflict('This idempotency key belongs to different material')
                else:
                    return self._current_result(existing)
            info = (self.repo.prepare_metadata(metadata or {}) if hasattr(self.repo, 'prepare_metadata')
                    else deepcopy(metadata or {}))
            refs = []
            if filtered.strip():
                refs.append(self.repo.save_source(filtered, from_, source_time, info))
            for item, data, duration in prepared_media:
                refs.append(self.repo.save_media(data, from_, source_time, item['mime_type'],
                    filename=item.get('filename'), duration=duration, metadata=info))
            if not refs and not info.get('content', {}).get('audit_only'):
                raise ValueError('Write requires nonempty source material')
            request = {
                'request_id': key, 'state': 'queued', 'stage': 'accepted', 'fingerprint': fingerprint,
                'source': from_, 'time': source_time, 'metadata': info, 'result': {},
                'compact': compaction.settings(self.config), 'source_refs': refs,
                'epoch': self.repo.state['deletion_epoch'], 'created_at': clock.time(), 'attempts': 0,
                'input_revisions': {Ref.parse(ref).alias: self.repo.revision(Ref.parse(ref).alias) for ref in refs},
            }
            if existing and projection_key:
                self.repo.state.setdefault('projection_updates', {})[key] = request
                for ref in refs:
                    self.repo.catalog[Ref.parse(ref).alias]['metadata'].setdefault('content', {})['pending_projection'] = True
            else:
                self.repo.state['writes'][key] = request
            self.repo.state['last_source_admission'] = clock.time()
            self.repo.save_state()
        self.changed.set()
        event('write_accepted', request_id=key, stage='accepted', count=len(refs))
        return {**self._current_result(self.repo.state['writes'][key]), 'status': 'accepted'}

    def process_write(self, key, *, defer_index=False):
        with self._write_lock:
            if key in self._active_writes:
                return self._current_result(self.repo.state['writes'][key])
            self._active_writes.add(key)
            previous = getattr(self._write_context, 'request_id', None)
            previous_defer = getattr(self._write_context, 'defer_index', False)
            self._write_context.request_id = key
            self._write_context.defer_index = defer_index
            try:
                return self._process_write(key)
            finally:
                self._active_writes.remove(key)
                self._write_context.request_id = previous
                self._write_context.defer_index = previous_defer
                self.changed.set()

    def _process_write(self, key):
        with self._write_lock, event_context(request_id=key):
            with self.repo.lock():
                request = self.repo.state['writes'][key]
                if request['state'] not in {'queued', 'running', 'waiting'}:
                    return self._current_result(request)
                attempt = request.get('attempts', 0) + 1
                request.update(state='running', attempts=attempt, retry_at=None)
                self.repo.save_state()
            started = clock.monotonic()
            try:
                if not request.get('metadata', {}).get('content', {}).get('audit_only'):
                    self._require_models()
                result = self._write_pipeline(key)
                with self.repo.lock():
                    request = self.repo.state['writes'][key]
                    if request['state'] == 'done':
                        self._compact_write(request)
                        self.repo.save_state()
                event('write_done', request_id=key, elapsed_ms=round((clock.monotonic()-started)*1000))
                return result
            except Exception as error:
                with self.repo.lock():
                    request = self.repo.state['writes'][key]
                    if request['state'] == 'done':
                        # Source/Units committed; index repair must not replay models.
                        self._compact_write(request)
                        self.repo.state['index_retry_at'] = clock.time() + 150
                    else:
                        request.update(failure(error, attempt, clock.time()))
                        if getattr(error, 'reason', None):
                            request['validation_reason'] = error.reason
                    self.repo.save_state()
                    event('write_deferred', request_id=key, stage=request.get('stage'),
                          error_code=getattr(error, 'code', type(error).__name__), status=request['state'])
                    return self._current_result(request)

    @staticmethod
    def _compact_write(request):
        for field in ('drafts', 'processed', 'digests', 'digest_refs', 'compact', 'input_revisions'):
            request.pop(field, None)
        request['stage'] = 'committed'

    def _write_pipeline(self, key, text="", media=None):
        request = self.repo.state["writes"][key]
        source, source_time = request["source"], request["time"]
        if "source_refs" not in request:
            # Filter before Source persistence. Blank excluded lines, do not join unrelated ranges.
            blacklist = self.config.build.blacklist
            filtered = "\n".join("" if any(re.search(term, line) for term in blacklist) else line
                                 for line in text.splitlines())
            decoded = [(item, decode_media(item)) for item in (media or [])]
            for item, data in decoded:
                kind = item["mime_type"].split("/")[0]
                if kind not in self.embedder.capabilities["modalities"]:
                    raise ValueError(f"Embedding model does not support {kind}")
            with self.repo.transaction():
                request = self.repo.state["writes"][key]
                refs = []
                if filtered.strip():
                    refs.append(self.repo.save_source(filtered, source, source_time, request["metadata"]))
                for item, data in decoded:
                    refs.append(self.repo.save_media(data, source, source_time, item["mime_type"],
                        filename=item.get("filename"), duration=probe_duration(data, item["mime_type"]),
                        metadata=request["metadata"]))
                request["source_refs"] = refs
                request["epoch"] = self.repo.state["deletion_epoch"]
                request["input_revisions"] = {Ref.parse(ref).alias: self.repo.revision(Ref.parse(ref).alias) for ref in refs}
                self.repo.save_state()
        request = self.repo.state["writes"][key]
        refs = request["source_refs"]
        if request.get('metadata', {}).get('content', {}).get('audit_only'):
            self._state_write(key, state='done', result={'units': [], 'views': [], 'audit_only': True})
            if refs and not getattr(self._write_context, 'defer_index', False):
                self.sync_index()
            return self._current_result(self.repo.state['writes'][key])
        if not refs:
            self._state_write(key, state="done", result={"units": [], "views": [], "filtered": True})
            return self._current_result(self.repo.state["writes"][key])
        records = [self.repo.read(ref) for ref in refs]
        if "compact" in request and "digest_refs" not in request:
            batches = []
            for record in records:
                if record['kind'] == 'media':
                    batches.extend([{'ref': ref, 'media': item}] for ref, item in self._media_items(record['ref']))
                elif len(record['body']) > request['compact']['digest_threshold_chars']:
                    portions = self._text_portions(record, limit=request['compact']['input_chars'])
                    if request['metadata'].get('content', {}).get('evidence_contract') == 'local-ai-v1':
                        batches.extend(self._batches(portions, limit=request['compact']['input_chars'], max_items=8))
                    else:
                        batches.extend([portion] for portion in portions)
            stage = self._compact_stage(key, 'digest', batches)
            self._state_write(key, digest_refs=list(dict.fromkeys(
                ref for record in stage['ledger'] for ref in record['refs'])))
            request = self.repo.state['writes'][key]
        if "digest_refs" not in request:
            digest_refs = []
            for record in records:
                ref = record["ref"]
                if record["kind"] == "media":
                    portions = [{"ref": item_ref, "media": item} for item_ref, item in self._media_items(ref)]
                elif len(record["body"]) > getattr(self.config.build, "digest_threshold_chars", 12000):
                    portions = self._text_portions(record)
                else:
                    continue
                for portion in portions:
                    # Retain intermediate commits by portion, allowing restart without duplicate summaries.
                    cache_key = (_hash([portion["ref"], portion["source_slice"], portion["body"]])
                                 if "source_slice" in portion else _hash(portion["ref"]))
                    cached = request.get("digests", {}).get(cache_key)
                    if cached:
                        digest_refs.extend(cached)
                        continue
                    output = self._task("digest", {"source": source, "time": source_time, "items": [portion]},
                                        {"refs": [portion["ref"]]}, [portion["ref"]])
                    with self.repo.transaction():
                        request = self.repo.state["writes"][key]
                        self._check_epoch(request)
                        appended = self.repo.append_entries("detail", output["entries"], source)
                        digest_refs.extend(appended)
                        request.setdefault("digests", {})[cache_key] = appended
                        self.repo.save_state()
            self._state_write(key, digest_refs=digest_refs)
        request = self.repo.state["writes"][key]
        if "compact" in request and "drafts" not in request:
            entries = [self.repo.read(ref) for ref in refs + request['digest_refs']]
            batches = list(self._unit_batches(entries, limit=request['compact']['input_chars'],
                                              max_items=64 if request['metadata'].get('content', {}).get('unit_limit') else request['compact']['unit_items'],
                                              digest_threshold=request['compact']['digest_threshold_chars']))
            stage = self._compact_stage(key, 'unit', batches)
            drafts = [record['entry'] for record in stage['ledger']]
            limit = request['metadata'].get('content', {}).get('unit_limit')
            reduction = 0
            while limit and len(drafts) > limit:
                items = [{'ref': entry.get('anchor') or entry['facts'][0], 'body': entry['body'],
                          'time': entry['time'], 'provenance': entry.get('provenance', {}),
                          'supporting_refs': entry['facts']} for entry in drafts]
                batches = list(self._batches(items, limit=request['compact']['input_chars']))
                if len(batches) >= len(drafts):
                    raise ValueError('Conversation reduction cannot fit two summaries in the task window')
                reduced = self._compact_stage(key, 'unit', batches, stage_name=f'unit_reduce_{reduction}')
                drafts = [record['entry'] for record in reduced['ledger']]
                reduction += 1
            self._state_write(key, drafts=drafts, processed=[])
            request = self.repo.state['writes'][key]
        if "drafts" not in request:
            allowed = refs + request["digest_refs"]
            entries = [self.repo.read(ref) for ref in allowed]
            drafts = []
            for batch in self._unit_batches(entries):
                scope_refs = [item["ref"] for item in batch]
                output = self._task("unit", {"source": source, "time": source_time, "items": batch,
                                            "topics": self._topics()}, {"refs": scope_refs}, scope_refs)
                for draft in output["units"]:
                    draft["topics"] = self._canonical_topics(draft["topics"])
                    if request["metadata"].get("operation") == "update" and request["metadata"].get("view") == "runtime":
                        target_topic = request["metadata"].get("scope")
                        if target_topic and target_topic not in draft["topics"]:
                            draft["topics"].append(target_topic)
                    drafts.append(draft)
            self._state_write(key, drafts=drafts, processed=[])
        request = self.repo.state["writes"][key]
        for position, draft in enumerate(request["drafts"]):
            if position < len(request["processed"]):
                continue
            query_items = [{"kind": "text", "text": draft["body"]}]
            media_refs = []
            for fact in draft["facts"]:
                if Ref.parse(fact).kind == "a":
                    for ref, item in self._media_items(fact):
                        media_refs.append(ref)
                        query_items.append(item)
            vectors = self._embed(query_items)
            if request['metadata'].get('content', {}).get('sequential_relations'):
                with self.repo.lock():
                    request = self.repo.state['writes'][key]
                    request['processed'].append({'draft': draft, 'relations': [], 'vector': vectors[0],
                                                'media_vectors': dict(zip(media_refs, vectors[1:])), 'candidates': {}})
                    self.repo.save_state()
                continue
            # Every draft performs this call, including an empty repository/candidate result.
            if self.repo.state['index_dirty']:
                try:
                    self.sync_index()
                except Exception as error:
                    raise IndexNotReady('Relation retrieval is waiting for index recovery') from error
                self._ensure_index()
            candidates = self.index.search(vectors, limit=self.config.build.relation_candidates)
            candidate_blocks = {c['ref']: self._candidate_fingerprint(c) for c in candidates}
            output = self._task("relate", {"draft": draft, "candidates": candidates},
                                {"refs": draft["facts"] + [c["ref"] for c in candidates]},
                                draft["facts"] + [c["ref"] for c in candidates],
                                candidates=[c["ref"] for c in candidates])
            candidate_revisions = {Ref.parse(c["ref"]).alias: c["revision"] for c in candidates}
            processed = {"draft": draft, "relations": output["relations"], "vector": vectors[0],
                         "media_vectors": dict(zip(media_refs, vectors[1:])), "candidates": candidate_revisions,
                         "candidate_blocks": candidate_blocks}
            with self.repo.lock():
                request = self.repo.state["writes"][key]
                request["processed"].append(processed)
                self.repo.save_state()
        with self.repo.transaction():
            request = self.repo.state["writes"][key]
            if request["state"] == "done":
                return self._current_result(request)
            self._check_epoch(request)
            for ref in refs + request["digest_refs"]:
                self.repo.validate_ref(ref)
            for entry in request["processed"]:
                if 'candidate_blocks' in entry:
                    if any(self._candidate_fingerprint(self.repo.read(ref)) != fingerprint
                           for ref, fingerprint in entry['candidate_blocks'].items()):
                        raise RevisionConflict('Relation candidate content changed during inference')
                    continue
                for alias, revision in entry["candidates"].items():
                    if self.repo.revision(alias) != revision:
                        raise RevisionConflict("Relation candidates changed; retry this write explicitly")
            entries = [dict(p["draft"], relations=p["relations"]) for p in request["processed"]]
            addresses = self.repo.append_entries("unit", entries, source)
            if request['metadata'].get('content', {}).get('sequential_relations'):
                self._link_conversation(request, addresses)
            affected = set()
            for address, entry in zip(addresses, request["processed"]):
                if entry['draft'].get('provenance'):
                    self.repo.state.setdefault('worklog_evidence', {})[Ref.parse(address).canonical] = entry['draft']['provenance']
                self.repo.state["staged_vectors"][address] = entry["vector"]
                self.repo.state["staged_vectors"].update(entry["media_vectors"])
                affected.update(self._mark_views(entry["draft"]))
            self.repo.state["index_dirty"] = True
            request.update(state="done", result={"units": addresses, "views": sorted(affected)})
            self._queue_topics()
            self.repo.save_state()
        self.changed.set()
        if not getattr(self._write_context, 'defer_index', False):
            self.sync_index()
        return self._current_result(self.repo.state["writes"][key])

    @staticmethod
    def _candidate_fingerprint(record):
        return _hash({name: record.get(name, [] if name in {'facts', 'relations', 'topics'} else None)
                      for name in ('body', 'time', 'source', 'facts', 'relations', 'topics')})

    def _check_epoch(self, request):
        if request["epoch"] != self.repo.state["deletion_epoch"]:
            raise RevisionConflict("Deletion occurred during this task; stale output cannot be committed")
        if any(self.repo.revision(alias) != revision for alias, revision in request.get("input_revisions", {}).items()):
            raise RevisionConflict("Source revisions changed during this task")

    def _compact_stage(self, key, task, batches, *, stage_name=None):
        """Commit each delta, coverage and replacement memory together before advancing."""
        plan = compaction.stage_plan(batches)
        stage_name = stage_name or task
        with self.repo.lock():
            request = self.repo.state['writes'][key]
            if request['compact']['version'] != 1:
                raise ValueError('Unsupported compact checkpoint version')
            stages = request['compact']['stages']
            if stage_name not in stages:
                stages[stage_name] = compaction.new_stage(plan)
                self.repo.save_state()
            elif stages[stage_name]['plan'] != plan:
                if not stages[stage_name]['next'] and not stages[stage_name]['ledger']:
                    # Nothing was committed from this stage; earlier completed
                    # stages remain intact when preparation is corrected.
                    stages[stage_name] = compaction.new_stage(plan)
                else:
                    raise RevisionConflict('Compact input plan changed; cannot reuse its completed prefix')
            stages[stage_name].setdefault('steps', [{'batch': index, 'path': ''} for index in range(len(batches))])
            self.repo.save_state()
        while True:
            request = self.repo.state['writes'][key]
            stage = request['compact']['stages'][stage_name]
            position = stage['next']
            if position == len(stage['steps']):
                return stage
            self._check_epoch(request)
            step = stage['steps'][position]
            batch = batches[step['batch']]
            for branch in step['path']:
                batch = split_batch(batch)[int(branch)]
            context = compaction.context_for(stage, task, batch, request['compact']['context_chars'])
            current = [{name: value for name, value in item.items() if name != 'media'} for item in batch]
            if compaction.size({'items': current, 'context': context}) > (
                    request['compact']['input_chars'] + request['compact']['context_chars'] + 32):
                raise ValueError('Current material and compact memory exceed the task window')
            allowed = compaction.evidence_refs(batch, context)
            material = {'source': request['source'], 'time': request['time'], 'items': batch, 'context': context}
            if request['metadata'].get('content', {}).get('material_kind'):
                material['material_kind'] = request['metadata']['content']['material_kind']
            if request['metadata'].get('content', {}).get('evidence_contract'):
                material['evidence_contract'] = request['metadata']['content']['evidence_contract']
                material['time'] = {'precision': 'unknown', 'value': None}
            if request['metadata'].get('content', {}).get('unit_limit'):
                material['unit_limit'] = request['metadata']['content']['unit_limit']
            if task == 'unit':
                material['topics'] = self._topics()
            self._state_write(key, stage=task)
            try:
                output = self._task(task, material, {'refs': allowed}, allowed)
            except OutputTruncated:
                if len(step['path']) >= self.config.build.max_split_depth:
                    raise
                split_batch(batch)  # Fail explicitly if no legal smaller evidence boundary exists.
                with self.repo.lock():
                    active = self.repo.state['writes'][key]['compact']['stages'][stage_name]
                    active['steps'][position:position + 1] = [
                        {'batch': step['batch'], 'path': step['path'] + branch} for branch in ('0', '1')]
                    self.repo.save_state()
                event('model_batch_split', request_id=key, task=task, count=len(batch))
                continue
            if task == 'unit':
                for draft in output['units']:
                    draft['topics'] = self._canonical_topics(draft['topics'])
                    if request['metadata'].get('operation') == 'update' and request['metadata'].get('view') == 'runtime':
                        target = request['metadata'].get('scope')
                        if target and target not in draft['topics']:
                            draft['topics'].append(target)
            updated, added = compaction.advance(stage, task, batch, output)
            with self.repo.transaction():
                request = self.repo.state['writes'][key]
                self._check_epoch(request)
                if request['compact']['stages'][stage_name]['next'] != position:
                    raise RevisionConflict('Compact stage advanced during model execution')
                if task == 'digest' and added:
                    refs = self.repo.append_entries('detail', [record['entry'] for record in added], request['source'])
                    for record, ref in zip(added, refs):
                        record['refs'] = [ref]
                        if record['entry'].get('provenance'):
                            self.repo.state.setdefault('worklog_evidence', {})[Ref.parse(ref).canonical] = record['entry']['provenance']
                if task == 'digest':
                    by_id = {record['id']: record['refs'] for record in updated['ledger']}
                    for item, covered in zip(batch, updated['coverage'][-1]['items']):
                        cache_key = (_hash([item['ref'], item['source_slice'], item['body']])
                                     if 'source_slice' in item else _hash(item['ref']))
                        request.setdefault('digests', {})[cache_key] = list(dict.fromkeys(
                            ref for identity in covered['records'] for ref in by_id[identity]))
                request['compact']['stages'][stage_name] = updated
                self.repo.save_state()

    def _text_portions(self, record, *, limit=None):
        ref = Ref.parse(record["ref"])
        if ref.kind in {"u", "r"}:
            return [record]
        metadata = self.repo.catalog.get(ref.alias, {}).get('metadata', {}) if ref.kind == 'c' else {}
        if hasattr(self.repo, 'full_metadata'):
            metadata = self.repo.full_metadata(metadata)
        content = metadata.get('content', {})
        messages = content.get('message_layout') or metadata.get('memulife', {}).get('message_layout')
        if content.get('evidence_contract') == 'local-ai-v1':
            # Batch several precise observations, rather than merging their
            # citations and timestamps into one undifferentiated Source range.
            rows = record['body'].splitlines(keepends=True)
            portions = []
            for message in messages or []:
                first, last = max(ref.start, message['start_line']), min(ref.end, message['end_line'])
                if first > last:
                    continue
                part = {**record, 'ref': f'{ref.alias}:{first}-{last}',
                        'body': ''.join(rows[first-ref.start:last-ref.start+1]).rstrip('\n')}
                provenance = self._observation(message)
                extra = {'provenance': provenance, 'time': self._observation_time(provenance)}
                for portion in text_portions(part, (limit or self.config.build.task_batch_chars) - len(_json(extra)) - 8):
                    portions.append({**portion, **extra})
            return portions
        if record.get('provenance'):
            extra = {'time': record['time'], 'provenance': record['provenance']}
            return [{**p, **extra} for p in text_portions(record,
                    (limit or self.config.build.task_batch_chars) - len(_json(extra)) - 8)]
        portions = text_portions(record, limit if limit is not None else self.config.build.task_batch_chars, messages)
        if content.get('material_kind') == 'agent_worklog':
            for portion in portions:
                portion['material_kind'] = 'agent_worklog'
        return portions

    @staticmethod
    def _observation(message):
        return {key: message.get(key) for key in ('role', 'event_kind', 'event_id', 'occurred_at',
                                                 'outcome', 'tool', 'tool_purpose', 'time_basis')}

    @staticmethod
    def _observation_time(provenance):
        stamp = provenance.get('occurred_at')
        return {'precision': 'instant', 'value': stamp} if stamp else {'precision': 'unknown', 'value': None}

    def _worklog_evidence(self, value):
        record = self.repo.read(value)
        ref = Ref.parse(value)
        if record['kind'] == 'source':
            meta = self.repo.catalog[ref.alias]['metadata']
            if hasattr(self.repo, 'full_metadata'):
                meta = self.repo.full_metadata(meta)
            matches = [row for row in meta.get('content', {}).get('message_layout', [])
                       if row['start_line'] <= ref.start and row['end_line'] >= ref.end]
            if len(matches) == 1:
                record['provenance'] = self._observation(matches[0])
        else:
            record['provenance'] = self.repo.state.setdefault('worklog_evidence', {}).get(ref.canonical)
        return record

    def _batches(self, items, *, max_items=None, limit=None):
        limit = self.config.build.task_batch_chars if limit is None else limit
        batch, size = [], 2
        for item in items:
            # Native media is governed by the encoder's media bounds, not a base64 character count.
            count = len(_json({k: v for k, v in item.items() if k != "media"}))
            if count + 2 > limit:
                raise ValueError("One material record exceeds the task budget; refine its stable source/digest first")
            if batch and (size + 1 + count > limit or (max_items is not None and len(batch) >= max_items)):
                yield batch
                batch, size = [], 2
            size += int(bool(batch)) + count
            batch.append(item)
        if batch:
            yield batch

    def _unit_batches(self, records, *, limit=None, max_items=None, digest_threshold=None):
        """Keep one native segment, its caption and accompanying text in a task scope."""
        limit = self.config.build.task_batch_chars if limit is None else limit
        max_items = self.config.build.unit_batch_items if max_items is None else max_items
        digest_threshold = self.config.build.digest_threshold_chars if digest_threshold is None else digest_threshold
        media = [record for record in records if record["kind"] == "media"]
        details = [record for record in records if record["kind"] == "detail"]
        sources = [record for record in records if record["kind"] == "source"]
        short_sources = [record for record in sources if len(record["body"]) <= digest_threshold]
        used_details = set()
        for record in media:
            for ref, native in self._media_items(record["ref"]):
                captions = [detail for detail in details if any(self._overlaps(fact, ref) for fact in detail["facts"])]
                used_details.update(detail["ref"] for detail in captions)
                # Repository reads also contain text, headers and storage metadata.
                # Pass each body once, with its evidence and attribution fields.
                def context_record(item):
                    return {key: value for key, value in item.items()
                            if key in {'ref', 'kind', 'body', 'time', 'facts', 'source', 'provenance'}}
                context = [context_record(item) for item in captions]
                # Captions describe this same native segment; they are supporting
                # context, not independent observations requiring duplicate
                # coverage entries. Preserve all evidence locators for citation.
                batch = [{"ref": ref, "media": native,
                          "context": context,
                          "supporting_refs": list(dict.fromkeys(
                              evidence for item in context
                              for evidence in [item['ref'], *item.get('facts', [])]))}]
                # Accompanying text may contain an independent event. Keep its
                # coverage separate so it never needs an unrelated media citation.
                batch.extend(context_record(item) for item in short_sources)
                if compaction.size([{k: v for k, v in item.items() if k != 'media'} for item in batch]) > limit:
                    raise ValueError("Media caption/context exceeds task budget; reduce accompanying material")
                yield batch
        text_items = []
        for record in records:
            if record["kind"] == "media" or record["ref"] in used_details:
                continue
            if record["kind"] == "source":
                if media and record in short_sources:
                    continue
                if len(record["body"]) > digest_threshold:
                    continue
            text_items.extend(self._text_portions(record, limit=limit))
        # Many short digests can fit the input budget while producing much more
        # structured Unit output. Bound the number of admitted records as well.
        yield from self._batches(text_items, max_items=max_items, limit=limit)
