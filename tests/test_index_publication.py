"""Focused parsed-snapshot and concurrent index publication checks; synthetic data only."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from memu.index import PostgresIndex
from memu.postgres_repository import PostgresRepository
from memu.service import MemoryService
from memu.storage import InvalidReference
from memu.worker import MaintenanceWorker
from test_reliability import Model, config, database  # noqa: F401
from test_service import SyntheticEmbedding
from test_storage import DAY, unit


def make_service(root, database):
    repo = PostgresRepository(root, database)
    return MemoryService(repo, config(), Model(), SyntheticEmbedding(), PostgresIndex(database))


def append_unit(service, source, text):
    with service._write_lock, service.repo.transaction():
        ref = service.repo.append_entries('unit', [unit(text, [source])], 'owner')[0]
        service.repo.state['staged_vectors'][ref] = service.embedder.embed([{'kind': 'text', 'text': text}])[0]
        service.repo.save_state()
        return ref


def block_publication(monkeypatch, index):
    entered, release = threading.Event(), threading.Event()
    original = index.sync
    def publish(*args, **kwargs):
        entered.set()
        assert release.wait(8), 'Test did not release index publication'
        return original(*args, **kwargs)
    monkeypatch.setattr(index, 'sync', publish)
    return entered, release


def test_parse_once_invalidation_and_isolated_results(database, tmp_path, monkeypatch):
    repo = PostgresRepository(tmp_path, database)
    source = repo.save_source('first\nsecond\nthird', 'owner', DAY)
    refs = repo.append_entries('unit', [unit(f'record {i} | for(;;)\\tail\nmore', [source]) for i in range(120)], 'owner')
    details = repo.append_entries('detail', [unit('detail ' + str(i), [source]) for i in range(20)], 'owner')
    path = repo.catalog[refs[0].split(':')[0]]['path']
    reads, parses = Counter(), Counter()
    read_bytes, parse = repo._bytes, repo._entries
    def counted_read(relative):
        reads[relative] += 1
        return read_bytes(relative)
    def counted_parse(text, alias, kind):
        parses[alias] += 1
        return parse(text, alias, kind)
    monkeypatch.setattr(repo, '_bytes', counted_read)
    monkeypatch.setattr(repo, '_entries', counted_parse)
    alias = refs[0].split(':')[0]
    repo._text_cache.clear(); repo._text_cache_size = 0
    rows = repo.iter_blocks(['unit', 'detail'])
    assert len(rows) == 140 and reads[path] == parses[alias] == 1
    rows[0]['facts'].clear()
    assert repo.read(refs[0])['facts'] == [source]
    assert repo.read(details[0])['body'] == 'detail 0'
    assert reads[path] == parses[alias] == 1
    later = repo.append_entries('unit', [unit('later', [source])], 'owner')[0]
    assert repo.read(later)['body'] == 'later'
    with pytest.raises(RuntimeError):
        with repo.transaction():
            aborted = repo.append_entries('unit', [unit('aborted', [source])], 'owner')[0]
            assert repo.read(aborted)['body'] == 'aborted'
            raise RuntimeError('rollback')
    with pytest.raises(InvalidReference):
        repo.read(aborted)
    repo.blank(refs[0])
    with pytest.raises(InvalidReference):
        repo.read(refs[0])
    # A same-length external edit must invalidate the cache without a catalog revision change.
    target = tmp_path / path
    target.write_text(target.read_text().replace('later', 'fresh'))
    assert repo.read(later)['body'] == 'fresh'
    repo.blank('c1:2-2')
    assert [r['ref'] for r in repo.iter_blocks(['source'])] == ['c1:1-1', 'c1:3-3']
    repo.close()


def test_publication_releases_write_lock_and_keeps_new_revisions(database, tmp_path, monkeypatch):
    service = make_service(tmp_path, database); repo = service.repo
    source = repo.save_source('selected evidence', 'owner', DAY)
    old = append_unit(service, source, 'old event')
    entered, release = block_publication(monkeypatch, service.index)
    def publish():
        with service._write_lock:  # Nested callers must release their outer lock too.
            return service.sync_index()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(publish)
        try:
            assert entered.wait(5)
            def admit_and_commit():
                service.memory_write(from_='owner', text='another durable Source')
                new = append_unit(service, source, 'new event')
                with repo.transaction():
                    alias = source.split(':')[0]
                    repo.catalog[alias]['metadata']['content'] = {'audit_only': True}
                    repo.state['dirty_files'][alias] = repo.revision(alias)
                    repo.save_state()
                return new
            new = pool.submit(admit_and_commit).result(timeout=5)
        finally:
            release.set()
        assert future.result(timeout=5)['status'] == 'pending'
    assert old.split(':')[0] in repo.state['dirty_files']
    assert source.split(':')[0] in repo.state['dirty_files']
    assert new in repo.state['staged_vectors']
    assert service.sync_index()['status'] == 'done'
    assert {r['ref'] for r in service.index.grep('event')} == {old, new}
    assert not service.index.grep('selected evidence', layers=['source'])
    repo.close()


def test_delete_during_publication_cannot_restore_erased_blocks(database, tmp_path, monkeypatch):
    service = make_service(tmp_path, database); repo = service.repo
    source = repo.save_source('erasable evidence', 'owner', DAY)
    ref = append_unit(service, source, 'erasable event')
    entered, release = block_publication(monkeypatch, service.index)
    committed = threading.Event()
    reconcile = service._reconcile_topics
    def on_delete():
        reconcile()
        committed.set()
    monkeypatch.setattr(service, '_reconcile_topics', on_delete)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(service.sync_index)
        try:
            assert entered.wait(5)
            deletion = pool.submit(service.memory_manage, from_='owner', action='delete', refs=[ref], with_source=True)
            assert committed.wait(5)
            with repo.lock():
                assert repo.state['deletion_epoch'] > 0
        finally:
            release.set()
        assert publication.result(timeout=5)['status'] == 'pending'
        assert deletion.result(timeout=5)['status'] == 'done'
    assert not service.index.grep('erasable', layers=['unit', 'source'])
    assert repo.state['index_dirty'] is False
    repo.close()


def test_source_priority_then_async_publication_does_not_occupy_model_slots(database, tmp_path, monkeypatch):
    service = make_service(tmp_path, database); repo = service.repo
    service.config.build.source_priority_backlog = 1
    for i in range(4):
        service.memory_write(from_='owner', text=f'计划 {i}', metadata={'content': {'sequential_relations': True}})
    entered, release = block_publication(monkeypatch, service.index)
    checked, finished = threading.Event(), threading.Event()
    priority = service.source_priority_deadline
    def check_priority():
        result = priority()
        checked.set()
        return result
    service.source_priority_deadline = check_priority
    original, results, lock = service.process_write, [], threading.Lock()
    def process(key, **kwargs):
        result = original(key, **kwargs)
        with lock:
            results.append(result)
            if len(results) == 4:
                finished.set()
        return result
    service.process_write = process
    service.refresh_due = lambda: None
    service.schedule_daily = lambda: None
    worker = MaintenanceWorker(service).start()
    try:
        assert checked.wait(5)
        assert not entered.is_set() and not results
        with repo.lock():
            repo.state['last_source_admission'] = 0
            repo.save_state()
        service.changed.set()
        assert entered.wait(5)
        assert finished.wait(5), 'Writes waited for index publication'
        assert all(r['status'] == 'done' for r in results)
        assert any(r['index_ready'] is False for r in results)
    finally:
        release.set()
        worker.stop()
    service.sync_index()
    assert len(service.index.grep('计划')) == 4 and not repo.state['index_dirty']
    repo.close()


def test_index_failure_defers_models_but_still_accepts_sources(database, tmp_path, monkeypatch):
    service = make_service(tmp_path, database)
    service.memory_write(from_='owner', text='first pending source')
    original = service.index.sync
    def fail(*args, **kwargs):
        raise ValueError('synthetic index failure')
    monkeypatch.setattr(service.index, 'sync', fail)
    with pytest.raises(ValueError):
        service.sync_index()
    assert service.prepare_writes(defer_index=True) == [] and service.runner.model.calls == []
    assert service.memory_write(from_='owner', text='source admitted during index repair')['status'] == 'accepted'
    monkeypatch.setattr(service.index, 'sync', original)
    assert service.sync_index()['status'] == 'done'
    assert service.repo.state.get('index_retry_at') is None
    assert service.prepare_writes(defer_index=True)
    service.repo.close()
