"""Key intake/receipt/index invariants only; no model endpoints or real imports."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import zstandard

from memu.acquisition.adapters.reader import IsolatedReader
from memu.acquisition.adapters.local.logs import CodexAdapter
from memu.acquisition.control_store import AcquisitionStore
from memu.acquisition.service import AcquisitionService
from memu.index import PostgresIndex
from memu.indexing import IndexMaintenance
from memu.concurrency import WriteLock
from memu.service import MemoryService
from test_index import fixture
from test_reliability import database  # noqa: F401


def test_source_pressure_releases_without_manual_resume(monkeypatch):
    service = object.__new__(MemoryService)
    backlog = [32]
    service.config = SimpleNamespace(build=SimpleNamespace(source_priority_backlog=32, source_priority_quiet_seconds=30))
    service.repo = SimpleNamespace(lock=nullcontext, state={'last_source_admission': 100},
                                  write_backlog_at_least=lambda threshold: backlog[0] >= threshold)
    monkeypatch.setattr('memu.service.clock.time', lambda: 110)
    assert service.source_priority_deadline() == 130
    backlog[0] = 31
    assert service.source_priority_deadline() is None
    backlog[0] = 32
    monkeypatch.setattr('memu.service.clock.time', lambda: 131)
    assert service.source_priority_deadline() is None


def test_index_publication_allows_intake_and_keeps_its_dirty_marker():
    lock = threading.RLock()
    catalog = {'c1': {'alias': 'c1', 'kind': 'source', 'revision': 'r1'}}
    state = {'dirty_files': {'c1': 'r1'}, 'deletion_epoch': 0, 'pending_views': {}, 'staged_vectors': {}}
    repo = SimpleNamespace(postgres=True, lock=lambda: lock, transaction=lambda: lock, catalog=catalog,
                           state=state, revision=lambda alias: catalog[alias]['revision'],
                           iter_blocks=lambda **kwargs: [], save_state=lambda: None,
                           db=SimpleNamespace(execute=lambda *args: None))
    def admit():
        with lock:
            catalog['c2'] = {'alias': 'c2', 'kind': 'source', 'revision': 'r2'}
            state['dirty_files']['c2'] = 'r2'
    def publish(*args, **kwargs):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(admit)
            # Avoid deadlocking test cleanup if the regression holds the lock.
            try:
                future.result(timeout=2)
            except TimeoutError:
                lock.release()
                future.result(timeout=2)
                raise AssertionError('Index publication blocked source intake') from None
    service = IndexMaintenance()
    service.repo, service._write_lock, service.changed = repo, WriteLock(), threading.Event()
    service._index_lock = threading.RLock()
    service.embedder = None
    service.index = SimpleNamespace(incremental=True, vectors=lambda _: {}, sync=publish)
    service.sync_index()
    assert state['dirty_files'] == {'c2': 'r2'} and state['index_dirty'] and service.changed.is_set()


def test_reader_reuses_process_and_decompression_without_owning_cursor(tmp_path):
    directory = tmp_path / 'sessions'; directory.mkdir()
    rows = [{'type': 'session_meta', 'payload': {'id': 's', 'source': 'cli'}}]
    rows += [{'type': 'event_msg', 'timestamp': '2026-11-16T01:00:00Z',
              'payload': {'type': 'user_message', 'message': f'choice {i}'}} for i in range(4)]
    path = directory / 'chosen.jsonl.zst'
    path.write_bytes(zstandard.ZstdCompressor().compress(('\n'.join(map(json.dumps, rows)) + '\n').encode()))
    adapter = CodexAdapter({'root': str(tmp_path)})
    first = adapter.read('backfill', limit=2)
    cached = adapter._decoded_cache
    adapter.read('backfill', first.cursor, limit=2)
    assert adapter._decoded_cache is cached
    reader = IsolatedReader('ai.local-logs', 'codex', {'root': str(tmp_path)}, persistent=True,
                            selection={'scopes': ['sessions/chosen.jsonl.zst']})
    try:
        first = reader.read('backfill', limit=2); process = reader.process
        second = reader.read('backfill', first.cursor, limit=2)
        assert reader.process is process
        replay = reader.read('backfill', first.cursor, limit=2)
        assert [r.object_id for r in second.records] == [r.object_id for r in replay.records]
    finally:
        reader.close()


def test_parallel_jobs_never_reserve_the_same_cursor_twice():
    service = object.__new__(AcquisitionService)
    service.lock, service.changed = threading.RLock(), threading.Event()
    service.inflight, service.readers = set(), {}
    service.state = {'sources': {'s': {'adapter_id': 'ai.local-logs'}}, 'jobs': {
        key: {'job_id': key, 'source_id': 's', 'state': 'queued', 'updated_at': 'same', 'receipts': {}}
        for key in ('a', 'b')}}
    barrier, selected = threading.Barrier(2), []
    def process(key):
        selected.append(key)
        barrier.wait(timeout=3)
        with service.lock:
            service.state['jobs'][key]['state'] = 'completed'
        return True
    service._run_job = process
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.run_once) for _ in range(2)]
        assert all(future.result(timeout=5) for future in futures)
    assert sorted(selected) == ['a', 'b'] and not service.inflight


def test_postgres_control_migration_and_receipt_update_are_local(database, tmp_path):
    outbox = tmp_path / 'outbox'; outbox.mkdir()
    original = {'version': 1, 'sources': {}, 'subscriptions': {}, 'jobs': {'j': {
        'job_id': 'j', 'state': 'processing', 'receipts': {}, 'progress': {'pages': 2, 'write_results': [
            {'request_id': 'one', 'status': 'done'}, {'request_id': 'two', 'status': 'queued'}]}}}}
    (tmp_path / 'control.json').write_text(json.dumps(original))
    (outbox / 'j.json').write_text(json.dumps({'cursor': {'offset': 50}, 'pending': None}))
    store = AcquisitionStore(tmp_path, database)
    try:
        assert not (tmp_path / 'control.json').exists() and not (outbox / 'j.json').exists()
        assert deepcopy(store.state) == original
        assert store.load_checkpoint('j')['cursor'] == {'offset': 50}
        before = {row['key']: row['tx'] for row in store.db.execute(
            'SELECT key,xmin::text AS tx FROM memu_acquisition_receipts ORDER BY key')}
        job_tx = store.db.execute("SELECT xmin::text AS tx FROM memu_acquisition_records WHERE namespace='jobs'").fetchone()['tx']
        store.state['jobs']['j']['progress']['write_results'][1]['status'] = 'done'
        store.save()
        after = {row['key']: row['tx'] for row in store.db.execute(
            'SELECT key,xmin::text AS tx FROM memu_acquisition_receipts ORDER BY key')}
        assert before['000000000000'] == after['000000000000']
        assert before['000000000001'] != after['000000000001']
        assert job_tx == store.db.execute("SELECT xmin::text AS tx FROM memu_acquisition_records WHERE namespace='jobs'").fetchone()['tx']
    finally:
        store.close()
    restored = AcquisitionStore(tmp_path, database)
    try:
        assert all(row['status'] == 'done' for row in restored.state['jobs']['j']['progress']['write_results'])
    finally:
        restored.close()


def test_incremental_blocks_keep_unrelated_rows_and_current_file_revision(database):
    index = PostgresIndex(database)
    files, blocks, vectors = fixture(index)
    with index._connect() as db:
        old = db.execute("SELECT xmin::text AS tx FROM blocks WHERE ref='u1:2'").fetchone()['tx']
        mapping = db.execute("SELECT xmin::text AS tx FROM unit_vector_targets WHERE unit_ref='u1:2' ORDER BY vector_ref").fetchall()
    changed = deepcopy(blocks[:2]); changed[1]['body'] = '用户修改了项目计划'
    unit_file = {**files[0], 'revision': 'rev2'}
    index.sync([unit_file], changed, {key: value for key, value in vectors.items() if key.startswith('u1:')}, full=False)
    with index._connect() as db:
        assert old == db.execute("SELECT xmin::text AS tx FROM blocks WHERE ref='u1:2'").fetchone()['tx']
        assert mapping == db.execute("SELECT xmin::text AS tx FROM unit_vector_targets WHERE unit_ref='u1:2' ORDER BY vector_ref").fetchall()
    assert index.grep('已测试')[0]['revision'] == 'rev2'
    detail = {**blocks[2], 'facts': ['a1@0-5']}
    index.sync([files[2]], [detail], {}, full=False)
    with index._connect() as db:
        targets = {row['vector_ref'] for row in db.execute("SELECT vector_ref FROM unit_vector_targets WHERE unit_ref='u1:3'")}
    assert 'a1@0-5' in targets and 'a1@10-20' not in targets
