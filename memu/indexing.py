"""Changed-file indexing and recovery, separate from write acceptance."""
from contextlib import nullcontext
from copy import deepcopy
import time

from .errors import IndexNotReady, RevisionConflict
from .refs import Ref
from .events import event
from .retry import error_code


class IndexMaintenance:
    def _indexable(self, block):
        if block['kind'] != 'source':
            return True
        content = self.repo.catalog[Ref.parse(block['ref']).alias]['metadata'].get('content', {})
        return not (content.get('audit_only') or content.get('superseded_by') or content.get('pending_projection'))

    def sync_index(self, *, rebuild_vectors=False):
        if not getattr(self.index, 'incremental', False) or not getattr(self.repo, 'postgres', False):
            return self._legacy_sync_index(rebuild_vectors=rebuild_vectors)
        started = time.monotonic()
        # No write-lock owner may wait for the publisher: delete/rebuild and
        # ordinary commits must share the same lock order.
        with self._write_lock.model_work(), self._index_lock:
            try:
                result = self._sync_index_snapshot(rebuild_vectors=rebuild_vectors)
            except Exception as error:
                with self.repo.lock():
                    self.repo.state['index_dirty'] = True
                    self.repo.state['index_retry_at'] = time.time() + 150
                    self.repo.save_state()
                self.changed.set()
                event('index_deferred', error_code=error_code(error))
                raise
        event('index_synced', count=result.get('files', 0), elapsed_ms=round((time.monotonic() - started) * 1000))
        return result

    def _sync_index_snapshot(self, *, rebuild_vectors=False):
        with self.repo.lock():
            changed = dict(self.repo.state['dirty_files'])
            if rebuild_vectors:
                changed = {alias: row['revision'] for alias, row in self.repo.catalog.items()}
            suppressed = {task['ref'] for task in self.repo.state['pending_views'].values()
                          if task.get('blocked')}
            for alias in suppressed:
                changed[alias] = self.repo.revision(alias)
            epoch = self.repo.state['deletion_epoch']
            invalidated = {alias: revision for alias, revision in self.repo.state.get('media_index_invalidated', {}).items()
                           if alias in changed}
            revised = {ref: revision for ref, revision in self.repo.state.get('unit_index_invalidated', {}).items()
                       if Ref.parse(ref).alias in changed}
            if not changed:
                self.repo.state['index_dirty'] = False
                self.repo.state.pop('index_retry_at', None)
                self.repo.save_state()
                return {'status': 'done', 'files': 0}
            files = [deepcopy(self.repo.catalog[alias]) for alias in changed]
            snapshots = {row['alias']: row for row in files}
            blocks = [block for block in self.repo.iter_blocks(aliases=changed)
                      if block['ref'] not in suppressed and self._indexable(block)]
            staged = {key: deepcopy(value) for key, value in self.repo.state['staged_vectors'].items()
                      if Ref.parse(key).alias in changed and key not in revised}
        vectors = {} if rebuild_vectors else self.index.vectors(changed)
        vectors = {ref: value for ref, value in vectors.items()
                   if Ref.parse(ref).alias not in invalidated and ref not in revised}
        generated = {}
        valid = {block['ref'] for block in blocks}
        segments = []
        for block in blocks:
            ref = block['ref']
            if block['kind'] == 'unit':
                if ref in staged:
                    vectors[ref] = dict(staged[ref])
                if ref not in vectors:
                    vectors[ref] = self._embed([{'kind': 'text', 'text': block['body']}])[0]
                    generated[ref] = vectors[ref]
            elif block['kind'] == 'media':
                alias = Ref.parse(ref).alias
                for vector_ref in staged:
                    if Ref.parse(vector_ref).alias == alias:
                        vectors[vector_ref] = dict(staged[vector_ref])
                cached = [key for key in vectors if Ref.parse(key).alias == alias]
                if not cached:
                    items = self._media_items(ref)
                    cached = [key for key, _ in items]
                    vectors.update(zip(cached, self._embed([item for _, item in items])))
                    generated.update({key: vectors[key] for key in cached})
                for key in cached:
                    valid.add(key)
                    if key != ref:
                        segments.append(dict(block, ref=key))
        vectors = {key: value for key, value in vectors.items() if key in valid}
        if self.embedder is not None and any(
            value.get(field) != self.embedder.capabilities.get(field)
            for value in vectors.values() for field in ('space_id', 'model_version', 'preprocess_version')
        ):
            raise IndexNotReady('Embedding model changed; explicitly rebuild vectors')
        with self.repo.lock():
            if epoch != self.repo.state['deletion_epoch']:
                self.changed.set()
                return {'status': 'pending', 'files': 0}
            # Only newly produced vectors are checkpointed; unrelated vectors
            # never round-trip through control state.
            for key, value in generated.items():
                alias = Ref.parse(key).alias
                if self.repo.revision(alias) == changed[alias]:
                    self.repo.state['staged_vectors'][key] = value
            self.repo.save_state()
        # Index SQL does not own the repository lock: intake may persist new
        # Sources while this derivative snapshot is being published.
        self.index.sync(files, blocks + segments, vectors, full=rebuild_vectors)
        with self.repo.transaction():
            current_epoch = self.repo.state['deletion_epoch']
            current_suppressed = {task['ref'] for task in self.repo.state['pending_views'].values()
                                  if task.get('blocked')}
            synced = {alias for alias, revision in changed.items() if epoch == current_epoch and
                      self.repo.catalog.get(alias, {}) == snapshots[alias] and
                      (alias in suppressed) == (alias in current_suppressed)}
            for alias in synced:
                self.repo.state['dirty_files'].pop(alias, None)
                if self.repo.state.get('media_index_invalidated', {}).get(alias) == invalidated.get(alias):
                    self.repo.state.get('media_index_invalidated', {}).pop(alias, None)
            active_staged = self.repo.state['staged_vectors']
            for ref, revision in revised.items():
                if (Ref.parse(ref).alias in synced and
                        self.repo.state.get('unit_index_invalidated', {}).get(ref) == revision):
                    self.repo.state['unit_index_invalidated'].pop(ref, None)
            for key in list(active_staged):
                if Ref.parse(key).alias in synced:
                    del active_staged[key]
            remaining = bool(self.repo.state['dirty_files']) or epoch != current_epoch
            self.repo.state['index_dirty'] = remaining
            self.repo.state.pop('index_retry_at', None)
            if remaining:
                self.changed.set()
            semantic = any(file['kind'] in {'unit', 'detail', 'media'} for file in files)
            self.repo.db.execute('UPDATE memu_repository SET grep_version=grep_version+1, '
                                 'search_version=search_version+%s', (int(semantic),))
            self.repo.save_state()
        return {'status': 'pending' if remaining else 'done', 'files': len(files),
                'blocks': len(blocks), 'vectors': len(vectors)}

    def _legacy_sync_index(self, *, rebuild_vectors=False):
        """Rebuild only the derivative index. Never regenerate Unit/model proposals."""
        if self.index is None:
            raise IndexNotReady("PostgreSQL index is not configured")
        with self._write_lock, (self._write_lock.pinned() if hasattr(self._write_lock, 'pinned') else nullcontext()):
            with self.repo.lock():
                revisions = {alias: self.repo.revision(alias) for alias in self.repo.catalog}
                epoch = self.repo.state["deletion_epoch"]
                staged = dict(self.repo.state["staged_vectors"])
                invalidated = dict(self.repo.state.get('media_index_invalidated', {}))
                revised = dict(self.repo.state.get('unit_index_invalidated', {}))
                staged = {ref: value for ref, value in staged.items() if ref not in revised}
            blocks = self._all_blocks()
            vectors = {} if rebuild_vectors else self.index.vectors()
            vectors = {ref: value for ref, value in vectors.items()
                       if Ref.parse(ref).alias not in invalidated and ref not in revised}
            if not rebuild_vectors:
                vectors.update(staged)
                if self.embedder is not None and any(
                    value.get(field) != self.embedder.capabilities.get(field)
                    for value in vectors.values() for field in ["space_id", "model_version", "preprocess_version"]
                ):
                    with self.repo.lock():
                        self.repo.state["index_dirty"] = True
                        self.repo.save_state()
                    raise IndexNotReady("Embedding model changed; explicitly rebuild the vector index")
            refs = {b["ref"] for b in blocks}
            media_segments = []
            for block in blocks:
                if block["kind"] == "unit" and block["ref"] not in vectors:
                    vectors[block["ref"]] = self._embed([{"kind": "text", "text": block["body"]}])[0]
                elif block["kind"] == "media":
                    alias = Ref.parse(block["ref"]).alias
                    cached = [ref for ref in vectors if Ref.parse(ref).alias == alias]
                    if not cached or rebuild_vectors:
                        items = self._media_items(block["ref"])
                        cached = [ref for ref, _ in items]
                        vectors.update(zip(cached, self._embed([item for _, item in items])))
                    for ref in cached:
                        refs.add(ref)
                        if ref != block["ref"]:
                            segment = dict(block, ref=ref)
                            media_segments.append(segment)
            vectors = {ref: value for ref, value in vectors.items() if ref in refs}
            with self.repo.lock():
                if epoch != self.repo.state["deletion_epoch"] or any(
                    self.repo.revision(alias) != revision for alias, revision in revisions.items()
                ):
                    raise RevisionConflict("Files changed during index reconstruction")
                self.repo.state["index_dirty"] = True
                self.repo.state["staged_vectors"] = vectors
                self.repo.save_state()
                self.index.sync(list(self.repo.catalog.values()), blocks + media_segments, vectors)
                self.repo.state["index_dirty"] = False
                self.repo.state["staged_vectors"] = {}
                for alias, revision in invalidated.items():
                    if self.repo.state.get('media_index_invalidated', {}).get(alias) == revision:
                        self.repo.state['media_index_invalidated'].pop(alias, None)
                for ref, revision in revised.items():
                    if self.repo.state.get('unit_index_invalidated', {}).get(ref) == revision:
                        self.repo.state['unit_index_invalidated'].pop(ref, None)
                self.repo.save_state()
        return {"status": "done", "blocks": len(blocks), "vectors": len(vectors)}
