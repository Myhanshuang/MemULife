"""Four small repository integrations, with explicitly synthetic model outputs.

These fixtures validate orchestration and persistence, not a real model's
language understanding, embedding quality, or multimodal capability.
"""

from collections import Counter
from copy import deepcopy
import hashlib

import pytest

from memu.config import Config
from memu.errors import AccessDenied, IndexNotReady
from memu.index import InMemoryIndex
from memu.refs import Ref
from memu.service import MemoryService
from memu.storage import InvalidReference, Repository


SOURCE_TIME = {"precision": "day", "value": "2026-11-13"}


def compact_fixture_output(request, output):
    """Synthetic bookkeeping for orchestration fixtures, not a summarizer."""
    if 'context' not in request['material']:
        return output
    items = request['material']['items']
    entries = output.get('entries', output.get('units'))
    return {**output, 'memory': {'summary': '已处理合成材料。', 'facts': [items[-1]['ref']]},
            'coverage': [{'item': i, 'new': [n for n, entry in enumerate(entries)
                if any(Ref.parse(item['ref']).contains(ref) for ref in entry['facts'])], 'previous': []}
                for i, item in enumerate(items)]}


class FixtureModel:
    """Prewritten test proposals; deliberately no production model behavior."""

    def __init__(self):
        self.calls = Counter()
        self.event_times = {}
        self.fail_rollup = False
        self.next_correction_body = None

    def complete(self, request):
        task, material = request['task'], request['material']
        self.calls[task] += 1
        if task == 'topics':
            return {'topics': [{'key': 'memory', 'description': '记忆系统项目',
                                'facts': [item['ref'] for item in material['items']]}]}
        if task == 'unit':
            if self.next_correction_body is not None:
                body, self.next_correction_body = self.next_correction_body, None
                return compact_fixture_output(request, {'units': [{'time': material['time'], 'body': body,
                                   'facts': [item['ref'] for item in material['items']],
                                   'topics': ['memory']}]})
            units = []
            for item in material['items']:
                ref = Ref.parse(item['ref'])
                for offset, body in enumerate(item['body'].splitlines()):
                    if not body.strip():
                        continue
                    fact = f'{ref.alias}:{ref.start + offset}'
                    units.append({'time': deepcopy(self.event_times.get(body, material['time'])),
                                  'body': body, 'facts': [fact], 'topics': ['memory']})
            return compact_fixture_output(request, {'units': units})
        if task == 'relate':
            candidates = material['candidates']
            return {'relations': [{'rel': '相关', 'target': candidates[0]['ref']}] if candidates else []}
        if task == 'rollup':
            if self.fail_rollup:
                raise RuntimeError('injected rollup provider failure')
            entries = {}
            for item in material.get('items', material.get('batch_entries', [])):
                if not item['body']:
                    continue
                entry = entries.setdefault(item['body'], {
                    'time': item.get('time', {'precision': 'unknown', 'value': None}),
                    'body': item['body'], 'facts': []})
                entry['facts'].extend([item['ref']] if 'ref' in item else item['facts'])
            rows = list(entries.values())
            maximum = request['scope'].get('max_entries', len(rows))
            if len(rows) > maximum:
                rows = [{'time': rows[-1]['time'], 'body': '\n'.join(row['body'] for row in rows),
                         'facts': list(dict.fromkeys(fact for row in rows for fact in row['facts']))}]
            limit = request['scope'].get('entry_chars', {'day': 160, 'month': 100}.get(request['scope']['view'], 400))
            for row in rows:
                row['body'] = row['body'][:limit]
            output = {'entries': rows}
            if 'agenda' in request['output_schema']['properties']:
                output['agenda'] = [{'matter': f'Fixture matter {i}', 'refs': row['facts']}
                                    for i, row in enumerate(rows)]
            return output
        raise AssertionError(f'This text-only fixture did not expect task {task}')


class SyntheticEmbedding:
    """Nonsemantic vectors for testing the required encode/recall path only."""

    capabilities = {'modalities': ['text'], 'dimension': 2, 'metric': 'cosine',
                    'space_id': 'test-only-synthetic', 'model_version': 'fixture-v1',
                    'preprocess_version': 'fixture-v1'}

    def __init__(self):
        self.calls = []

    def embed(self, items):
        self.calls.append(deepcopy(items))
        outputs = []
        for item in items:
            assert item['kind'] == 'text'
            value = hashlib.sha256(item['text'].encode()).digest()[0] / 255
            outputs.append({'vector': [1.0, value], 'space_id': 'test-only-synthetic',
                            'model_version': 'fixture-v1', 'preprocess_version': 'fixture-v1'})
        return outputs


class ObservedIndex(InMemoryIndex):
    def __init__(self):
        super().__init__()
        self.search_calls = 0
        self.fail_unit_sync = False

    def search(self, *args, **kwargs):
        self.search_calls += 1
        return super().search(*args, **kwargs)

    def sync(self, files, blocks, vectors):
        blocks = list(blocks)
        if self.fail_unit_sync and any(block['kind'] == 'unit' for block in blocks):
            raise RuntimeError('injected database outage after file commit')
        return super().sync(files, blocks, vectors)


class ImmediateWorkerService(MemoryService):
    """Legacy orchestration fixtures explicitly drain the now-asynchronous worker."""
    def memory_write(self, **arguments):
        result = super().memory_write(**arguments)
        return self.process_write(result['request_id']) if result['status'] == 'accepted' else result

    def memory_manage(self, **arguments):
        result = super().memory_manage(**arguments)
        return self.process_write(result['request_id']) if result.get('status') == 'accepted' else result


def service_fixture(tmp_path):
    config = Config.model_validate({
        'repository': {'timezone': 'Asia/Shanghai'},
        'access': {'whitelist': {'owner': ['read', 'write'], 'agent2': ['read', 'write'],
                                 'reader': ['read']}, 'owners': ['owner']},
    })
    repo = Repository(tmp_path / 'memory', timezone=config.repository.timezone)
    model, embedding, index = FixtureModel(), SyntheticEmbedding(), ObservedIndex()
    service = ImmediateWorkerService(repo, config, model, embedding, index)
    # These fixtures exercise the explicitly tracked project, not automatic discovery.
    service.memory_manage(from_='owner', action='topic', scope='memory', text='记忆系统项目')
    return service, model, embedding, index


def read(service, ref):
    return service.memory_read(from_='owner', refs=[ref])['results'][0]


def test_pipeline_idempotency_distinct_sources_and_view_drilldown(tmp_path):
    service, model, embedding, index = service_fixture(tmp_path)
    text = '用户计划下月停用 A，目前只是已测试 B。'
    first = service.memory_write(from_='owner', text=text, time=SOURCE_TIME, idempotency_key='first')
    assert first['status'] == 'done' and len(first['units']) == 1
    assert index.search_calls == 1
    assert all(item['kind'] == 'text' and '>' not in item['text']
               for call in embedding.calls for item in call)
    before = deepcopy(model.calls)
    repeated = service.memory_write(from_='owner', text=text, time=SOURCE_TIME, idempotency_key='first')
    assert repeated['units'] == first['units'] and model.calls == before
    assert index.search_calls == 1
    due = {key: job['due'] for key, job in service.repo.state['pending_views'].items()}
    second = service.memory_write(from_='agent2', text=text, time=SOURCE_TIME, idempotency_key='first')
    assert second['units'] != first['units'] and index.search_calls == 2
    assert all(service.repo.state['pending_views'][key]['due'] == value for key, value in due.items())
    assert len(service.repo.iter_blocks(['unit'])) == 2
    with pytest.raises(AccessDenied):
        service.memory_write(from_='reader', text='denied')
    with pytest.raises(AccessDenied):
        service.memory_read(from_='unknown')
    assert read(service, 'MEMORY')['pending_update'] is True
    service.refresh_due(force=True)
    overview = read(service, 'MEMORY')
    assert overview['body'] and overview['pending_update'] is False
    runtime = read(service, overview['facts'][0])
    unit = read(service, runtime['facts'][0])
    source = read(service, unit['facts'][0])
    assert unit['body'] == source['body'] == text
    assert unit['time'] == SOURCE_TIME
    assert runtime['body'] == text  # Fixture merges the two literal expressions.
    assert set(runtime['facts']) == set(first['units'] + second['units'])
    service.config.read.default_method = 'semantic'
    result = service.memory_search(from_='owner', query='测试 B')
    assert {row['ref'] for row in result['results']} == set(first['units'] + second['units'])
    assert embedding.calls[-1][0]['purpose'] == 'query'


def test_late_month_and_unknown_event_dates_do_not_borrow_source_dates(tmp_path):
    service, model, _, _ = service_fixture(tmp_path)
    cases = [
        ('用户回忆 10 日完成了项目。', {'precision': 'day', 'value': '2026-11-10'},
         '2026/11/10/units.md'),
        ('用户只知道事情发生在 11 月。', {'precision': 'month', 'value': '2026-11'},
         '2026/11/_undated/units.md'),
        ('用户不知道事件日期。', {'precision': 'unknown', 'value': None}, '_undated/units.md'),
    ]
    for body, event_time, expected_path in cases:
        model.event_times[body] = event_time
        result = service.memory_write(from_='owner', text=body, time=SOURCE_TIME)
        unit = read(service, result['units'][0])
        assert unit['path'] == expected_path and unit['time'] == event_time
        assert read(service, unit['facts'][0])['path'].startswith('2026/11/13/')
    jobs = list(service.repo.state['pending_views'].values())
    assert {job['scope'] for job in jobs if job['view'] == 'day'} == {'2026-11-10'}
    assert {job['scope'] for job in jobs if job['view'] == 'month'} == {'2026-11'}
    service.refresh_due(force=True)
    month = read(service, service.repo.view_ref('month', '2026-11'))
    assert len(month['facts']) == 2
    assert len(service.memory_search(from_='owner', query='用户',
                                    scope={'directory': '2026/11/10'})['results']) == 1


def test_source_fragment_deletion_suppresses_old_views_then_rebuild_and_correction(tmp_path):
    service, model, _, index = service_fixture(tmp_path)
    result = service.memory_write(from_='owner', text='用户记录旧决定。\n用户记录一并清除的内容。\n用户记录保留的独立事件。',
                                  time=SOURCE_TIME, idempotency_key='two-events')
    removed, shared, retained = result['units']
    source_fragment = read(service, removed)['facts'][0]
    overlapping_source = f'{Ref.parse(source_fragment).alias}:1-2'
    retained_line = read(service, retained)['start_line']
    service.refresh_due(force=True)
    views = [entry['ref'] for entry in result['views']]
    assert read(service, 'MEMORY')['body']
    model.fail_rollup = True
    service.memory_manage(from_='owner', action='delete', refs=[removed], with_source=True,
                          source_refs=[source_fragment, overlapping_source])
    with pytest.raises(InvalidReference):
        service.repo.read(removed)
    with pytest.raises(InvalidReference):
        service.repo.read(source_fragment)
    with pytest.raises(InvalidReference):
        service.repo.read(f'{Ref.parse(source_fragment).alias}:2')
    with pytest.raises(InvalidReference):
        service.repo.read(shared)
    assert read(service, retained)['start_line'] == retained_line
    assert removed not in index.vectors()
    for ref in views:
        record = read(service, ref)
        assert record['pending_update'] and record['withheld'] and record['body'] == ''
    model.fail_rollup = False
    service.memory_manage(from_='owner', action='retry')
    service.refresh_due(force=True)
    assert '旧决定' not in read(service, 'MEMORY')['body']
    model.next_correction_body = '用户明确更正为新的独立事件。'
    service.memory_manage(from_='owner', action='correct', refs=[retained],
                          text='用户明确更正为新的独立事件。', time=SOURCE_TIME)
    with pytest.raises(InvalidReference):
        service.repo.read(retained)
    remaining = service.repo.iter_blocks(['unit'])
    assert len(remaining) == 1 and remaining[0]['ref'] not in {removed, retained}
    assert remaining[0]['body'] == '用户明确更正为新的独立事件。'
    assert not service.repo.state['index_dirty']


def test_embedding_failure_retry_retains_drafts_but_revalidates_relations(tmp_path, monkeypatch):
    service, model, embedding, index = service_fixture(tmp_path)
    original_embed = embedding.embed
    calls = 0

    def fail_second_call(items):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('synthetic embedding outage')
        return original_embed(items)

    monkeypatch.setattr(embedding, 'embed', fail_second_call)
    failed = service.memory_write(from_='owner', text='用户提出第一件事。\n用户确认第二件事。',
                                  time=SOURCE_TIME, idempotency_key='embedding-outage')
    assert failed['status'] == 'failed'
    checkpoint = deepcopy(service.repo.state['writes'][failed['request_id']])
    assert len(checkpoint['drafts']) == 2 and len(checkpoint['processed']) == 1
    assert model.calls['unit'] == 1 and model.calls['relate'] == 1
    source_before = service.repo.read(failed['sources'][0])['body']
    resumed = service.memory_manage(from_='owner', action='retry', request_id=failed['request_id'])
    assert resumed['status'] == 'done' and len(resumed['units']) == 2
    assert model.calls['unit'] == 1 and model.calls['relate'] == 3
    assert index.search_calls == 3
    assert service.repo.state['writes'][failed['request_id']]['drafts'] == checkpoint['drafts']
    assert service.repo.read(failed['sources'][0])['body'] == source_before


def test_committed_files_repair_index_after_outage_without_regenerating_models(tmp_path):
    service, model, embedding, index = service_fixture(tmp_path)
    index.fail_unit_sync = True
    result = service.memory_write(from_='owner', text='用户确认保留已提交的事件。',
                                  time=SOURCE_TIME, idempotency_key='recover-me')
    assert result['status'] == 'done' and result['index_ready'] is False
    assert len(service.repo.iter_blocks(['unit'])) == 1
    calls, encodes, recalls = deepcopy(model.calls), len(embedding.calls), index.search_calls
    with pytest.raises(IndexNotReady):
        service.memory_search(from_='owner', query='事件')
    reopened = MemoryService(Repository(service.repo.root), service.config, model, embedding, index)
    repeated = reopened.memory_write(from_='owner', text='用户确认保留已提交的事件。',
                                     time=SOURCE_TIME, idempotency_key='recover-me')
    assert repeated['units'] == result['units']
    index.fail_unit_sync = False
    reopened.sync_index()
    assert model.calls == calls and len(embedding.calls) == encodes and index.search_calls == recalls
    assert len(reopened.repo.iter_blocks(['unit'])) == 1
    assert reopened.memory_search(from_='owner', query='已提交')['results'][0]['ref'] == result['units'][0]
