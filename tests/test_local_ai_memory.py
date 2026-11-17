"""The local-log failures observed in production; no real inference calls."""
import json

import pytest

from memu.acquisition.sources.local_ai_content import group_records, material_content
from memu.config import Config
from memu.tasks.contract import TaskRunner, TaskValidationError
from memu.postgres_repository import PostgresRepository
from memu.index import PostgresIndex
from memu.service import MemoryService
from test_reliability import database, config  # noqa: F401
from test_service import SyntheticEmbedding


def snapshot(value, n=0, metadata=None):
    return {'object_id': str(n), 'raw': json.dumps(value).encode(), 'metadata': metadata or {}}


def project(profile, values):
    rows = group_records([snapshot(value, i, {'session_id': 'session'}) for i, value in enumerate(values)], profile)
    return [material_content(row['raw'], profile) for row in rows]


def test_claude_context_replay_notification_and_user_answer_have_distinct_roles():
    base = {'sessionId': 'session', 'timestamp': '2026-06-12T07:53:19Z'}
    values = [
        {**base, 'type': 'user', 'uuid': '1', 'message': {'content': '请修复保存断点。'}},
        {**base, 'type': 'user', 'uuid': '2', 'isCompactSummary': True, 'message': {'content': '伪造的新用户决定'}},
        {**base, 'type': 'user', 'uuid': '3', 'message': {'content': '<task-notification><event>accuracy=0.652</event></task-notification>'}},
        {**base, 'type': 'assistant', 'uuid': '4', 'message': {'content': [{'type': 'tool_use', 'name': 'AskUserQuestion', 'id': 'q', 'input': {}}]}},
        {**base, 'type': 'user', 'uuid': '5', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'q', 'content': 'User has answered your questions: use four GPUs'}]}},
    ]
    materials = project('claude-code', values)
    assert len(materials) == 2  # Explicit answers start a decision turn; summaries/notifications do not.
    m = materials[0]
    assert '伪造的新用户决定' not in m['text']
    audit = m['transport_audit']
    assert any(json.loads(r['raw_utf8']).get('isCompactSummary') for r in audit['records'])
    assert [(e['role'], e['event_kind']) for m in materials for e in m['message_layout']] == [
        ('user', 'message'), ('tool', 'notification'), ('user', 'user_answer')]


def test_codex_canonical_messages_deduplicate_mirrors_without_replaying_context():
    def row(kind, payload):
        return {'type': kind, 'timestamp': '2026-06-12T07:53:19Z', 'payload': payload}
    values = [
        row('event_msg', {'type': 'user_message', 'message': '修复断点'}),
        row('response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '修复断点'}]}),
        row('response_item', {'type': 'message', 'role': 'developer', 'content': 'do-not-store-config'}),
        row('response_item', {'type': 'reasoning', 'summary': [{'text': 'private-reasoning'}]}),
        row('compacted', {'replacement_history': 'do-not-replay'}),
        row('response_item', {'type': 'function_call', 'name': 'exec_command', 'call_id': 'c', 'arguments': '{"cmd":"verify"}'}),
        row('response_item', {'type': 'function_call_output', 'call_id': 'c', 'output': '{"exit_code":0,"output":"verification passed"}'}),
        row('response_item', {'type': 'message', 'role': 'assistant', 'phase': 'final', 'content': [{'type': 'output_text', 'text': '断点已修复。'}]}),
        row('event_msg', {'type': 'agent_message', 'message': '断点已修复。'}),
    ]
    materials = project('codex', values)
    assert len(materials) == 1
    body = materials[0]['text']
    assert body.count('修复断点') == 1 and body.count('断点已修复。') == 1
    assert all(value not in body for value in ('do-not-store-config', 'private-reasoning', 'do-not-replay'))
    assert '工具返回（不是用户发言）' in body and 'verification passed' in body


def test_other_local_profiles_accept_explicit_messages_and_quarantine_unknown_state():
    cases = [
        ('gemini-cli', {'messages': [{'id': 'm', 'type': 'gemini', 'timestamp': '2026-06-12T07:00:00Z', 'content': '建议采用方案 B', 'thoughts': [{'text': 'private-reasoning'}]}]}),
        ('opencode', {'id': 'm', 'time_created': 1781247600000, 'data': json.dumps({'role': 'user', 'content': '采用方案 B'})}),
        ('cursor', {'key': 'bubbleId:m', 'value': json.dumps({'type': 1, 'createdAt': '2026-06-12T07:00:00Z', 'text': '采用方案 B'})}),
    ]
    for profile, value in cases:
        m = material_content(json.dumps(value).encode(), profile)
        assert '方案 B' in m['text'] and 'private-reasoning' not in m['text']
        assert not m['audit_only'] and m['message_layout'][0]['occurred_at']
        unknown = material_content(json.dumps({'opaque_internal_state': 'do-not-invent-an-event'}).encode(), profile)
        assert unknown['audit_only'] and 'do-not-invent-an-event' not in unknown['text']
    patch = {'$patch': {'id': 'm', 'content': '改用 C'}}
    m = material_content(json.dumps(patch).encode(), 'gemini-cli', {'semantic_messages': [
        {'id': 'm', 'type': 'gemini', 'content': 'old', 'timestamp': '2026-06-01T01:00:00Z'}]})
    assert '改用 C' in m['text'] and 'old' not in m['text']
    assert m['message_layout'][0]['occurred_at'] is None  # Patch has no new timestamp.


def test_claim_and_time_are_owned_by_the_anchor_not_the_model():
    cfg = Config.model_validate({'models': {'mllm': {'repair_attempts': 0}}})
    material = {'material_kind': 'agent_worklog', 'evidence_contract': 'local-ai-v1', 'topics': [],
                'items': [{'ref': 'c1:1', 'body': 'attempt update',
                           'provenance': {'role': 'assistant', 'event_kind': 'tool_call'}},
                          {'ref': 'c1:2', 'body': 'file updated',
                           'provenance': {'role': 'tool', 'event_kind': 'tool_result'}}],
                'context': {'recorded': [], 'memory': {'summary': '', 'facts': []}, 'memory_chars': 1000}}
    class Model:
        forged_claim = False
        def complete(self, request):
            props = request['output_schema']['$defs']['WorklogUnitDraft']['properties']
            assert 'time' not in props and 'claim' not in props
            return {'units': [{'body': '增加断点参数',
                              'topics': [], 'facts': [], 'anchor': 'c1:2',
                              **({'claim': 'user_statement'} if self.forged_claim else {})}],
                    'memory': {'summary': '已增加断点参数', 'facts': ['c1:2']}}
    model = Model()
    evidence = lambda ref: {'provenance': {'role': 'tool', 'event_kind': 'tool_result', 'occurred_at': '2026-06-12T07:53:19+00:00'}}
    result = TaskRunner(model, cfg).run('unit', material, {}, allowed_refs=['c1:1-2'], read_ref=evidence)
    assert result['units'][0]['time']['value'] == '2026-06-12T07:53:19+00:00'
    assert result['units'][0]['claim'] == 'tool_observation'
    assert result['units'][0]['facts'] == ['c1:2']
    assert result['coverage'][0]['ignored'] == 'operational_context'
    assert result['coverage'][1]['new'] == [0]
    model.forged_claim = True
    with pytest.raises(TaskValidationError, match='Extra inputs'):
        TaskRunner(model, cfg).run('unit', material, {}, allowed_refs=['c1:1-2'], read_ref=evidence)


def test_observation_time_survives_digest_unit_and_repository_restart(database, tmp_path):
    class Model:
        tasks = []
        def complete(self, request):
            task = request['task']; self.tasks.append(task)
            if task == 'relate':
                return {'relations': []}
            items = request['material']['items']; entries = []
            for i, item in enumerate(items):
                p = item['provenance']
                if p['event_kind'] == 'tool_call':
                    continue
                entries.append({'body': '已增加保存断点参数',
                                'facts': [item['ref']], 'anchor': item['ref'],
                                **({'topics': []} if task == 'unit' else {})})
            return {'units' if task == 'unit' else 'entries': entries,
                    'memory': {'summary': '已增加保存断点参数', 'facts': [items[-1]['ref']]}}
    values = [
        {'sessionId': 's', 'uuid': 'call', 'timestamp': '2026-06-12T07:53:10Z', 'type': 'assistant',
         'message': {'content': [{'type': 'tool_use', 'id': 'edit', 'name': 'Edit', 'input': {'new_string': 'x' * 4000}}]}},
        {'sessionId': 's', 'uuid': 'result', 'timestamp': '2026-06-12T07:53:19Z', 'type': 'user',
         'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'edit', 'content': 'The file has been updated successfully.'}]}},
    ]
    m = project('claude-code', values)[0]; cfg = config(); cfg.build.digest_threshold_chars = 100
    repo = PostgresRepository(tmp_path, database); model = Model()
    service = MemoryService(repo, cfg, model, SyntheticEmbedding(), PostgresIndex(database))
    accepted = service.memory_write(from_='owner', text=m['text'], time={'precision': 'day', 'value': '2026-06-01'},
                                    metadata={'content': {k:v for k,v in m.items() if k not in {'text','transport_audit'}}})
    result = service.process_write(accepted['request_id'])
    assert result['status'] == 'done', result
    assert {'digest', 'unit'} <= set(model.tasks)
    unit = result['units'][0]; repo.close(); repo = PostgresRepository(tmp_path, database)
    saved = repo.read(unit)
    assert saved['time']['value'] == '2026-06-12T07:53:19+00:00'
    assert saved['path'].startswith('2026/06/12/') and saved['provenance']['role'] == 'tool'
    # Audit-only receipts keep their metadata without creating empty Source records.
    service = MemoryService(repo, cfg, index=PostgresIndex(database))
    audit = service.memory_write(from_='owner', text='audit-only-sentinel', metadata={'content': {'audit_only': True}})
    assert service.process_write(audit['request_id'])['status'] == 'done'
    assert service.index.grep('audit-only-sentinel') == []
    assert audit['sources'] == []
    with repo.transaction():
        alias = accepted['sources'][0].split(':')[0]
        repo.catalog[alias]['metadata']['content']['superseded_by'] = 'local-ai-v2'
        repo.state['dirty_files'][alias] = repo.revision(alias)
        repo.state['index_dirty'] = True
        repo.save_state()
    service.sync_index()
    assert not any(hit['ref'].startswith(alias + ':') for hit in service.index.grep('updated successfully'))
    repo.close()
