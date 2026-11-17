"""Topic lifecycle tests use explicit model proposals, not claimed semantic evaluation."""
from copy import deepcopy

import pytest

from memu.config import Config
from memu.errors import AccessDenied
from memu.index import InMemoryIndex
from memu.service import MemoryService
from memu.storage import Repository
from memu.tasks.contract import TaskRunner, TaskValidationError
from tests.test_service import FixtureModel, SyntheticEmbedding
from tests.test_contracts import ScriptedMLLM


KEY = '本地模型部署与性能优化'


class DiscoveryModel(FixtureModel):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.fail_discovery = False
        self.before_proposal = None

    def complete(self, request):
        self.requests.append(deepcopy(request))
        if request['task'] == 'topics':
            self.calls['topics'] += 1
            if self.fail_discovery:
                raise RuntimeError('injected topic failure')
            if self.before_proposal:
                callback, self.before_proposal = self.before_proposal, None
                callback()
            definitions = request['material']['topics']
            key = next((topic['key'] for topic in definitions if topic['origin'] == 'user'), KEY)
            return {'topics': [{'key': key, 'description': '模型部署、硬件选择和并发调优',
                'facts': [unit['ref'] for unit in request['material']['items']]}]}
        return super().complete(request)


def make_service(tmp_path, model=None):
    config = Config.model_validate({'access': {'whitelist': {
        'owner': ['read', 'write'], 'agent': ['read', 'write']}, 'owners': ['owner']}})
    return MemoryService(Repository(tmp_path / 'memory'), config, model or DiscoveryModel(),
                         SyntheticEmbedding(), InMemoryIndex())


def write(service, text, day):
    result = service.memory_write(from_='owner', text=text, time={'precision': 'day', 'value': day})
    if result['status'] == 'accepted':
        result = service.process_write(result['request_id'])
    assert result['status'] == 'done'
    service.refresh_due(force=True)
    assert service.repo.state['topic_registry']['job']['state'] == 'done'
    return result['units']


def test_one_conversation_and_entity_tags_do_not_publish_runtime(tmp_path):
    service = make_service(tmp_path)
    write(service, '用户询问 A6000 的吞吐。\n助手提供了 Qwen 的性能估算。', '2026-10-14')
    assert not service.repo.iter_blocks(['runtime'])
    rollups = [request for request in service.runner.model.requests if request['task'] == 'rollup']
    assert all('detail' not in request['policy'] for request in rollups)
    assert all('topics' not in item and 'text' not in item for request in rollups for item in request['material'].get('items', []))
    monthly = next(request for request in rollups if request['scope']['view'] == 'month')
    assert 'items' not in monthly['material']
    assert {ref for entry in monthly['material']['batch_entries'] for ref in entry['facts']} == {'u1:2', 'u1:3'}
    topic = service.repo.state['topic_registry']['definitions'][KEY]
    assert topic['source_count'] == topic['day_count'] == 1 and not topic['active']
    assert service.repo.read(service.repo.view_ref('memory', 'global'))['facts'] == ['u1:2', 'u1:3']
    # A second source on the same day is still not long-range evidence.
    write(service, '用户比较了另一种运行配置。', '2026-10-14')
    assert not service.repo.iter_blocks(['runtime'])
    write(service, '用户隔天调整了并发配置。', '2026-10-16')
    runtimes = service.repo.iter_blocks(['runtime'])
    assert len(runtimes) == 1 and runtimes[0]['scope'] == KEY
    assert len(runtimes[0]['facts']) == 4
    assert not service.repo.state['index_dirty']


def test_owner_topic_backfills_and_tracks_without_creating_fictitious_units(tmp_path):
    service = make_service(tmp_path)
    first = write(service, '用户在两张显卡上试跑了本地模型。', '2026-10-14')
    paths = [service.repo.root / row['path'] for row in service.repo.catalog.values() if row['kind'] in {'source', 'unit'}]
    frozen = {path: path.read_bytes() for path in paths}
    with pytest.raises(AccessDenied):
        service.memory_manage(from_='agent', action='topic', scope='我的推理服务', text='部署及延迟优化')
    result = service.memory_manage(from_='owner', action='topic', scope='我的推理服务', text='部署及延迟优化')
    assert result['status'] == 'queued'
    service.refresh_due(force=True)
    row = service.memory_manage(from_='owner', action='topics')['topics'][0]
    assert row['origin'] == 'user' and row['unit_count'] == 1 and row['state'] == 'done'
    assert service.repo.read(row['ref'])['facts'] == first
    assert all(path.read_bytes() == data for path, data in frozen.items())
    second = write(service, '用户为接口增加了批处理。', '2026-10-15')
    assert set(service.repo.read(row['ref'])['facts']) == set(first + second)
    # Editing the scope performs a fresh classification, not a blind union with old membership.
    service.memory_manage(from_='owner', action='topic', scope='我的推理服务', text='只跟踪延迟优化')
    service.refresh_due(force=True)
    assert service.repo.state['topic_registry']['definitions']['我的推理服务']['description'] == '只跟踪延迟优化'


def test_failure_is_not_auto_retried_and_deleted_evidence_cannot_resurrect(tmp_path):
    service = make_service(tmp_path)
    first = write(service, '用户第一次测试并发。', '2026-10-14')
    second = write(service, '用户随后修改并发配置。', '2026-10-15')
    runtime = service.repo.iter_blocks(['runtime'])[0]['ref']
    service.memory_manage(from_='owner', action='delete', refs=second)
    assert not service.repo.iter_blocks(['runtime'])
    assert service.repo.read(runtime)['body'] == ''
    assert service.repo.read(service.repo.view_ref('memory', 'global'))['facts'] == first
    service.runner.model.fail_discovery = True
    service.memory_manage(from_='owner', action='rebuild', view='topics')
    calls = service.runner.model.calls['topics']
    service.refresh_due(force=True)
    assert service.runner.model.calls['topics'] == calls
    assert service.repo.state['topic_registry']['job']['state'] == 'failed'
    service.runner.model.fail_discovery = False
    service.memory_manage(from_='owner', action='retry', view='topics')
    assert service.repo.state['topic_registry']['job']['state'] == 'done'
    assert not any(ref in second for topic in service.repo.state['topic_registry']['definitions'].values() for ref in topic['members'])


def test_new_owner_scope_invalidates_in_flight_discovery(tmp_path):
    service = make_service(tmp_path)
    service.memory_write(from_='owner', text='用户记录一次测试。', time={'precision': 'day', 'value': '2026-10-14'})
    service.runner.model.before_proposal = lambda: service._set_topic('手动主题', '测试过程')
    service.refresh_due(force=True)
    registry = service.repo.state['topic_registry']
    assert registry['job']['state'] == 'queued' and KEY not in registry['definitions']
    service.refresh_due(force=True)
    registry = service.repo.state['topic_registry']
    assert registry['job']['state'] == 'done' and registry['definitions']['手动主题']['members']


def test_legacy_entity_views_are_retired_without_changing_units(tmp_path):
    service = make_service(tmp_path)
    refs = write(service, '用户咨询了一次 GPU 配置。', '2026-10-14')
    old = service.repo.write_view('runtime', 'GPU', [{'body': '旧的实体视图',
        'time': {'precision': 'unknown', 'value': None}, 'facts': refs}])
    service.memory_manage(from_='owner', action='rebuild', view='topics')
    assert not service.repo.iter_blocks(['runtime'])
    assert service.repo.read(old)['active'] is False
    assert service.repo.read(refs[0])['body'] == '用户咨询了一次 GPU 配置。'


def test_discovery_checkpoints_resume_after_restart_without_replaying_successful_batches(tmp_path):
    class FailSecondBatch(DiscoveryModel):
        def complete(self, request):
            if request['task'] == 'topics' and self.calls['topics'] == 1:
                self.fail_discovery = True
            return super().complete(request)
    service = make_service(tmp_path, FailSecondBatch())
    result = service.memory_write(from_='owner', text='\n'.join(f'用户记录部署测试步骤 {i}。' for i in range(34)),
        time={'precision': 'day', 'value': '2026-10-14'})
    service.refresh_due(force=True)
    registry = service.repo.state['topic_registry']
    assert registry['job']['state'] == 'failed' and len(registry['seen']) == 32
    resumed = MemoryService(Repository(service.repo.root), service.config, DiscoveryModel(), service.embedder, service.index)
    resumed.memory_manage(from_='owner', action='retry', view='topics')
    requests = [request for request in resumed.runner.model.requests if request['task'] == 'topics']
    assert len(requests) == 1 and len(requests[0]['material']['items']) == 2
    assert set(resumed.repo.state['topic_registry']['seen']) == set(result['units'])
    assert resumed.repo.state['topic_registry']['job']['state'] == 'done'


def test_topic_scope_and_summary_level_are_enforced():
    config = Config.model_validate({'models': {'mllm': {'repair_attempts': 0}}})
    proposal = {'topics': [{'key': KEY, 'description': '性能优化', 'facts': ['u2:2']}]}
    with pytest.raises(TaskValidationError, match='outside'):
        TaskRunner(ScriptedMLLM(proposal), config).run('topics', {'items': []}, {'refs': ['u1:2']},
            allowed_refs=['u1:2'], read_ref=lambda ref: {})
    entry = {'body': '长' * 101, 'time': {'precision': 'unknown', 'value': None}, 'facts': ['u1:2']}
    runner = TaskRunner(ScriptedMLLM({'agenda': [{'matter': '事项', 'refs': ['u1:2']}],
                                     'entries': [entry]}), config)
    with pytest.raises(TaskValidationError, match='matter-level'):
        runner.run('rollup', {'items': []}, {'view': 'month', 'refs': ['u1:2']},
                   allowed_refs=['u1:2'], read_ref=lambda ref: {})
