"""Lossless message-aware batching with immutable original evidence and restart coverage."""
from copy import deepcopy
import hashlib
import json

import pytest

from memu.batching import encoded_size, text_portions
from memu.config import Config
from memu.memulife import snapshot_material
from memu.refs import Ref
from memu.tasks.contract import TaskRunner
from test_service import FixtureModel, SOURCE_TIME, service_fixture, compact_fixture_output


def conversation(texts):
    value = {'format': 'memulife.browser-conversation.v2', 'provider': 'gemini', 'native_conversation_id': 'c_test',
             'messages': [{'id': f'm{i}', 'role': 'user' if i % 2 == 0 else 'assistant', 'text': text,
                           'occurred_at': '2026-10-13T01:00:00.000Z'} for i, text in enumerate(texts)],
             'responses': ['FULL_SYNTHETIC_TRANSPORT'], 'time_basis': {'default': 'native_message_time'}}
    origin = {'adapter_id': 'browser.ai-history', 'source_profile': 'gemini',
              'source_instance_id': 'synthetic', 'source_namespace': 'fixture'}
    return snapshot_material(origin, {'object_id': 'c_test', 'raw': json.dumps(value).encode()})


def assert_coverage(record, pieces, budget):
    assert ''.join(piece['body'] for piece in pieces) == record['body']
    position = 0
    for piece in pieces:
        assert encoded_size([piece]) <= budget
        span = piece['source_slice']
        assert span == {'ref': record['ref'], 'start': position, 'end': position + len(piece['body'])}
        assert record['body'][span['start']:span['end']] == piece['body']
        position = span['end']


def test_whole_messages_pack_up_to_budget_without_rewriting_source(tmp_path):
    service, _, _, _ = service_fixture(tmp_path)
    service.config.build.task_batch_chars = 14000
    material = conversation(['a' * 6000, 'b' * 6000, 'c' * 6000, 'd' * 6000])
    original = '\n'.join(material['text'].splitlines())
    ref = service.repo.save_source(original, 'owner', material['time'], material['metadata'])
    record = service.repo.read(ref)
    pieces = service._text_portions(record)
    assert [[m['id'] for m in p['messages']] for p in pieces] == [['m0', 'm1'], ['m2', 'm3']]
    assert_coverage(record, pieces, 14000)
    assert (service.repo.root / record['path']).read_text() == original
    assert service.repo.catalog[Ref.parse(ref).alias]['metadata']['memulife']['transport_audit']['responses'] == ['FULL_SYNTHETIC_TRANSPORT']
    for piece in pieces:
        assert service.repo.read(piece['ref'])['body'] == piece['body'].removesuffix('\n')


@pytest.mark.parametrize('text', ['长' * 409642, '\\"\t\x01🙂' * 10000, '段落一。' * 180 + '\n\n' + '第二段很长。' * 1000])
def test_single_long_message_is_complete_bounded_and_keeps_its_identity(text):
    material = conversation([text])
    body = '\n'.join(material['text'].splitlines())
    record = {'kind': 'source', 'ref': f'c1:1-{len(body.splitlines())}', 'body': body}
    pieces = text_portions(record, 32000, material['metadata']['memulife']['message_layout'])
    assert_coverage(record, pieces, 32000)
    assert all(piece['messages'][0]['id'] == 'm0' and piece['messages'][0]['role'] == 'user' for piece in pieces)
    assert all(piece['messages'][0]['occurred_at'] == '2026-10-13T01:00:00.000Z' for piece in pieces)


def test_message_like_content_does_not_create_false_boundaries():
    text = 'quoted:\n## 2. AI 助手\n消息 ID：fake\n消息时间：未知\n\n' + '甲' * 2100
    material = conversation([text, 'real answer'])
    body = '\n'.join(material['text'].splitlines())
    record = {'kind': 'source', 'ref': f'c1:1-{len(body.splitlines())}', 'body': body}
    pieces = text_portions(record, 1000, material['metadata']['memulife']['message_layout'])
    assert_coverage(record, pieces, 1000)
    assert {m['id'] for piece in pieces for m in piece['messages']} == {'m0', 'm1'}


def test_generic_text_prefers_paragraph_then_sentence_boundaries():
    body = '第一段。' * 35 + '\n\n' + ('a sentence. ' * 180) + '\n\n尾段'
    record = {'kind': 'source', 'ref': 'c1:10-14', 'body': body}
    pieces = text_portions(record, 700)
    assert_coverage(record, pieces, 700)
    assert pieces[0]['body'].endswith('\n\n')
    assert pieces[1]['body'].endswith('. ')
    assert pieces[0]['ref'].startswith('c1:10-')


class ChunkModel(FixtureModel):
    def __init__(self, service):
        super().__init__()
        self.service, self.completed, self.failed_once = service, [], False

    def complete(self, request):
        task, material = request['task'], request['material']
        if task == 'digest':
            self.calls[task] += 1
            # Original Source is durable before any chunk reaches the model.
            assert len([r for r in self.service.repo.catalog.values() if r['kind'] == 'source']) == 1
            item = material['items'][0]
            if len(self.completed) == 1 and not self.failed_once:
                self.failed_once = True
                raise RuntimeError('synthetic interruption after one committed chunk')
            self.completed.append(deepcopy(item))
            return compact_fixture_output(request, {'entries': [{'body': f'chunk {item["source_slice"]["start"]}', 'facts': [item['ref']], 'time': material['time']}]})
        if task == 'unit':
            self.calls[task] += 1
            return compact_fixture_output(request, {'units': [{'body': item['body'], 'facts': [item['ref']], 'topics': ['memory'], 'time': material['time']}
                              for item in material['items']]})
        return super().complete(request)


def test_retry_preserves_original_and_resumes_each_slice_of_same_long_line_once(tmp_path):
    service, _, _, _ = service_fixture(tmp_path)
    service.config.build.task_batch_chars = 1500
    service.config.build.digest_threshold_chars = 100
    model = ChunkModel(service); service.runner = TaskRunner(model, service.config)
    original = '甲' * 7000
    failed = service.memory_write(from_='owner', text=original, time=SOURCE_TIME, idempotency_key='same-original')
    assert failed['status'] == 'failed' and len(model.completed) == 1
    record = service.repo.read(failed['sources'][0])
    path = service.repo.root / record['path']; checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = service._text_portions(record, limit=service.repo.state['writes'][failed['request_id']]['compact']['input_chars'])
    assert len({p['ref'] for p in expected}) == 1 and len(expected) > 1
    resumed = service.memory_manage(from_='owner', action='retry', request_id=failed['request_id'])
    assert resumed['status'] == 'done'
    assert model.completed == expected
    assert 'digests' not in service.repo.state['writes'][failed['request_id']]  # Completed checkpoints are compacted.
    assert len(resumed['units']) == len(expected)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == checksum
    assert path.read_text() == original
    assert len([r for r in service.repo.catalog.values() if r['kind'] == 'source']) == 1
    before = deepcopy(model.calls)
    assert service.memory_write(from_='owner', text=original, time=SOURCE_TIME, idempotency_key='same-original')['status'] == 'done'
    assert model.calls == before


def test_existing_gemini_source_recovers_boundaries_from_original_audit(tmp_path):
    from memu.acquisition.sources.browser_content import material_content
    turns = []
    for i in reversed(range(3)):
        turns.append([['c_test', f'r{i}'], None, [['问' * 200]], [[[f'rc{i}', ['答' * 200]]]], [1791873000, 0]])
    wire = ")]}'\n" + json.dumps([['wrb.fr', 'hNvQHb', json.dumps([turns, None])]])
    audit = {'format': 'memulife.browser-response-set.v1', 'provider': 'gemini', 'native_conversation_id': 'c_test', 'responses': [wire]}
    value = material_content(json.dumps(audit).encode(), 'gemini', 'c_test')
    from memu.acquisition.migrate_content import migrate_metadata
    service, _, _, _ = service_fixture(tmp_path); service.config.build.task_batch_chars = 1200
    ref = service.repo.save_source('\n'.join(value['text'].splitlines()), 'owner', SOURCE_TIME,
                                  migrate_metadata({'memulife': {'object_id': 'c_test', 'transport_audit': audit}}, value['text']))
    record = service.repo.read(ref); pieces = service._text_portions(record)
    assert_coverage(record, pieces, 1200)
    ids = [m['id'] for p in pieces for m in p['messages']]
    assert ids == [m['id'] for m in value['message_layout']]


def test_default_and_environment_batch_budget_are_32k():
    assert Config().build.task_batch_chars == 32000
    assert Config.from_environment({'MEMU_TASK_BATCH_CHARS': '33000'}).build.task_batch_chars == 33000


def test_unit_batches_bound_digest_count_without_losing_or_slicing_records(tmp_path):
    service, _, _, _ = service_fixture(tmp_path)
    records = [{'kind': 'detail', 'ref': f's1:{i}-{i}', 'body': f'完整摘要 {i}'} for i in range(1, 29)]
    batches = list(service._unit_batches(records))
    assert [len(batch) for batch in batches] == [8, 8, 8, 4]
    assert [item['ref'] for batch in batches for item in batch] == [item['ref'] for item in records]
    assert [item['body'] for batch in batches for item in batch] == [item['body'] for item in records]
    assert service.config.build.task_batch_chars == 32000
    assert Config.from_environment({'MEMU_UNIT_BATCH_ITEMS': '4'}).build.unit_batch_items == 4


def test_nonempty_chunk_cannot_be_committed_as_an_empty_digest(tmp_path):
    service, _, _, _ = service_fixture(tmp_path)
    service.config.build.digest_threshold_chars = 10
    class EmptyDigest:
        def complete(self, request):
            assert request['output_schema']['properties']['entries']['minItems'] == 1
            return {'entries': []}
    service.runner = TaskRunner(EmptyDigest(), service.config)
    result = service.memory_write(from_='owner', text='synthetic original remains complete', time=SOURCE_TIME)
    assert result['status'] == 'failed'
    assert result['error']['code'] == 'task_validation_failed'
    assert not service.repo.iter_blocks(['unit', 'detail'])
    assert service.repo.read(result['sources'][0])['body'] == 'synthetic original remains complete'
