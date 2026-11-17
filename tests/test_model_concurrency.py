"""Independent model limits and concurrent commit safety, using synthetic providers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

import pytest

from memu.config import Config
from memu.index import PostgresIndex
from memu.postgres_repository import PostgresRepository
from memu.service import MemoryService
from memu.worker import MaintenanceWorker
from test_reliability import Model, config, database  # noqa: F401
from test_service import SyntheticEmbedding


class Probe:
    def __init__(self, parallel):
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(parallel)
        self.active = self.peak = self.calls = 0

    @contextmanager
    def call(self):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls += 1
        try:
            self.barrier.wait(timeout=5)
            yield
        finally:
            with self.lock:
                self.active -= 1


@pytest.mark.parametrize('mllm,embedding', [(2, 1), (1, 2)])
def test_independent_limits_dispatch_and_conversation_order(database, tmp_path, mllm, embedding):
    cfg = Config.from_environment({'MLLM_CONCURRENCY': str(mllm), 'EMB_CONCURRENCY': str(embedding)})
    cfg.access = config().access
    model_probe, emb_probe = Probe(mllm), Probe(embedding)
    class ParallelModel(Model):
        def complete(self, request):
            with model_probe.call():
                return super().complete(request)
    class ParallelEmbedding(SyntheticEmbedding):
        def embed(self, items):
            with emb_probe.call():
                return super().embed(items)
    repo = PostgresRepository(tmp_path, database)
    service = MemoryService(repo, cfg, ParallelModel(), ParallelEmbedding(), PostgresIndex(database))
    accepted = [service.memory_write(from_='owner', text=f'用户确定第 {i} 项计划。',
                time={'precision': 'instant', 'value': f'2026-11-16T01:00:0{i}+00:00'},
                metadata={'content': {'sequential_relations': True, 'conversation_id': 'one'}}) for i in range(6)]
    finished, outcomes, lock = threading.Event(), {}, threading.Lock()
    original = service.process_write
    def process(key, **kwargs):
        result = original(key, **kwargs)
        with lock:
            outcomes[key] = result
            if len(outcomes) == 6:
                finished.set()
        return result
    service.process_write = process
    service.refresh_due = lambda: None
    service.schedule_daily = lambda: None
    worker = MaintenanceWorker(service).start()
    try:
        assert finished.wait(15), 'Concurrent queue did not drain'
    finally:
        worker.stop()
    assert all(row['status'] == 'done' for row in outcomes.values()), outcomes
    assert (model_probe.peak, emb_probe.peak) == (mllm, embedding)
    assert model_probe.calls == emb_probe.calls == 6
    refs = [outcomes[row['request_id']]['units'][0] for row in accepted]
    for index, ref in enumerate(refs):
        expected = [{'rel': '延续', 'target': refs[index - 1]}] if index else []
        assert repo.read(ref)['relations'] == expected
    repo.close()
    restored = PostgresRepository(tmp_path, database)
    try:
        assert len(restored.iter_blocks(['unit'])) == 6
        assert all(restored.state['writes'][row['request_id']]['state'] == 'done' for row in accepted)
    finally:
        restored.close()


def test_duplicate_dispatch_and_deletion_during_inference(database, tmp_path):
    entered, release = threading.Event(), threading.Event()
    class BlockedModel(Model):
        blocked = False
        def complete(self, request):
            if self.blocked:
                entered.set()
                assert release.wait(5)
            return super().complete(request)
    repo = PostgresRepository(tmp_path, database)
    model = BlockedModel()
    service = MemoryService(repo, config(), model, SyntheticEmbedding(), PostgresIndex(database))
    seed = service.memory_write(from_='owner', text='用于执行删除操作的既有条目。')
    units = service.process_write(seed['request_id'])['units']
    model.calls.clear()
    model.blocked = True
    accepted = service.memory_write(from_='owner', text='待删除的合成材料。')
    key = accepted['request_id']
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.process_write, key)
        try:
            assert entered.wait(5)
            assert service.process_write(key)['status'] == 'running'
            service.memory_manage(from_='owner', action='delete', refs=units,
                                  with_source=True, source_refs=accepted['sources'])
        finally:
            release.set()
        assert future.result(timeout=5)['status'] == 'failed'
    assert not repo.iter_blocks(['unit'])
    assert repo.state['writes'][key]['deleted'] is True
    assert model.calls == ['unit']
    repo.close()


def test_appending_a_neighbor_does_not_invalidate_relation_evidence(database, tmp_path):
    entered, release = threading.Event(), threading.Event()
    class RelatedModel(Model):
        def complete(self, request):
            if request['task'] == 'relate' and request['material']['draft']['body'] == 'first':
                entered.set()
                assert release.wait(5)
            return super().complete(request)
    cfg = config()
    cfg.models.mllm.concurrency = 2
    repo = PostgresRepository(tmp_path, database)
    service = MemoryService(repo, cfg, RelatedModel(), SyntheticEmbedding(), PostgresIndex(database))
    def accept(text, sequential=False):
        return service.memory_write(from_='owner', text=text, time={'precision': 'day', 'value': '2026-11-16'},
            metadata={'content': {'sequential_relations': sequential}})['request_id']
    assert service.process_write(accept('seed', True))['status'] == 'done'
    first, second = accept('first'), accept('second')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.process_write, first)
        try:
            assert entered.wait(5)
            assert service.process_write(second)['status'] == 'done'
        finally:
            release.set()
        assert future.result(timeout=5)['status'] == 'done'
    assert len(repo.iter_blocks(['unit'])) == 3
    repo.close()
