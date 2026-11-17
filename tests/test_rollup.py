"""Regressions from the real day preview: clipped prose and one item per Unit."""
from copy import deepcopy
import json

import pytest

from memu.config import Config
from memu.tasks.contract import TaskRunner, TaskValidationError


class Proposals:
    def __init__(self, *outputs):
        self.outputs = iter(outputs)
        self.requests = []

    def complete(self, request):
        self.requests.append(deepcopy(request))
        return deepcopy(next(self.outputs))


def entry(body, fact='u89:58'):
    return {'time': {'precision': 'day', 'value': '2026-03-10'}, 'body': body, 'facts': [fact]}


def summary(entries):
    return {'agenda': [{'matter': f'事项 {i}', 'refs': row['facts']} for i, row in enumerate(entries)],
            'entries': entries}


@pytest.mark.parametrize('view,count,chars,total', [('day', 10, 500, 1600), ('month', 8, 100, 800)])
def test_rollup_budget_is_for_matters_not_input_unit_count(view, count, chars, total):
    refs = [f'u89:{i}' for i in range(1, 61)]
    scope = {'view': view, 'refs': refs, 'max_entries': 60}
    request = TaskRunner(None, Config()).prepare('rollup', {'items': []}, scope)
    assert scope['max_entries'] == 60  # Preparing a request cannot mutate caller state.
    assert request['scope']['max_entries'] == count
    assert request['scope']['entry_chars'] == chars
    assert request['scope']['total_chars'] == total
    if view == 'day':
        assert request['scope']['entry_target_chars'] == 160
    schema = request['output_schema']
    assert schema['properties']['entries']['maxItems'] == count
    assert list(schema['properties']) == ['agenda', 'entries']
    assert schema['$defs']['SummaryMatter']['properties']['refs']['items']['enum'] == refs
    assert 'maxLength' not in schema['$defs']['Entry']['properties']['body']
    assert schema['$defs']['Entry']['properties']['facts']['items']['enum'] == refs
    assert schema['additionalProperties'] is False


def test_overlong_prose_is_repaired_whole_with_measured_feedback():
    body = '助手报告（未独立验证）：' + '依赖问题仍需确认，' * 60 + '下一步验证修复结果。'
    original = summary([entry(body)])
    corrected = summary([entry('助手报告已修复依赖问题（未独立验证），建议重启后验证。')])
    model = Proposals(original, corrected)
    config = Config.model_validate({'models': {'mllm': {'repair_attempts': 1}}})
    output = TaskRunner(model, config).run(
        'rollup', {}, {'view': 'day', 'refs': ['u89:58']},
        allowed_refs=['u89:58'], read_ref=lambda ref: {'body': 'evidence'})
    assert output == {'entries': corrected['entries']}  # Grouping is not published as memory.
    repair = json.loads(model.requests[1]['messages'][-1]['content'])
    assert repair['previous_proposal'] == original
    assert str(len(body)) in repair['validation_error'] and 'limit 500' in repair['validation_error']
    assert '不得截断正文' in repair['instruction']


@pytest.mark.parametrize('entries,scope,reason', [
    ([entry('相同事项。'), entry('相同事项。', 'u89:59')], {'view': 'day'}, 'summary_duplicate_matter'),
    ([entry('内容。', 'u90:1')], {'view': 'day'}, 'evidence_scope'),
    ([entry('正文。' * 167)], {'view': 'day'}, 'summary_matter_budget'),
    ([entry(str(i), f'u89:{i + 1}') for i in range(11)], {'view': 'day'}, 'summary_entry_count'),
    ([entry('甲' * 40), entry('乙' * 40)], {'view': 'runtime'}, 'runtime_budget'),
])
def test_rollup_still_rejects_budget_duplicates_and_outside_evidence(entries, scope, reason):
    config = Config.model_validate({'models': {'mllm': {'repair_attempts': 0}},
                                    'views': {'per_topic_chars': 64}})
    refs = list(dict.fromkeys(fact for row in entries for fact in row['facts']))
    if reason == 'evidence_scope':
        refs = ['u89:58']
    proposal = summary(entries) if scope['view'] == 'day' else {'entries': entries}
    with pytest.raises(TaskValidationError) as caught:
        TaskRunner(Proposals(proposal), config).run(
            'rollup', {}, {**scope, 'refs': refs}, allowed_refs=refs, read_ref=lambda ref: {'body': 'evidence'})
    assert caught.value.reason == reason


def test_small_configured_total_also_limits_single_entry():
    config = Config.model_validate({'views': {'day_summary_chars': 100}})
    scope = TaskRunner(None, config).prepare('rollup', {'view': 'day'}, {})['scope']
    assert (scope['max_entries'], scope['entry_chars'], scope['total_chars']) == (1, 100, 100)


@pytest.mark.parametrize('groups,rows', [
    ([['u89:58']], [entry('只看了开头。')]),
    ([['u89:58'], ['u89:58', 'u89:59']], [entry('一。'), entry('二。', 'u89:59')]),
    ([['u89:58'], ['u89:59']], [entry('错组引用。', 'u89:59'), entry('二。', 'u89:59')]),
])
def test_summary_requires_full_grouping_without_cross_matter_citations(groups, rows):
    proposal = {'agenda': [{'matter': '事项', 'refs': refs} for refs in groups], 'entries': rows}
    config = Config.model_validate({'models': {'mllm': {'repair_attempts': 0}}})
    refs = ['u89:58', 'u89:59']
    with pytest.raises(TaskValidationError) as caught:
        TaskRunner(Proposals(proposal), config).run(
            'rollup', {}, {'view': 'day', 'refs': refs}, allowed_refs=refs, read_ref=lambda ref: {})
    assert caught.value.reason == 'summary_coverage'
