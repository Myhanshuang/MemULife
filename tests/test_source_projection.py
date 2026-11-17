"""Focused projection regressions, with a disposable database and fake inference."""
import json
from copy import deepcopy

import pytest

from memu.acquisition.sources.chat_content import MAX_MESSAGES
from memu.acquisition.sources.local_ai_tools import shell_kind
from memu.memulife import snapshot_material, page_materials
from memu.postgres_repository import PostgresRepository
from memu.index import PostgresIndex
from memu.service import MemoryService
from memu.storage import InvalidReference
from test_local_ai_memory import project
from test_reliability import database, config  # noqa: F401
from test_service import SyntheticEmbedding


def source(profile='qq', adapter='qq.napcat'):
    return dict(adapter_id=adapter, source_profile=profile, source_instance_id='selected', source_namespace='local')


def message(n, conversation='room'):
    return dict(object_id=str(n), raw=json.dumps({'post_type': 'message', 'message_id': n}).encode(),
                text=f'甲: 决定 {n}', occurred_at='2026-06-12T07:00:00Z',
                metadata={'conversation_id': conversation, 'actor': {'display_name': '甲'}})


def test_adapter_text_structure_and_cross_page_chat_boundaries():
    m = snapshot_material(source(), message(1))
    assert m['text'] == '甲: 决定 1' and 'post_type' not in m['text']
    for metadata in ({'native_event_type': 'notice'}, {'kind': 'directory_listing'}, {'kind': 'folder'}):
        m = snapshot_material(source(), dict(object_id='structure', raw=b'{"items":[]}', metadata=metadata))
        assert m['metadata']['content']['audit_only']
    first = page_materials(source(), dict(records=[message(i) for i in range(20)], cursor={'page':2}, has_more=True, warnings=[]))
    second = page_materials(source(), dict(records=[message(i) for i in range(20,45)] + [message(99,'other')],
                                         cursor={'page':3}, has_more=False, warnings=[]), first['carry'])
    assert not first['requests']
    assert [len(m['metadata']['content']['message_layout']) for m in second['requests']] == [MAX_MESSAGES,5,1]
    assert len({m['idempotency_key'] for m in second['requests']}) == 3


@pytest.mark.parametrize('prefix', ['Your questions have been answered:', 'User has answered your questions:'])
def test_answers_and_tool_noise(prefix):
    rows = [dict(type='user', uuid='u', message={'content': '只允许四张 GPU，先暂停其他训练'})]
    for n, (name, args, result) in enumerate([
        ('Bash', {'command': "ssh host 'cd /work && ls | head'"}, 'read-only-sentinel'),
        ('Bash', {'command': 'until test -f ready; do echo waiting; sleep 2; done'}, 'poll-sentinel'),
        ('Edit', {'file_path': '/work/train.py', 'new_string': 'code-sentinel' * 2000}, 'updated successfully'),
        ('AskUserQuestion', {}, prefix + ' use four GPUs'),
    ]):
        rows += [dict(type='assistant', uuid=f'c{n}', message={'content': [dict(type='tool_use', name=name, id=str(n), input=args)]}),
                 dict(type='user', uuid=f'r{n}', message={'content': [dict(type='tool_result', tool_use_id=str(n), content=result)]})]
    rows = [dict(sessionId='s', cwd='/work', timestamp='2026-06-12T07:00:00Z', **row) for row in rows]
    materials = project('claude-code', rows)
    assert len(materials) == 2
    body = '\n'.join(m['text'] for m in materials)
    assert not any(marker in body for marker in ('poll-sentinel','code-sentinel'))
    assert 'read-only-sentinel' in body  # The model judges its value, rather than the tool class.
    assert any(e.get('tool_purpose') == 'read' for m in materials for e in m['message_layout'])
    assert '先暂停其他训练' in body and 'use four GPUs' in body
    assert materials[-1]['message_layout'][0]['event_kind'] == 'user_answer'
    assert body.count('工作目录：') == 2
    assert shell_kind('cd /work && rg needle . | head') == 'read'


class OneUnit:
    def __init__(self):
        self.calls = []

    def complete(self, request):
        self.calls.append(request['task'])
        assert request['task'] in {'unit', 'digest'}
        items = request['material']['items']
        user = next((item for item in items if item.get('provenance', {}).get('role') == 'user'), items[0])
        entry = dict(body='用户选择四张 GPU，助手报告已暂停其他训练，仍待执行验证。', anchor=user['ref'],
                     facts=[i['ref'] for i in items])
        if request['task'] == 'unit':
            entry['topics'] = []
        return {'units' if request['task'] == 'unit' else 'entries': [entry],
                'memory': {'summary': '选择四张 GPU，暂停其他训练。', 'facts': [user['ref']]}}


def local_material(n):
    from memu.acquisition.sources.local_ai_content import group_records
    row = dict(object_id=str(n), raw=json.dumps(dict(sessionId='s', type='user', uuid=str(n),
        timestamp=f'2026-06-12T07:{n:02d}:00Z', message={'content': '选择四张 GPU，暂停其他训练。'})).encode())
    return snapshot_material(source('claude-code','ai.local-logs'), group_records([row])[0])


def test_projection_replacement_and_continuations_survive_restart(database, tmp_path):
    repo = PostgresRepository(tmp_path, database)
    model = OneUnit(); cfg = config()
    service = MemoryService(repo, cfg, model, SyntheticEmbedding(), PostgresIndex(database))
    a, b, c = [local_material(n) for n in (1,2,3)]
    accepted = [service.memory_write(from_='owner', **m) for m in (a,b,c)]
    assert service.memory_status(from_='owner', idempotency_key=b['idempotency_key'])['request_id'] == accepted[1]['request_id']
    results = [service.process_write(r['request_id']) for r in accepted]
    assert all(r['status'] == 'done' and len(r['units']) == 1 for r in results), results
    assert 'relate' not in model.calls
    assert repo.read(results[2]['units'][0])['relations'][0]['target'] == results[1]['units'][0]
    newer = deepcopy(b)
    newer['text'] = newer['text'].replace('四张', '4 张')
    newer['metadata']['projection'].update(version='test-next-projection',
        generation=b['metadata']['projection']['generation'] + 1)
    replacement = service.memory_write(from_='owner', **newer)
    key = accepted[1]['request_id']
    assert replacement['request_id'] == key and replacement['status'] == 'accepted'
    assert service.memory_write(from_='owner', **b)['request_id'] == key
    repo.close(); repo = PostgresRepository(tmp_path, database)
    service = MemoryService(repo, cfg, model, SyntheticEmbedding(), PostgresIndex(database))
    service.recover_writes()
    replaced = service._current_result(repo.state['writes'][key])
    assert replaced['status'] == 'done' and len(replaced['units']) == 1, replaced
    with pytest.raises(InvalidReference):
        repo.read(results[1]['units'][0])
    assert repo.read(results[2]['units'][0])['relations'][0]['target'] == replaced['units'][0]
    assert repo.read(replaced['units'][0])['relations'][0]['target'] == results[0]['units'][0]
    assert service.memory_write(from_='owner', **b)['units'] == replaced['units']
    original = repo.original_audit(repo.state['writes'][key]['metadata'])
    assert '选择四张' in json.loads(original['records'][0]['raw_utf8'])['message']['content']
    assert len(list((tmp_path/'.meta/source-originals').glob('*.bin'))) == 3
    service.retire_projections([accepted[0]['request_id'], key], replaced_by='manual-rebuild')
    assert repo.read(results[2]['units'][0])['relations'] == []
    d = service.memory_write(from_='owner', **local_material(4))
    d = service.process_write(d['request_id'])
    service.memory_manage(from_='owner', action='delete', refs=results[2]['units'])
    assert repo.read(d['units'][0])['relations'] == []
    with pytest.raises(ValueError, match='Superseded'):
        service.memory_manage(from_='owner', action='retry', request_id=key)
    audit = snapshot_material(source(), dict(object_id='notice', raw=b'{"post_type":"notice"}', metadata={'native_event_type':'notice'}))
    admission = service.memory_write(from_='owner', **audit)
    service.model = service.embedder = None
    assert service.process_write(admission['request_id'])['units'] == []
    repo.close()
