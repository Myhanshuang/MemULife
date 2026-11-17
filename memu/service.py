"""Memory operations with Markdown bodies and durable PostgreSQL control."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time as clock
import uuid
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo

from .errors import AccessDenied, IndexNotReady, ModelNotConfigured, RevisionConflict
from .media import decode_media, native_item, probe_duration
from .refs import Ref, merge_refs
from .tasks.contract import TASKS, TaskRunner
from .types import time_scopes
from .topics import TopicMaintenance
from .indexing import IndexMaintenance
from .ingestion import Ingestion
from .sequencing import ConversationSequence
from .projections import ProjectionReplacement
from .events import event
from .persistence import private_save
from .retry import failure, error_code
from .concurrency import WriteLock
from .browsing import MemoryBrowsing
from .revisions import MemoryRevisions
from .revision_storage import EDITABLE_VIEWS, pins_for


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class MemoryService(MemoryRevisions, MemoryBrowsing, TopicMaintenance, IndexMaintenance, Ingestion, ConversationSequence, ProjectionReplacement):
    def __init__(self, repository, config, model=None, embedder=None, index=None):
        self.repo = repository
        self.config = config
        self.runner = TaskRunner(model, config) if model is not None else None
        self.embedder = embedder
        self.index = index
        self._write_lock = WriteLock()
        self._index_lock = threading.RLock()
        self._active_writes = set()
        self._write_context = threading.local()
        self._mllm_slots = threading.BoundedSemaphore(config.models.mllm.concurrency)
        self._embedding_slots = threading.BoundedSemaphore(config.models.embedding.concurrency)
        self._maintenance_lock = threading.Lock()
        self.changed = threading.Event()
        self._media_cache = {}
        self._media_cache_lock = threading.Lock()
        with self.repo.lock():
            for key, value in {
                "writes": {}, "pending_views": {}, "deleted_refs": [], "deletion_epoch": 0,
                "index_dirty": True, "staged_vectors": {}, "runtime_changed": {},
            }.items():
                self.repo.state.setdefault(key, value)
            for task in self.repo.state["pending_views"].values():
                if task["state"] == "running":
                    task["state"] = "queued"
            self._init_topics()
            self.repo.save_state()

    def _authorize(self, source, permission="read"):
        self.config.authorize(source, permission)

    def _owner(self, source):
        self._authorize(source, "write")
        if source not in self.config.access.owners:
            raise AccessDenied("memory_manage requires an owner identity")

    def _require_models(self):
        if self.runner is None or self.embedder is None:
            raise ModelNotConfigured("Configure the MLLM and native embedding endpoints before writing")

    def _state_write(self, key, **fields):
        with self.repo.lock():
            self.repo.state["writes"][key].update(fields)
            self.repo.save_state()

    def _task(self, task, material, scope, allowed, *, mode="build", candidates=None):
        if self.runner is None:
            raise ModelNotConfigured("MLLM is not configured")
        if task == 'rollup' and scope.get('view') in {'runtime', 'memory'}:
            # A topic's question/reply Units form one overview item, including
            # candidates which are not yet admitted as Runtime pages.
            remaining, groups = set(allowed), []
            for topic in self.repo.state['topic_registry']['definitions'].values():
                members = set(topic['members']) & set(allowed)
                if members:
                    overlapping = [group for group in groups if group & members]
                    for group in overlapping:
                        groups.remove(group)
                        members |= group
                    groups.append(members)
                    remaining -= members
            group_count = max(1, len(groups) + len(remaining))
            scope = {**scope, 'max_entries': group_count}
            if group_count == 1:
                scope['entry_chars'] = {'day': 80, 'month': 60, 'memory': 80, 'runtime': self.config.views.per_topic_chars}[scope['view']]
        evidence = (self._worklog_evidence if isinstance(material, dict) and
                    material.get('evidence_contract') == 'local-ai-v1' else self.repo.read)
        def checked_evidence(ref):
            with self.repo.lock():
                self._check_active_inputs()
                record = evidence(ref)
                if task == 'rollup' and record.get('view') == 'runtime':
                    valid = [entry for entry in record['entries'] if not entry.get('invalid_refs')]
                    record = dict(record, entries=valid, body='\n'.join(entry['body'] for entry in valid),
                                  facts=list(dict.fromkeys(f for entry in valid for f in entry['facts'])))
                    record.pop('text', None)
                return record
        on_rejected = None
        if task == 'rollup':
            on_rejected = lambda error, attempt: self._save_view_proposal(error, attempt, scope, mode)
        elif task in {'digest', 'unit'}:
            on_rejected = lambda error, attempt: self._save_write_proposal(error, attempt, task, material, scope)
        # Release the model slot before reacquiring the write lock, so a commit
        # or index update cannot deadlock waiting for a model that needs that lock.
        with self._write_lock.model_work(), self._mllm_slots:
            self._check_active_inputs()
            result = self.runner.run(task, material, scope, mode=mode, allowed_refs=allowed,
                                     read_ref=checked_evidence, candidates=candidates or [],
                                     on_rejected=on_rejected)
        self._check_active_inputs()
        return result

    def _save_write_proposal(self, error, attempt, task, material, scope):
        """Retain a failed proposal privately; never send it to memory or logs."""
        try:
            directory = self.repo.root / '.meta' / 'write-proposals'
            directory.mkdir(mode=0o700, exist_ok=True)
            path = directory / (uuid.uuid4().hex + '.json')
            private_save(path, {'task': task, 'prompt_version': TASKS[task].prompt_version,
                               'recorded_at': clock.time(), 'attempt': attempt,
                               'input_refs': [item['ref'] for item in material.get('items', [])],
                               'scope': getattr(error, 'scope', scope),
                               'validation_reason': getattr(error, 'reason', 'output_contract'),
                               'validation_error': str(error), 'proposal': error.proposal})
            error.diagnostic = str(path.relative_to(self.repo.root))
            event('write_proposal_saved', request_id=path.stem, task=task,
                  validation_reason=getattr(error, 'reason', 'output_contract'))
        except (OSError, TypeError, ValueError):
            event('write_proposal_save_failed', task=task, error_code='diagnostic_io_error')

    def _save_view_proposal(self, error, attempt, scope, mode):
        """Keep rejected proposals private, outside memory bodies and operational logs."""
        try:
            directory = self.repo.root / '.meta' / 'view-proposals'
            directory.mkdir(mode=0o700, exist_ok=True)
            path = directory / (uuid.uuid4().hex + '.json')
            private_save(path, {'task': 'rollup', 'prompt_version': TASKS['rollup'].prompt_version,
                               'recorded_at': clock.time(), 'mode': mode, 'attempt': attempt,
                               'scope': getattr(error, 'scope', scope),
                               'validation_reason': getattr(error, 'reason', 'output_contract'),
                               'validation_error': str(error), 'proposal': error.proposal})
            error.diagnostic = str(path.relative_to(self.repo.root))
            event('view_proposal_saved', request_id=path.stem, stage=scope.get('view'),
                  validation_reason=getattr(error, 'reason', 'output_contract'))
        except (OSError, TypeError, ValueError):
            event('view_proposal_save_failed', stage=scope.get('view'), error_code='diagnostic_io_error')

    def _media_items(self, ref):
        parsed = Ref.parse(ref)
        record = self.repo.read(ref)
        cache_key = (ref, record['revision'])
        with self._media_cache_lock:
            cached = self._media_cache.get(cache_key)
        if cached is not None:
            return cached
        path = self.repo.root / record["path"]
        metadata = self.repo.catalog[parsed.alias].get("metadata", {})
        mime = record.get("mime_type") or metadata.get("mime_type")
        data = path.read_bytes()
        duration = record.get("duration") or metadata.get("duration")
        start = parsed.start_seconds
        end = parsed.end_seconds
        if mime.startswith("image/"):
            return self._cache_media(cache_key, [(ref, native_item(data, mime))])
        if duration is None:
            raise ValueError("Audio/video needs a verified duration")
        caps = self.embedder.capabilities
        bounds = caps.get("max_duration_seconds", {})
        maximum = bounds.get(mime.split("/")[0], 30) if isinstance(bounds, dict) else bounds
        maximum = float(maximum or 30)
        if maximum <= 0:
            raise ValueError("Embedding segment duration must be positive")
        position, finish = float(start or 0), float(end if end is not None else duration)
        intervals = [(position, finish)]
        for deleted in record.get("redacted_ranges", []):
            retained = []
            for left, right in intervals:
                if right <= deleted["start"] or left >= deleted["end"]:
                    retained.append((left, right))
                else:
                    if left < deleted["start"]:
                        retained.append((left, deleted["start"]))
                    if right > deleted["end"]:
                        retained.append((deleted["end"], right))
            intervals = retained
        result = []
        for position, finish in intervals:
            while position < finish:
                stop = min(finish, position + maximum)
                address = f"{parsed.alias}@{format(Decimal(str(position)), 'f')}-{format(Decimal(str(stop)), 'f')}"
                result.append((address, native_item(data, mime, position, stop)))
                position = stop
        return self._cache_media(cache_key, result)

    def _cache_media(self, key, values):
        size = sum(len(item.get('data_base64', '')) for _, item in values)
        if size <= 8 * 1024 * 1024:
            with self._media_cache_lock:
                if len(self._media_cache) >= 4:
                    self._media_cache.clear()
                self._media_cache[key] = values
        return values

    def _embed(self, items):
        if self.embedder is None:
            raise ModelNotConfigured("Embedding is not configured")
        with self._write_lock.model_work(), self._embedding_slots:
            self._check_active_inputs()
            result = self.embedder.embed(items)
        self._check_active_inputs()
        return result

    def _check_active_inputs(self):
        key = getattr(self._write_context, 'request_id', None)
        if key is not None:
            with self.repo.lock():
                self._check_epoch(self.repo.state['writes'][key])

    def _all_blocks(self):
        """Exclude tombstoned and suppressed views even while rebuilding the mirror."""
        blocks = self.repo.iter_blocks()
        with self.repo.lock():
            deleted = set(self.repo.state["deleted_refs"])
            suppressed = {
                item.get("ref") for item in self.repo.state["pending_views"].values()
                if item.get("blocked")
            }
        valid = {record["ref"] for record in blocks if record["ref"] not in deleted}
        result = []
        for record in blocks:
            if record["ref"] in deleted or record["ref"] in suppressed or not self._indexable(record):
                continue
            record = dict(record)
            record["relations"] = [rel for rel in record.get("relations", []) if rel["target"] in valid]
            result.append(record)
        return result

    def _ensure_index(self):
        if self.index is None or self.repo.state.get("index_dirty", True):
            raise IndexNotReady("Index is not ready; rebuild the index before searching")

    def _current_result(self, request):
        request = self.repo.state.get('projection_updates', {}).get(request.get('request_id'), request)
        result = dict(request.get("result", {}))
        result["request_id"] = request.get("request_id")
        result["sources"] = request.get("source_refs", [])
        result["status"] = request["state"]
        result['stage'] = request.get('stage', request['state'])
        result['attempts'] = request.get('attempts', 0)
        result['retry_at'] = request.get('retry_at')
        if request.get("error"):
            result['error'] = {'code': request.get('error_code', 'task_failed')}
        if result.get("views"):
            result['views'] = [{'ref': ref, 'status': (self._pending(ref) or {}).get('state', 'updated')}
                               for ref in request['result']['views']]
        result["deleted_units"] = [ref for ref in result.get("units", []) if ref in self.repo.state["deleted_refs"]]
        result["index_ready"] = not self.repo.state["index_dirty"]
        return result

    def _canonical_topics(self, topics):
        names = self._topics()
        result = []
        for topic in topics:
            matches = [entry["key"] for entry in names if topic == entry["key"] or topic in entry["aliases"]]
            if len(set(matches)) > 1:
                raise ValueError(f"Ambiguous topic alias {topic!r}; explicit canonical name required")
            canonical = matches[0] if matches else topic
            if matches and canonical not in result:
                result.append(canonical)
        return result

    def _queue_view(self, view, scope, *, rebuild=False, immediate=False, blocked=False):
        """Must be called inside a repository transaction/lock."""
        key = _json([view, scope])
        ref = self.repo.view_ref(view, scope, create=True)
        jobs = self.repo.state["pending_views"]
        now = clock.time()
        old = jobs.get(key)
        if old is None or old["state"] == "done":
            jobs[key] = {"view": view, "scope": scope, "ref": ref, "generation": 1,
                         "due": now if immediate else now + self.config.views.refresh_delay_seconds,
                         "state": "queued", "mode": "rebuild" if rebuild else "update", "blocked": blocked}
        else:
            old["generation"] += 1
            old['attempts'] = 0
            old["state"] = "queued" if old["state"] != "running" else "running"
            if immediate:
                old["due"] = now
            if rebuild:
                old["mode"] = "rebuild"
            old["blocked"] = blocked or old.get("blocked", False)
            old.pop("error", None)
        return ref

    def _mark_views(self, draft, *, rebuild=False, immediate=False, blocked=False):
        periods = time_scopes(draft["time"], self.config.repository.timezone)
        views = [("day", day) for day in periods["days"]]
        views += [("month", month) for month in periods["months"]]
        active = {entry['key'] for entry in self._topics()}
        topics = [topic for topic in draft.get('topics', []) if topic in active]
        if draft.get('ref'):
            topics += [key for key, value in self.repo.state['topic_registry']['definitions'].items()
                       if value.get('active') and draft['ref'] in value['members'] and key not in topics]
        views += [("runtime", topic) for topic in topics]
        views += [("memory", "global")]
        for topic in topics:
            self.repo.state["runtime_changed"][topic] = clock.time()
        return [self._queue_view(view, scope, rebuild=rebuild, immediate=immediate, blocked=blocked)
                for view, scope in views]

    def _pending(self, ref):
        return next((job for job in self.repo.state["pending_views"].values()
                     if job["ref"] == ref and job["state"] != "done"), None)

    def _view_units(self, view, scope):
        units = self.repo.iter_blocks(["unit"])
        if view in {"memory", "runtime"}:
            return units if view == "memory" else self._topic_units(scope, units)
        field = "days" if view == "day" else "months"
        return [u for u in units if scope in time_scopes(u["time"], self.config.repository.timezone)[field]]

    def _dependencies_ready(self, job):
        if job['view'] in {'runtime', 'memory'} and self.repo.state['topic_registry']['job']['state'] != 'done':
            return False
        for other in self.repo.state["pending_views"].values():
            if other["state"] == "done":
                continue
            if job["view"] == "month" and other["view"] == "day" and str(other["scope"]).startswith(job["scope"]):
                return False
            if job["view"] == "memory" and other["view"] == "runtime":
                return False
        return True

    def _has_ready_writes(self):
        with self.repo.lock():
            if hasattr(self.repo, 'ready_write_keys'):
                return bool(self.repo.ready_write_keys(clock.time()))
            return any(row['state'] in {'queued', 'running'} or
                       (row['state'] == 'waiting' and row.get('retry_at', clock.time()+1) <= clock.time())
                       for row in self.repo.state['writes'].values())

    def refresh_due(self, *, force=False):
        """One serial maintenance executor; reads never wait for model inference."""
        # Publish Units first, including retries admitted during a maintenance pass.
        # Explicit owner refreshes still take effect.
        if not force and self._has_ready_writes():
            return []
        if not self._maintenance_lock.acquire(blocking=False):
            return []
        completed = []
        try:
            self._discover_topics(force=force)
            with self.repo.lock():
                keys = sorted(self.repo.state["pending_views"], key=lambda key: (
                    {"day": 0, "month": 1, "runtime": 2, "memory": 3}[self.repo.state["pending_views"][key]["view"]], key))
            for key in keys:
                if not force and self._has_ready_writes():
                    break
                with self.repo.lock():
                    priority = getattr(self, '_view_recovery_refs', frozenset())
                    if priority and not any(job['ref'] in priority and job['state'] in
                                            {'queued', 'running', 'waiting'}
                                            for job in self.repo.state['pending_views'].values()):
                        self._view_recovery_refs = frozenset()
                        discovery = self.repo.state['topic_registry']['job']
                        if discovery['state'] == 'queued':
                            discovery['due'] = clock.time()
                        self.repo.save_state()
                        self.changed.set()
                        break  # Resume ordinary discovery order on the next pass.
                    job = self.repo.state["pending_views"][key]
                    if job['state'] == 'waiting' and (job.get('retry_at') or 0) <= clock.time():
                        job['state'] = 'queued'
                    if job["state"] != "queued" or (job["due"] > clock.time() and not force):
                        continue
                    if not self._dependencies_ready(job):
                        continue
                    generation, epoch = job["generation"], self.repo.state["deletion_epoch"]
                    snapshot = dict(job)
                    job["state"] = "running"
                    job['attempts'] = job.get('attempts', 0) + 1
                    self.repo.save_state()
                try:
                    self._refresh_view(snapshot, generation, epoch)
                    completed.append(snapshot["ref"])
                except Exception as exc:
                    with self.repo.lock():
                        current = self.repo.state["pending_views"][key]
                        current.update(failure(exc, current.get('attempts', 1), clock.time()))
                        current['validation_reason'] = getattr(exc, 'reason', None)
                        if getattr(exc, 'diagnostic', None):
                            current['diagnostic'] = exc.diagnostic
                        if current['generation'] != generation:
                            current.update(state='queued', attempts=0)
                        event('view_deferred', stage=current['view'], error_code=error_code(exc), status=current['state'],
                              validation_reason=current['validation_reason'])
                        self.repo.save_state()
            return completed
        finally:
            self._maintenance_lock.release()

    def _refresh_view(self, job, generation, epoch):
        view, scope = job["view"], job["scope"]
        units = self._view_units(view, scope)
        if view in EDITABLE_VIEWS:
            pins = pins_for(self.repo, job['ref'])
            covered = {ref for entry in pins.values() for ref in entry['facts']}
            units = [unit for unit in units if unit['ref'] not in covered]
        material = units
        if view == "memory":
            covered = {ref for topic in self.repo.state['topic_registry']['definitions'].values()
                       if topic.get('active') for ref in topic['members']}
            runtimes = []
            for runtime in self.repo.iter_blocks(['runtime']):
                valid = [entry for entry in runtime['entries'] if not entry.get('invalid_refs')]
                if valid:
                    runtimes.append(dict(runtime, body='\n'.join(entry['body'] for entry in valid)))
            material = runtimes + [u for u in units if u['ref'] not in covered]
        # Snapshot all evidence revisions before invoking the model.
        expected = {Ref.parse(item["ref"]).alias: item["revision"] for item in material}
        admitted = [item['ref'] for item in material]
        monthly_entries = None
        if view == 'month':
            # Compress the completed daily overviews, preserving Unit-level
            # evidence. Month-only dates and facts absent from day views remain
            # available, rather than inventing a day or dropping the evidence.
            monthly_entries, covered = [], set()
            for day in self.repo.iter_blocks(['day']):
                if not str(day['scope']).startswith(scope + '-'):
                    continue
                expected[day['ref']] = day['revision']
                for entry in day['entries']:
                    if not entry.get('invalid_refs') and set(entry['facts']) <= set(admitted):
                        monthly_entries.append({key: entry[key] for key in ('body', 'time', 'facts')})
                        covered.update(entry['facts'])
            monthly_entries.extend({'body': unit['body'], 'time': unit['time'], 'facts': [unit['ref']]}
                                   for unit in units if unit['ref'] not in covered)
        old = self.repo.read(job["ref"])
        expected[Ref.parse(job["ref"]).alias] = old["revision"]
        entries = []
        allowed = admitted
        # Unit tags may predate topic discovery. They are not evidence of a
        # published Runtime entrance. Rendered text duplicates those tags and
        # directory headers, so the summarizer receives only factual fields.
        fields = {'ref', 'kind', 'time', 'body', 'facts', 'source', 'scope', 'title'}
        batches = list(self._batches(monthly_entries if monthly_entries is not None else
                                    [{key: value for key, value in item.items() if key in fields} for item in material]))
        for batch in batches:
            scope_refs = (list(dict.fromkeys(ref for item in batch for ref in item['facts']))
                          if monthly_entries is not None else [item['ref'] for item in batch])
            input_material = {"view": view, 'batch_entries' if monthly_entries is not None else 'items': batch,
                              "names": self._topics()}
            if view == 'month':
                input_material['directions'] = [key for key, topic in self.repo.state['topic_registry']['definitions'].items()
                                                if set(topic['members']) & set(scope_refs)]
            if view == 'runtime':
                input_material['topic'] = next((topic for topic in self._topics() if topic['key'] == scope), {})
            if (job["mode"] == "update" and not job.get("blocked") and len(batches) == 1
                    and not (view in EDITABLE_VIEWS and pins_for(self.repo, job['ref']))):
                input_material["old_view"] = {key: value for key, value in old.items() if key in fields}
            output = self._task("rollup", input_material, {"view": view, "scope": scope, "refs": scope_refs},
                                scope_refs, mode=job["mode"])
            entries.extend(output["entries"])
        if len(batches) > 1:
            # The final proposal sees every completed batch's evidence-linked output.
            # Only the complete result is ever published.
            while len(_json(entries)) > self.config.build.task_batch_chars:
                before = len(_json(entries))
                reduced = []
                for group in self._batches(entries):
                    group_refs = list(dict.fromkeys(fact for entry in group for fact in entry["facts"]))
                    reduced.extend(self._task("rollup", {"view": view, "batch_entries": group, "names": self._topics()},
                        {"view": view, "scope": scope, "refs": group_refs}, group_refs, mode="rebuild")["entries"])
                if len(_json(reduced)) >= before:
                    raise ValueError("Rollup did not reduce material within the configured task budget")
                entries = reduced
            # Earlier batches may omit process-only citations. The final model
            # groups only evidence present in these summaries, not unseen Units.
            allowed = list(dict.fromkeys(fact for entry in entries for fact in entry['facts']))
            entries = self._task("rollup", {"view": view, "batch_entries": entries, "names": self._topics()},
                                 {"view": view, "scope": scope, "refs": allowed}, allowed, mode="rebuild")["entries"]
        with self.repo.transaction():
            key = _json([view, scope])
            current = self.repo.state["pending_views"][key]
            if current["generation"] != generation or epoch != self.repo.state["deletion_epoch"] or any(
                self.repo.revision(alias) != revision for alias, revision in expected.items()
            ):
                current["state"] = "queued"
                self.repo.save_state()
                return
            metadata = {"prompt_version": TASKS['rollup'].prompt_version, "built_at": datetime.now(ZoneInfo(
                self.config.repository.timezone)).isoformat()}
            if view == "runtime":
                metadata.update(active=True, key=scope, title=scope, aliases=self.repo.catalog[Ref.parse(job["ref"]).alias]
                                .get("metadata", {}).get("aliases", []))
            if view == "memory":
                metadata["directory"] = [{"ref": record["ref"], "title": record.get("title", record.get("scope"))}
                                         for record in self.repo.iter_blocks(["runtime", "month"])]
            self.repo.write_view(view, scope, entries, metadata)
            current.update(state="done", blocked=False)
            current.pop("error", None)
            for field in ('error_code', 'validation_reason', 'retry_at', 'diagnostic'):
                current.pop(field, None)
            self.repo.state["index_dirty"] = True
            self.repo.save_state()
        try:
            self.sync_index()
        except Exception as error:
            # File publication is authoritative; mirror outages are explicit on search.
            with self.repo.lock():
                self.repo.state['index_retry_at'] = clock.time() + 150
                self.repo.save_state()
            event('index_deferred', error_code=error_code(error))

    def schedule_daily(self, now=None):
        now = now or datetime.now(ZoneInfo(self.config.repository.timezone))
        date_key = now.date().isoformat()
        if now.strftime("%H:%M") < self.config.views.daily_at:
            return
        with self.repo.transaction():
            if self.repo.state.get("last_daily") == date_key:
                return
            self.repo.state["last_daily"] = date_key
            for job in self.repo.state["pending_views"].values():
                if job["state"] == "queued":
                    job["due"] = clock.time()
                # Same failed generation stays failed until an explicit owner retry.
            if now.strftime("%A").lower() == self.config.views.runtime_rebuild_weekday:
                cutoff = now.timestamp() - 7 * 86400
                for topic, changed in list(self.repo.state["runtime_changed"].items()):
                    if changed >= cutoff:
                        existing = self.repo.state["pending_views"].get(_json(["runtime", topic]))
                        if not existing or existing["state"] != "failed":
                            self._queue_view("runtime", topic, rebuild=True, immediate=True)
                            self._queue_view("memory", "global", rebuild=True, immediate=True)
            self.repo.save_state()
        self.changed.set()

    def source_priority_deadline(self):
        """Let a pressured intake drain; durable timestamps also survive a restart."""
        with self.repo.lock():
            deadline = self.repo.state.get('last_source_admission', 0) + self.config.build.source_priority_quiet_seconds
            if deadline <= clock.time():
                return None
            threshold = self.config.build.source_priority_backlog
            if hasattr(self.repo, 'write_backlog_at_least'):
                pressured = self.repo.write_backlog_at_least(threshold)
            else:
                pressured = sum(row['state'] in {'queued', 'running'}
                                for row in self.repo.state['writes'].values()) >= threshold
            return deadline if pressured else None

    def prepare_writes(self, *, limit=1, exclude=(), defer_index=False):
        """Select bounded independent tasks; reserve them in the caller before dispatch."""
        # A shared index outage must defer the worker, not fail unrelated writes
        # after spending inference on each of them.
        with self._write_lock:
            with self.repo.lock():
                dirty = self.repo.state.get('index_dirty', False)
            if defer_index and self.repo.state.get('index_retry_at') is not None:
                return []
            if dirty and not defer_index:
                self.sync_index()
            excluded = set(exclude) | self._active_writes
            pending = next(iter(self.repo.state.get('projection_updates', {})), None)
            if pending is not None:
                # Retirement updates deletion epochs and must wait for all model
                # proposals already in flight, including reserved pool jobs.
                if excluded:
                    return []
                self._activate_projection(pending)
                return [pending]
            now = clock.time()
            if hasattr(self.repo, 'ready_write_keys'):
                return self.repo.ready_write_keys(now, limit=limit, exclude=excluded)
            with self.repo.lock():
                return [key for key, request in self.repo.state['writes'].items() if key not in excluded and
                        (request['state'] in {'running', 'queued'} or
                         (request['state'] == 'waiting' and request.get('retry_at', now + 1) <= now))][:limit]

    def recover_writes(self):
        """Synchronous single-task recovery for explicit callers."""
        return [self.process_write(key) for key in self.prepare_writes()]

    def memory_status(self, *, from_, request_id=None, idempotency_key=None):
        self._authorize(from_, 'write')
        if not request_id and not idempotency_key:
            raise ValueError('Provide request_id or the original idempotency_key')
        key = request_id or _hash([from_, idempotency_key])
        with self.repo.lock():
            request = self.repo.state['writes'].get(key)
            if request is None or (request['source'] != from_ and from_ not in self.config.access.owners):
                raise ValueError('Unknown write request')
            return self._current_result(request)

    def memory_statuses(self, *, from_, request_ids):
        """Bounded batch receipts for acquisition; never copy model checkpoints over HTTP."""
        self._authorize(from_, 'write')
        if (not isinstance(request_ids, list) or not 1 <= len(request_ids) <= 200 or
                any(not isinstance(key, str) or not key or len(key) > 128 for key in request_ids)):
            raise ValueError('Provide between 1 and 200 request identifiers')
        with self.repo.lock():
            rows = (self.repo.write_statuses(request_ids) if hasattr(self.repo, 'write_statuses') else
                    {key: self.repo.state['writes'].get(key) for key in request_ids})
            if any(not rows.get(key) or (rows[key]['source'] != from_ and from_ not in self.config.access.owners)
                   for key in request_ids):
                raise ValueError('Unknown write request')
            return {'results': [self._current_result(rows[key]) for key in request_ids]}

    def readiness(self):
        storage = self.repo.healthy() if hasattr(self.repo, 'healthy') else self.repo.root.is_dir()
        configured = self.runner is not None and self.embedder is not None
        indexed = storage and not self.repo.state.get('index_dirty', True)
        return {'ready': storage and configured, 'storage': storage, 'accepting_writes': storage,
                'models_configured': configured, 'index_ready': indexed, 'degraded': not indexed,
                'model_concurrency': {'mllm': self.config.models.mllm.concurrency,
                                      'embedding': self.config.models.embedding.concurrency}}

    def media_resource(self, identity, uri):
        self._authorize(identity)
        if not isinstance(uri, str) or not uri.startswith('memu://media/'):
            raise ValueError('Invalid media resource')
        ref = Ref.parse(uri[len('memu://media/'):])
        record = self.repo.read(ref.canonical)
        if record['kind'] != 'media':
            raise ValueError('Resource must name native media')
        if record.get('redacted_ranges') and ref.start_seconds is None:
            raise ValueError('Redacted media requires an intact segment')
        data = (self.repo.root / record['path']).read_bytes()
        if ref.start_seconds is not None:
            item = native_item(data, record['mime_type'], float(ref.start_seconds), float(ref.end_seconds))
            data = base64.b64decode(item['data_base64'])
        if len(data) > 16 * 1024 * 1024:
            raise ValueError('Media resource exceeds 16 MiB; request a smaller segment')
        return {'contents': [{'uri': uri, 'mimeType': record['mime_type'],
                              'blob': base64.b64encode(data).decode()}]}

    def _resolve(self, ref):
        if ref in {"MEMORY", "MEMORY.md"}:
            result = self.repo.view_ref("memory", "global")
            if result is None:
                raise ValueError("MEMORY has not been built yet")
            return result
        return Ref.parse(ref).canonical

    def _page(self, records, *, limit, budget, cursor, signature):
        if not 1 <= limit <= 1000 or not 1 <= budget <= 1_000_000:
            raise ValueError("Invalid limit or character budget")
        offset = 0
        if cursor:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if decoded["signature"] != signature:
                    raise ValueError("Query or repository changed; restart pagination")
                offset = decoded["offset"]
                if not isinstance(offset, int) or offset < 0:
                    raise ValueError("Invalid cursor")
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Invalid cursor") from exc
        results, used, position = [], 0, offset
        while position < len(records) and len(results) < limit and used < budget:
            record = dict(records[position])
            body = record.get("body", record.get("text", ""))
            available = budget - used
            record["body"] = body[:available]
            record.pop("text", None)
            record.pop("entries", None)
            record["truncated"] = len(body) > available
            used += len(record["body"])
            results.append(record)
            position += 1
        next_cursor = None if position >= len(records) else base64.urlsafe_b64encode(
            _json({"offset": position, "signature": signature}).encode()).decode()
        return {"results": results, "next_cursor": next_cursor,
                "truncated": bool(next_cursor) or any(r["truncated"] for r in results),
                "returned_chars": used, "total_matches": len(records)}

    def _revision_signature(self, refs=None):
        with self.repo.lock():
            if refs is not None:
                return _hash({Ref.parse(ref).alias: self.repo.revision(ref) for ref in refs})
            if hasattr(self.repo, 'version'):
                return self.repo.version('grep_version')
            return _hash({alias: row['revision'] for alias, row in self.repo.catalog.items()})

    def import_result(self, identity, source_instance_id, object_ids):
        self._authorize(identity)
        if identity not in self.config.access.owners:
            raise PermissionError('Only owners may inspect import results')
        if not isinstance(source_instance_id, str) or not isinstance(object_ids, list) or not 1 <= len(object_ids) <= 1000 or any(not isinstance(v, str) for v in object_ids):
            raise ValueError('Invalid import lookup')
        with self.repo.lock():
            if hasattr(self.repo, 'import_sources'):
                aliases = self.repo.import_sources(source_instance_id, object_ids)
                selected = [block['ref'] for block in self.repo.iter_blocks(['source'], aliases=aliases)]
                for request in self.repo.writes_referencing(aliases):
                    selected.extend(ref for ref in request.get('result', {}).get('units', [])
                                    if ref not in self.repo.state['deleted_refs'])
                return {'refs': list(dict.fromkeys(selected))[:100]}
            aliases = {alias for alias, row in self.repo.catalog.items()
                       if row['kind'] == 'source' and row['metadata'].get('memulife', {}).get('source_instance_id') == source_instance_id
                       and row['metadata']['memulife'].get('object_id') in object_ids}
            blocks = self.repo.iter_blocks(['source', 'unit', 'detail'])
            selected = [block['ref'] for block in blocks if block['ref'].split(':')[0] in aliases]
            evidence = list(selected)
            # Follow direct source evidence and optional digests, excluding removed Units.
            for kind in ['detail', 'unit']:
                for block in blocks:
                    if block['kind'] == kind and block['ref'] not in self.repo.state['deleted_refs'] and any(self._overlaps(ref, known) for ref in block.get('facts', []) for known in evidence):
                        selected.append(block['ref']); evidence.append(block['ref'])
            return {'refs': list(dict.fromkeys(selected))[:100]}

    def memory_read(self, *, from_, refs=None, links=False, direction="both", relations=None,
                    hops=None, budget_chars=None, cursor=None, history=False, entry_id=None):
        self._authorize(from_)
        refs = refs or ["MEMORY"]
        with self.repo.lock():
            if history:
                return self._revision_history(refs, entry_id, budget_chars or self.config.read.budget_chars,
                                              cursor, from_)
            if entry_id and len(refs) != 1:
                raise ValueError('entry_id requires exactly one Summary or Runtime reference')
            results = []
            for address in refs:
                ref = self._resolve(address)
                pending = self._pending(ref)
                record = self.repo.read(ref)
                protected = [entry for entry in record.get('entries', []) if entry.get('protected')]
                if pending and pending.get("blocked") and not protected:
                    results.append({"ref": ref, "body": "", "pending_update": True,
                                    "withheld": True, "revision": self.repo.revision(Ref.parse(ref).alias)})
                    continue
                if pending and pending.get('blocked') and protected:
                    record = dict(record, entries=protected, body='\n'.join(e['body'] for e in protected),
                                  facts=list(dict.fromkeys(f for e in protected for f in e['facts']
                                                         if f not in e['invalid_refs'])), withheld_unprotected=True)
                record["relations"] = [rel for rel in record.get("relations", [])
                                       if rel["target"] not in self.repo.state["deleted_refs"]]
                record["pending_update"] = bool(pending)
                if record["kind"] == "media":
                    record['resource'] = 'memu://media/' + ref
                    record.pop('absolute_path', None)
                if links:
                    self._ensure_index()
                    record["links"] = self.index.links(ref, direction=direction, relations=relations,
                                                        hops=hops or self.config.read.relation_hops)
                if record.get('view') in EDITABLE_VIEWS:
                    # Preserve the existing view response; entry_id reads one
                    # body without duplicating the entire view in the budget.
                    if not entry_id:
                        record['view_entries'] = [{key: entry[key] for key in
                            ('entry_id', 'edit_revision', 'protected', 'facts', 'time', 'invalid_refs')}
                            for entry in record['entries']]
                        if record['view'] == 'runtime':
                            record['runtime_entries'] = record['view_entries']
                        results.append(record)
                    else:
                        entry = next((e for e in record['entries'] if e['entry_id'] == entry_id), None)
                        if entry is None:
                            raise ValueError('View entry not found')
                        results.append(dict(entry, ref=record['ref'], kind=record['view'] + '_entry',
                            scope=record['scope'], pending_update=record['pending_update']))
                elif entry_id:
                    raise ValueError('entry_id applies only to Summary and Runtime')
                else:
                    results.append(record)
            signature = _hash([from_, refs, links, direction, relations, hops, entry_id,
                               self._revision_signature([self._resolve(ref) for ref in refs]),
                               self._revision_signature() if links else None])
        return self._page(results, limit=1000, budget=budget_chars or self.config.read.budget_chars,
                          cursor=cursor, signature=signature)

    def memory_search(self, *, from_, method=None, query="", query_refs=None, query_media=None,
                      layers=None, scope=None, limit=None, budget_chars=None, cursor=None):
        self._authorize(from_)
        if method is None:
            method = self.config.read.default_method
        if method not in {"grep", "semantic"}:
            raise ValueError("Search method must be grep or semantic")
        layers, scope = layers or ["unit"], scope or {}
        limit, budget = limit or self.config.read.limit, budget_chars or self.config.read.budget_chars
        self._ensure_index()
        if method == "grep":
            if query_refs or query_media:
                raise ValueError("grep matches text only; use semantic for media queries")
            if not query:
                raise ValueError("grep needs a nonempty text query")
            query_vectors = None
        else:
            if layers != ["unit"]:
                raise ValueError("Semantic search returns Units and native-media hit entrances")
            items = [{"kind": "text", "text": query}] if query else []
            for ref in query_refs or []:
                ref = self._resolve(ref)
                record = self.repo.read(ref)
                if self._pending(ref) and self._pending(ref).get("blocked"):
                    raise ValueError("Deleted view content is unavailable as a query")
                if record["kind"] == "media":
                    items.extend(item for _, item in self._media_items(ref))
                else:
                    items.append({"kind": "text", "text": record["body"]})
            for media in query_media or []:
                data = decode_media(media)
                duration = probe_duration(data, media["mime_type"])
                if duration is None:
                    items.append(native_item(data, media["mime_type"]))
                else:
                    bounds = self.embedder.capabilities.get("max_duration_seconds", {})
                    maximum = bounds.get(media["mime_type"].split("/")[0], 30) if isinstance(bounds, dict) else bounds
                    start = 0.0
                    while start < duration:
                        end = min(duration, start + float(maximum or 30))
                        items.append(native_item(data, media["mime_type"], start, end))
                        start = end
            if not items:
                raise ValueError("Semantic search requires text, a locator, or native media")
            query_vectors = self._embed([{**item, "purpose": "query"} for item in items])
        with self.repo.lock():
            # Freeze the committed file generation while taking results from its mirror.
            # Deletion cannot slip between readiness validation and result retrieval.
            self._ensure_index()
            version = (self.repo.version('search_version') if method == 'semantic' and hasattr(self.repo, 'version')
                       else self._revision_signature())
            signature = _hash([from_, method, query, query_refs, query_media, layers, scope, version])
            if method == 'grep':
                offset = self._cursor_offset(cursor, signature)
                records = self.index.grep(query, scope=scope, layers=layers, limit=limit + 1, offset=offset)
            else:
                records = self.index.search(query_vectors, scope=scope, limit=1000)
            for record in records:
                record['actual_scope'] = {key: record.get(key) for key in ['path', 'source', 'topics', 'time']}
        result = self._page(records, limit=limit, budget=budget,
                            cursor=cursor if method == 'semantic' else None, signature=signature)
        if method == 'grep':
            consumed = len(result['results'])
            result['next_cursor'] = (base64.urlsafe_b64encode(_json({'offset': offset + consumed,
                                      'signature': signature}).encode()).decode() if consumed < len(records) else None)
            result['total_matches'] = None
        else:
            result['candidate_matches'] = result.pop('total_matches')
            result['candidate_limit'] = 1000
            result['exhaustive'] = False
        result['scope'] = scope
        return result

    @staticmethod
    def _cursor_offset(cursor, signature):
        if not cursor:
            return 0
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if value['signature'] != signature or type(value['offset']) is not int or value['offset'] < 0:
                raise ValueError
            return value['offset']
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ValueError('Query or index changed; restart pagination') from None

    @staticmethod
    def _overlaps(first, second):
        a, b = Ref.parse(first), Ref.parse(second)
        if a.alias != b.alias:
            return False
        if a.kind == "a":
            if a.start_seconds is None or b.start_seconds is None:
                return True
            return a.start_seconds < b.end_seconds and b.start_seconds < a.end_seconds
        if a.start is None or b.start is None:
            return True
        return a.start <= b.end and b.start <= a.end

    def _deletion_plan(self, refs, *, with_source=False, source_refs=None, allow_sources=False):
        refs = list(dict.fromkeys(self._resolve(ref) for ref in refs))
        units = [self.repo.read(ref) for ref in refs]
        if (not refs and not (allow_sources and source_refs)) or any(unit["kind"] != "unit" for unit in units):
            raise ValueError("delete/correct requires live Unit locators")
        selected_sources = list(source_refs or [])
        if selected_sources and not with_source:
            raise ValueError("source_refs requires with_source=true")
        if with_source and not selected_sources:
            pending = [fact for unit in units for fact in unit["facts"]]
            seen = set()
            while pending:
                ref = pending.pop()
                if ref in seen:
                    continue
                seen.add(ref)
                record = self.repo.read(ref)
                if record["kind"] == "detail":
                    pending.extend(record["facts"])
                else:
                    selected_sources.append(ref)
        selected_sources = merge_refs(selected_sources)
        for ref in selected_sources:
            record = self.repo.read(ref)
            if record["kind"] not in {"source", "media", "detail"}:
                raise ValueError("with_source may select only source, detail, or native media")
        all_blocks = (self.index.dependent_blocks(refs + selected_sources)
                      if hasattr(self.index, 'dependent_blocks') and not self.repo.state['index_dirty']
                      else self.repo.iter_blocks(['unit', 'detail', 'view']))
        erased = set(refs + selected_sources)
        # A selected subrange of a detail record invalidates that whole stable caption/summary.
        erased.update(block["ref"] for block in all_blocks if block["kind"] == "detail"
                      and any(self._overlaps(block["ref"], ref) for ref in selected_sources))
        # Expand only factual dependencies; relation neighbors remain intact.
        while True:
            dependent = {block["ref"] for block in all_blocks if block["kind"] in {"unit", "detail"}
                         and any(self._overlaps(fact, target) for fact in block.get("facts", []) for target in erased)}
            if dependent <= erased:
                break
            erased.update(dependent)
        affected_units = [block for block in all_blocks if block["kind"] == "unit" and block["ref"] in erased]
        # Follow view links transitively; immediate suppression prevents reads of stale deleted prose.
        affected_views = set()
        while True:
            found = {block["ref"] for block in all_blocks if block["kind"] == "view"
                     and any(self._overlaps(fact, target) for fact in block["facts"]
                             for target in erased | affected_views)}
            if found <= affected_views:
                break
            affected_views.update(found)
        revisions = {Ref.parse(ref).alias: self.repo.revision(Ref.parse(ref).alias)
                     for ref in erased | affected_views}
        return {'units': units, 'sources': selected_sources, 'erased': erased,
                'affected_units': affected_units, 'affected_views': affected_views,
                'revisions': revisions, 'revision': _hash([sorted(erased), sorted(affected_views),
                                                         revisions, self.repo.state['deletion_epoch']])}

    def _delete(self, refs, *, with_source=False, source_refs=None, allow_sources=False, expected_revision=None,
                defer_index=False):
        plan = self._deletion_plan(refs, with_source=with_source, source_refs=source_refs, allow_sources=allow_sources)
        if expected_revision is not None and plan['revision'] != expected_revision:
            raise RevisionConflict('Delete targets changed; review the deletion again')
        units, selected_sources, erased = plan['units'], plan['sources'], plan['erased']
        affected_units, affected_views = plan['affected_units'], plan['affected_views']
        erased_by_alias = {}
        for target in erased:
            erased_by_alias.setdefault(Ref.parse(target).alias, []).append(target)
        def intersects_erased(ref):
            return any(self._overlaps(ref, target) for target in erased_by_alias.get(Ref.parse(ref).alias, []))
        redactions = {}
        for ref in erased:
            parsed = Ref.parse(ref)
            if parsed.kind == "a" and parsed.start_seconds is not None:
                redactions.setdefault(parsed.alias, []).append((float(parsed.start_seconds), float(parsed.end_seconds)))
        prepared = {}
        if redactions:
            from .media import redact_media
            for alias, ranges in redactions.items():
                if alias in erased:
                    continue
                record = self.repo.read(alias)
                original = (self.repo.root / record["path"]).read_bytes()
                prepared[alias] = (*redact_media(original, record["mime_type"], ranges), record["revision"])
        with self.repo.transaction():
            for alias, revision in plan['revisions'].items():
                if self.repo.revision(alias) != revision:
                    raise RevisionConflict("Delete targets changed before commit")
            self._remove_conversation_units(erased)
            self.repo.state["deletion_epoch"] += 1
            for unit in affected_units:
                self._mark_views(unit, rebuild=True, immediate=True, blocked=True)
            for ref in affected_views:
                record = self.repo.read(ref)
                self._queue_view(record["view"], record["scope"], rebuild=True, immediate=True, blocked=True)
            for ref in merge_refs(sorted(erased)):
                parsed = Ref.parse(ref)
                if parsed.kind == "a" and parsed.start_seconds is not None:
                    continue
                self.repo.blank(ref)
            for alias, (data, mime, revision) in prepared.items():
                if self.repo.revision(alias) != revision:
                    raise RevisionConflict("Media changed before redaction commit")
                record = self.repo.catalog[alias]
                record["metadata"]["mime_type"] = mime
                self.repo.commit({record["path"]: data})
                self.repo.state.setdefault('media_index_invalidated', {})[alias] = self.repo.revision(alias)
                self.repo.state.setdefault("media_blanked_ranges", []).extend(
                    {"alias": alias, "start": start, "end": end} for start, end in redactions[alias])
            for ref in erased:
                self.repo.state.get('unit_pins', {}).pop(ref, None)
                self.repo.state.setdefault('worklog_evidence', {}).pop(Ref.parse(ref).canonical, None)
                if ref not in self.repo.state["deleted_refs"]:
                    self.repo.state["deleted_refs"].append(ref)
            # Clear drafts/candidates that can contain deleted material from maintenance checkpoints.
            requests = (self.repo.writes_referencing({Ref.parse(ref).alias for ref in erased})
                        if hasattr(self.repo, 'writes_referencing') else self.repo.state['writes'].values())
            for request in requests:
                inputs = request.get("source_refs", []) + request.get("digest_refs", [])
                committed = request.get("result", {}).get("units", [])
                if any(intersects_erased(ref) for ref in inputs + committed):
                    for field in ["drafts", "processed", "digests", "compact"]:
                        request.pop(field, None)
                    if request["state"] != "done":
                        request.update(state="failed", error="Task evidence was deleted; explicit rebuild required", deleted=True)
            self.repo.state["staged_vectors"] = {ref: vector for ref, vector in self.repo.state["staged_vectors"].items()
                if Ref.parse(ref).alias not in prepared and not intersects_erased(ref)}
            self.repo.state["index_dirty"] = True
            self.repo.state.pop('index_retry_at', None)
            with self._media_cache_lock:
                self._media_cache.clear()
            if affected_units:
                self._queue_topics(immediate=True)
                if not defer_index:
                    self._reconcile_topics()
            self.repo.save_state()
        if self.index is not None and not defer_index:
            try:
                # Remove obsolete vectors before rebuilding so a redacted media clip cannot reuse its old embedding.
                aliases = {Ref.parse(ref).alias for ref in erased} | set(prepared)
                indexed = (self.index.vectors(aliases) if getattr(self.index, 'incremental', False)
                           else self.index.vectors())
                with self._write_lock.model_work(), self._index_lock:
                    self.index.delete([ref for ref in indexed if Ref.parse(ref).alias in prepared or
                                       any(self._overlaps(ref, target) for target in erased)] + list(erased))
                self.sync_index()
            except Exception as error:
                with self.repo.lock():
                    self.repo.state['index_retry_at'] = clock.time() + 150
                    self.repo.save_state()
                event('delete_index_deferred', error_code=error_code(error))
        self.changed.set()
        return {"status": "done", "deleted": sorted(erased), "sources_retained": not with_source,
                "pending_views": [job["ref"] for job in self.repo.state["pending_views"].values() if job["state"] != "done"],
                "index_ready": not self.repo.state["index_dirty"]}

    def memory_manage(self, *, from_, action, refs=None, with_source=False, source_refs=None,
                      text="", time=None, view=None, scope=None, request_id=None):
        self._owner(from_)
        if scope is not None and not isinstance(scope, str):
            raise ValueError("Management scope must be a day, month, or canonical topic string")
        if action == "update" and view == "runtime" and not scope:
            raise ValueError("Runtime updates require a canonical topic scope")
        with self._write_lock:
            if action == 'topics':
                return self._list_topics()
            if action == 'topic':
                return self._set_topic(scope, text)
            if action == "delete":
                return self._delete(refs or [], with_source=with_source, source_refs=source_refs)
            if action == "correct":
                self._require_models()
                originals = [self.repo.read(self._resolve(ref)) for ref in refs or []]
                if not originals:
                    raise ValueError("correct requires Unit locators")
                sources = list(dict.fromkeys(fact for entry in originals for fact in entry["facts"]))
                deletion = self._delete(refs or [], with_source=False)
                # Preserve original evidence addresses; corrections append, never edit the old Unit.
                key = uuid.uuid4().hex
                with self.repo.transaction():
                    if text:
                        correction_time = time or {"precision": "instant", "value": datetime.now(
                            ZoneInfo(self.config.repository.timezone)).isoformat()}
                        sources.append(self.repo.save_source(text, from_, correction_time, {"operation": "correct"}))
                    self.repo.state["writes"][key] = {
                        "request_id": key, "state": "running", "fingerprint": _hash([sources, key]), "source": from_,
                        "time": time or {"precision": "unknown", "value": None}, "metadata": {"operation": "correct"},
                        "source_refs": sources, "epoch": self.repo.state["deletion_epoch"], "result": {},
                        "input_revisions": {Ref.parse(ref).alias: self.repo.revision(Ref.parse(ref).alias) for ref in sources},
                    }
                    self.repo.save_state()
                with self.repo.lock():
                    request = self.repo.state['writes'][key]
                    request.update(state='queued', stage='accepted', attempts=0, created_at=clock.time())
                    request['result']['replaces_deleted'] = deletion['deleted']
                    self.repo.save_state()
                self.changed.set()
                return {**self._current_result(request), 'status': 'accepted'}
            if action == "update":
                if view not in {"runtime", "memory"} or not text:
                    raise ValueError("update needs runtime/memory and the owner's actual instruction text")
                if view == 'runtime' and scope not in {entry['key'] for entry in self._topics()}:
                    self._set_topic(scope, scope)
                instruction = f"用户要求更新 {view} {scope or 'global'}：\n{text}"
                result = self.memory_write(from_=from_, text=instruction, time=time or {
                    "precision": "instant", "value": datetime.now(ZoneInfo(self.config.repository.timezone)).isoformat()},
                    metadata={"operation": "update", "view": view, "scope": scope})
                if result["status"] != "done":
                    return result
                with self.repo.transaction():
                    self._queue_view(view, scope or "global", immediate=True)
                    if view == "runtime":
                        self._queue_view("memory", "global", immediate=True)
                    self.repo.save_state()
                self.changed.set()
                return {**result, "updated_view": self.repo.view_ref(view, scope or "global")}
            if action == "rebuild":
                if view == 'topics':
                    with self.repo.transaction():
                        self._queue_topics(immediate=True, rescan=True)
                        self.repo.save_state()
                    self.changed.set()
                    return {'status': self.repo.state['topic_registry']['job']['state'], **self._list_topics()}
                if view == "index":
                    return self.sync_index(rebuild_vectors=True)
                if view not in {"day", "month", "runtime", "memory"}:
                    raise ValueError("rebuild requires a view or index")
                if view == 'runtime' and scope not in {entry['key'] for entry in self._topics()}:
                    raise ValueError('Specify a tracked topic with action=topic before rebuilding Runtime')
                with self.repo.transaction():
                    ref = self._queue_view(view, scope or "global", rebuild=True, immediate=True)
                    if view == "runtime":
                        self._queue_view("memory", "global", rebuild=True, immediate=True)
                    self.repo.save_state()
                self.changed.set()
                pending = self._pending(ref)
                return {"ref": ref, "status": pending["state"] if pending else "done"}
            if action == "retry":
                if request_id:
                    with self.repo.lock():
                        request = self.repo.state["writes"].get(request_id)
                        if request is None:
                            raise ValueError("Unknown write request")
                        if request["state"] == "done":
                            if self.repo.state["index_dirty"]:
                                self.sync_index()
                            return self._current_result(request)
                        if request.get("superseded_by"):
                            raise ValueError("Superseded projection cannot be retried; use the current projection")
                        if request.get("deleted"):
                            raise ValueError("Deleted evidence cannot be retried; rebuild explicitly from surviving sources")
                        request.update(state="queued", attempts=0, retry_at=None, epoch=self.repo.state["deletion_epoch"])
                        request.pop("error", None)
                        # Relations must be revalidated against today's candidates after an explicit retry.
                        # Validated Unit drafts are already checkpointed. An embedding/index
                        # outage must not discard them and repeat a successful model task.
                        request["processed"] = []
                        self.repo.save_state()
                        if not request.get("source_refs"):
                            return {"status": "queued", "request_id": request_id, "resubmit_material": True}
                    self.changed.set()
                    return {**self._current_result(self.repo.state['writes'][request_id]), 'status': 'accepted'}
                with self.repo.lock():
                    count = 0
                    discovery = self.repo.state['topic_registry']['job']
                    if discovery['state'] == 'failed' and view in {None, 'topics'}:
                        self._queue_topics(immediate=True)
                        count += 1
                    for job in self.repo.state["pending_views"].values():
                        if job["state"] == "failed" and (view is None or job["view"] == view) and (scope is None or job["scope"] == scope):
                            job.update(state="queued", attempts=0, due=clock.time())
                            job.pop("error", None)
                            count += 1
                    self.repo.save_state()
                self.changed.set()
                return {"retried": count, "pending": [job for job in self.repo.state["pending_views"].values() if job["state"] != "done"]}
            raise ValueError("Unknown management action")
