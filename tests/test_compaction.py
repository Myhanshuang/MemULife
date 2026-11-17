from copy import deepcopy
import hashlib

import pytest

from memu import compaction
from memu.config import Config
from memu.service import MemoryService
from memu.storage import Repository
from memu.tasks.contract import TaskRunner, TaskValidationError
from test_contracts import ScriptedMLLM
from test_service import FixtureModel, SOURCE_TIME, compact_fixture_output, service_fixture


def test_media_coverage_bookkeeping_can_be_repaired_only_with_complete_citations():
    refs = ['a1', 's1:2', 's1:3', 's1:4', 'c1:1-5']
    material = {'items': [{'ref': ref} for ref in refs],
                'context': {'recorded': [], 'memory_chars': 1000}}
    output = {'units': [{'facts': ['a1', 's1:2', 'c1:1-5']},
                        {'facts': ['a1', 's1:3']}, {'facts': ['a1', 's1:4']}],
              'memory': {'summary': 'Current image and its accompanying context.', 'facts': refs},
              'coverage': [{'item': i, 'new': [i], 'previous': []} for i in range(3)]}
    original_facts = deepcopy(output['units'])
    compaction.normalize_cited_coverage(output, material)
    compaction.validate_coverage(output, material)
    assert len(output['coverage']) == len(refs)
    assert output['units'] == original_facts

    missing_evidence = deepcopy(output)
    missing_evidence['units'][0]['facts'].remove('c1:1-5')
    compaction.normalize_cited_coverage(missing_evidence, material)
    with pytest.raises(ValueError, match='must cite its current input'):
        compaction.validate_coverage(missing_evidence, material)


def test_configured_input_window_is_independent_of_output_allowance():
    config = Config()
    config.build.task_batch_chars = 96000
    config.build.compact_context_chars = 12000
    config.build.digest_threshold_chars = 48000
    config.models.mllm.max_output_tokens = 8192
    limits = compaction.settings(config)
    assert limits['input_chars'] == 83968
    assert limits['context_chars'] == 12000
    assert limits['digest_threshold_chars'] == 48000


def test_media_unit_context_keeps_body_once_with_native_evidence():
    service = MemoryService.__new__(MemoryService)
    service.config = Config()
    service._media_items = lambda ref: [(ref, {'kind': 'image', 'data_base64': 'original'})]
    service._overlaps = lambda fact, ref: fact == ref
    source = {'ref': 'c1:1-562', 'kind': 'source', 'body': '文' * 9484, 'text': '文' * 9484,
              'time': SOURCE_TIME, 'facts': [], 'source': 'chatgpt'}
    caption = {'ref': 's1:2-2', 'kind': 'detail', 'body': '图片内容', 'text': 'serialized caption',
               'time': SOURCE_TIME, 'facts': ['a1']}
    batches = list(service._unit_batches([source, caption, {'ref': 'a1', 'kind': 'media'}],
                                        limit=12256, digest_threshold=12000))
    assert len(batches) == 1
    assert len(batches[0]) == 2
    item = batches[0][0]
    assert item['media']['data_base64'] == 'original'
    assert item['context'][0]['facts'] == ['a1']
    assert batches[0][1]['body'] == source['body']
    assert all('text' not in row for row in item['context'])
    context = {'memory': {'facts': []}, 'recorded': []}
    assert set(compaction.evidence_refs(batches[0], context)) == {'a1', 's1:2-2', 'c1:1-562'}
    assert compaction.size([{k: v for k, v in row.items() if k != 'media'} for row in batches[0]]) <= 12256


def entry(body='用户计划周五发布 A。', facts=None):
    return {'body': body, 'time': SOURCE_TIME, 'facts': facts or ['c1:1']}


def material():
    return {'items': [{'ref': 'c1:2', 'body': '重复同一计划。'}], 'context': {
        'memory': {'summary': '用户尚未发布 A，计划周五发布。', 'facts': ['c1:1']},
        'recorded': [{'id': 'D1', **entry(), 'excerpt': False}], 'memory_chars': 400}}


def run_output(output, value=None):
    value = material() if value is None else value
    return TaskRunner(ScriptedMLLM(output), Config()).run('digest', value, {},
        allowed_refs=compaction.evidence_refs(value['items'], value['context']),
        read_ref=lambda ref: {'ref': ref, 'body': 'synthetic evidence', 'facts': []})


def repeated_output():
    return {'entries': [], 'memory': material()['context']['memory'],
            'coverage': [{'item': 0, 'new': [], 'previous': ['D1']}]}


def test_repeat_requires_explicit_existing_record_and_complete_coverage():
    assert run_output(repeated_output())['entries'] == []
    changes = [
        {'coverage': []},
        {'coverage': [{'item': 0, 'new': [], 'previous': []}]},
        {'coverage': [{'item': 0, 'new': [], 'previous': ['D99']}]},
        {'coverage': [{'item': 1, 'new': [], 'previous': ['D1']}]},
        {'coverage': [{'item': 0, 'new': [0], 'previous': []}]},
        {'memory': {'summary': '过' * 401, 'facts': ['c1:1']}},
        {'memory': {'summary': '捏造来源', 'facts': ['c9:1']}},
        {'entries': [entry()], 'coverage': [{'item': 0, 'new': [0], 'previous': []}]},
    ]
    for change in changes:
        with pytest.raises(TaskValidationError):
            run_output({**repeated_output(), **change})
    value = material(); value['context']['recorded'] = []
    with pytest.raises(TaskValidationError, match='discarded'):
        run_output(repeated_output(), value)


def test_new_correction_cites_current_input_and_can_retain_old_context():
    value = material(); value['items'][0]['body'] = '取消这个计划，改为下周评估。'
    output = {'entries': [entry('用户取消 A 周五发布的计划，改为下周评估。', ['c1:1', 'c1:2'])],
              'memory': {'summary': 'A 周五发布计划已取消；下周评估。', 'facts': ['c1:1', 'c1:2']},
              'coverage': [{'item': 0, 'new': [0], 'previous': []}]}
    assert run_output(output, value)['entries'][0]['facts'] == ['c1:1-2']
    request = TaskRunner(None, Config()).prepare('digest', value, {})
    assert request['output_schema']['properties']['entries']['minItems'] == 0
    assert request['output_schema']['$defs']['MaterialCoverage']['properties']['previous']['items']['enum'] == ['D1']
    model = ScriptedMLLM(output)
    TaskRunner(model, Config()).run('digest', value, {}, allowed_refs=['c1:1-2'], read_ref=lambda ref: {})
    assert model.requests[0]['tools'] == []  # No readback loop expands the short window.


def test_context_is_bounded_and_retrieves_older_relevant_memory_without_replaying_raw():
    stage = compaction.new_stage('fixture')
    stage['memory'] = {'summary': '仍有未结束的发布计划。', 'facts': ['c1:1']}
    stage['ledger'] = [{'id': f'D{i}', 'entry': entry('旧事件' + str(i), [f'c1:{i}']),
                        'origins': [], 'refs': [f's1:{i}']} for i in range(1, 201)]
    stage['ledger'][0]['entry']['body'] = 'EvoScientist SQLite sessions 数据库存储计划'
    before = deepcopy(stage)
    batch = [{'ref': 'c1:201', 'body': 'EvoScientist SQLite sessions 计划后续'}]
    context = compaction.context_for(stage, 'digest', batch, 1600)
    assert compaction.size(context) <= 1600
    assert context['recorded'][0]['id'] == 'D1'
    assert 0 < len(context['recorded']) <= 4
    assert stage == before and len(stage['ledger']) == 200
    assert '旧事件100' not in compaction.encoded(context)


def test_exact_repetition_is_collapsed_but_new_time_or_evidence_is_retained():
    stage = compaction.new_stage('fixture')
    batch = [{'ref': 'c1:1', 'body': '计划'}]
    output = {'entries': [entry(), entry()], 'memory': {'summary': '计划', 'facts': ['c1:1']},
              'coverage': [{'item': 0, 'new': [0, 1], 'previous': []}]}
    stage, added = compaction.advance(stage, 'digest', batch, output)
    assert len(added) == len(stage['ledger']) == 1
    assert stage['coverage'][0]['items'][0]['records'] == ['D1']
    later = entry(facts=['c1:2']); later['time'] = {'precision': 'day', 'value': '2026-11-14'}
    output['entries'] = [entry(), later]
    stage, added = compaction.advance(stage, 'digest', batch, output)
    assert len(added) == 1 and len(stage['ledger']) == 2


class RollingFixture(FixtureModel):
    def __init__(self):
        super().__init__()
        self.digest_inputs, self.unit_inputs = [], []
        self.fail_digest = self.fail_unit = True

    def complete(self, request):
        task, value = request['task'], request['material']
        if task not in {'digest', 'unit'}:
            return super().complete(request)
        context = value['context']
        assert compaction.size(context) <= 1000
        assert request['tools'] == []
        assert len(value['items']) == 1
        item = value['items'][0]
        if task == 'digest':
            self.digest_inputs.append(deepcopy(value))
            if context['recorded'] and self.fail_digest:
                self.fail_digest = False
                raise RuntimeError('synthetic interruption during digest')
            if '重复' in item['body']:
                assert context['memory']['summary'] == 'A 计划周五发布。'
                return {'entries': [], 'memory': context['memory'],
                        'coverage': [{'item': 0, 'new': [], 'previous': ['D1']}]}
            text = '用户取消 A 周五发布计划。' if '取消' in item['body'] else '用户计划周五发布 A。'
            summary = 'A 计划已取消。' if '取消' in item['body'] else 'A 计划周五发布。'
            return {'entries': [entry(text, [item['ref']])],
                    'memory': {'summary': summary, 'facts': [item['ref']]},
                    'coverage': [{'item': 0, 'new': [0], 'previous': []}]}
        self.unit_inputs.append(deepcopy(value))
        if context['recorded'] and self.fail_unit:
            self.fail_unit = False
            raise RuntimeError('synthetic interruption during unit')
        return compact_fixture_output(request, {'units': [{**entry(item['body'], [item['ref']]), 'topics': ['memory']}]})


def test_restart_resumes_memory_and_deltas_atomically_without_repeating_completed_batches(tmp_path):
    service, _, embedding, index = service_fixture(tmp_path)
    service.config.build.task_batch_chars = 4000
    service.config.build.unit_batch_items = 1
    service.config.build.digest_threshold_chars = 100
    model = RollingFixture(); service.runner = TaskRunner(model, service.config)
    original = '\n\n'.join([word + '甲' * 2480 for word in ['计划', '重复', '取消']])
    failed = service.memory_write(from_='owner', text=original, time=SOURCE_TIME, idempotency_key='rolling')
    key = failed['request_id']
    assert failed['status'] == 'failed' and 'during digest' in failed['error']
    assert service.repo.state['writes'][key]['compact']['stages']['digest']['next'] == 1
    source = service.repo.root / service.repo.read(failed['sources'][0])['path']
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    # A new service instance reads only the durable checkpoint.
    service = MemoryService(Repository(service.repo.root), service.config, model, embedding, index)
    service.config.build.unit_batch_items = 5
    service.config.build.task_batch_chars = 1200
    service.config.build.digest_threshold_chars = 99999
    second = service.memory_manage(from_='owner', action='retry', request_id=key)
    assert second['status'] == 'failed' and 'during unit' in second['error']
    state = deepcopy(service.repo.state['writes'][key]['compact']['stages'])
    assert state['digest']['next'] == 3 and len(state['digest']['ledger']) == 2
    assert state['digest']['coverage'][1]['items'][0]['records'] == ['D1']
    assert state['unit']['next'] == 1 and len(state['unit']['ledger']) == 1
    assert len(service.repo.iter_blocks(['detail'])) == 2
    service = MemoryService(Repository(service.repo.root), service.config, model, embedding, index)
    done = service.memory_manage(from_='owner', action='retry', request_id=key)
    assert done['status'] == 'done' and len(done['units']) == 2
    assert len(model.digest_inputs) == 4 and len(model.unit_inputs) == 3
    assert model.digest_inputs[1]['context'] == model.digest_inputs[2]['context']
    assert model.unit_inputs[1]['context'] == model.unit_inputs[2]['context']
    assert source.read_text() == original and hashlib.sha256(source.read_bytes()).hexdigest() == checksum
    assert len(service.repo.state['writes'][key]['digests']) == 3
    assert service.repo.state['writes'][key]['compact']['stages']['unit']['next'] == 2
    service.memory_manage(from_='owner', action='delete', refs=[done['units'][0]])
    assert 'compact' not in service.repo.state['writes'][key]


def test_invalid_compaction_does_not_commit_or_advance(tmp_path):
    service, _, _, _ = service_fixture(tmp_path)
    service.config.build.digest_threshold_chars = 1
    class BadMemory:
        def complete(self, request):
            item = request['material']['items'][0]
            return {'entries': [entry(facts=[item['ref']])], 'memory': {'summary': '越界' * 5000, 'facts': [item['ref']]},
                    'coverage': [{'item': 0, 'new': [0], 'previous': []}]}
    service.runner = TaskRunner(BadMemory(), service.config)
    result = service.memory_write(from_='owner', text='原始记录完整保存', time=SOURCE_TIME)
    assert result['status'] == 'failed' and 'budget' in result['error']
    stage = service.repo.state['writes'][result['request_id']]['compact']['stages']['digest']
    assert stage['next'] == 0 and not stage['ledger']
    assert not service.repo.iter_blocks(['detail', 'unit'])
    assert service.repo.read(result['sources'][0])['body'] == '原始记录完整保存'
