"""Focused failure checks; no real model, user data or production database calls."""
import json
import os
from uuid import uuid4

import pytest

from memu.config import Config
from memu.index import PostgresIndex
from memu.models import OutputTruncated
from memu.postgres_repository import PostgresRepository
from memu.service import MemoryService
from memu.tasks.contract import TaskRunner
from memu.acquisition.sources.claude_content import group_records, material_content
from test_service import SyntheticEmbedding, compact_fixture_output


@pytest.fixture
def database():
    dsn = os.environ.get('MEMU_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('Disposable PostgreSQL database required')
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    schema = 'verify_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute('CREATE EXTENSION IF NOT EXISTS vector')
        conn.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        yield make_conninfo(dsn, options=f'-c search_path={schema},public')
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def config():
    return Config.model_validate({'access': {'whitelist': {'owner': ['read', 'write'], 'writer': ['write']},
                                            'owners': ['owner']}})


class Model:
    def __init__(self, truncate=False):
        self.calls = []
        self.truncate = truncate

    def complete(self, request):
        self.calls.append(request['task'])
        material = request['material']
        if request['task'] == 'relate':
            return {'relations': []}
        if self.truncate and any(len(item.get('body', '')) > 220 for item in material['items']):
            raise OutputTruncated('synthetic truncation')
        entries = [{'body': item['body'][:80], 'time': material['time'], 'facts': [item['ref']],
                    **({'topics': []} if request['task'] == 'unit' else {})} for item in material['items']]
        key = 'units' if request['task'] == 'unit' else 'entries'
        return compact_fixture_output(request, {key: entries})


def test_acceptance_restart_and_resource_deferral(database, tmp_path):
    repo = PostgresRepository(tmp_path, database)
    model, embed = Model(), SyntheticEmbedding()
    index = PostgresIndex(database)
    service = MemoryService(repo, config(), model, embed, index)
    service.sync_index()
    accepted = service.memory_write(from_='writer', text='用户决定先修复写入恢复。', idempotency_key='one')
    assert accepted['status'] == 'accepted' and model.calls == []
    assert repo.state['index_dirty'] is True
    assert service.memory_status(from_='writer', idempotency_key='one')['status'] == 'queued'
    assert service.memory_write(from_='writer', text='用户决定先修复写入恢复。', idempotency_key='one')['request_id'] == accepted['request_id']
    repo.close()
    repo = PostgresRepository(tmp_path, database)
    service = MemoryService(repo, config(), model, embed, index)
    original = embed.embed
    embed.embed = lambda _: (_ for _ in ()).throw(MemoryError('synthetic resource limit'))
    result = service.process_write(accepted['request_id'])
    assert result['status'] == 'waiting' and result['error']['code'] == 'resource_unavailable'
    unit_calls = model.calls.count('unit')
    embed.embed = original
    result = service.process_write(accepted['request_id'])
    assert result['status'] == 'done' and model.calls.count('unit') == unit_calls
    saved = repo.state['writes'][accepted['request_id']]
    assert 'drafts' not in saved and 'processed' not in saved and 'compact' not in saved
    assert len(repo.iter_blocks(['unit'])) == 1
    assert service.memory_search(from_='owner', query='恢复')['results']
    repo.close()


def test_file_publication_recovery_and_transaction_rollback(database, tmp_path, monkeypatch):
    repo = PostgresRepository(tmp_path, database)
    original = repo._replace_file
    def interrupted(path, data):
        if '/source/' in path:
            raise OSError('synthetic interruption after SQL commit')
        original(path, data)
    monkeypatch.setattr(repo, '_replace_file', interrupted)
    with pytest.raises(OSError):
        with repo.transaction():
            ref = repo.save_source('原文应在重启后发布。', 'owner', {'precision': 'unknown', 'value': None})
            repo.state['writes']['request'] = {'state': 'queued', 'source_refs': [ref]}
            repo.save_state()
    repo.close()
    reopened = PostgresRepository(tmp_path, database)
    assert reopened.read(ref)['body'] == '原文应在重启后发布。'
    assert reopened.state['writes']['request']['state'] == 'queued'
    with pytest.raises(ValueError):
        with reopened.transaction():
            reopened.state['writes']['request']['state'] = 'done'
            reopened.save_state()
            raise ValueError('abort')
    assert reopened.state['writes']['request']['state'] == 'queued'
    reopened.close()


def test_legacy_migration_preserves_addresses_and_externalizes_metadata(database, tmp_path):
    from memu.storage import Repository
    legacy = Repository(tmp_path)
    stamp = {'precision': 'day', 'value': '2026-11-13'}
    ref = legacy.save_source('原始对话', 'owner', stamp, {'audit': 'x' * 10000})
    with legacy.lock():
        legacy.state['writes'] = {'done': {'state': 'done', 'source_refs': [ref],
            'fingerprint': 'original', 'metadata': {'audit': 'x' * 10000}, 'result': {},
            'drafts': [{'body': 'completed'}], 'processed': []}}
        legacy.save_state()
    repository = PostgresRepository(tmp_path, database)
    assert repository.read(ref)['body'] == '原始对话'
    assert repository.read(ref)['time'] == stamp
    metadata = repository.catalog[ref.split(':')[0]]['metadata']
    assert len(json.dumps(metadata)) < 1000 and repository.full_metadata(metadata)['audit'] == 'x' * 10000
    assert 'drafts' not in repository.state['writes']['done']
    assert repository.state['writes']['done']['fingerprint'] == 'original'
    assert 'idempotency' not in repository.state
    assert repository.state['index_dirty'] is True
    repository.close()


def test_truncation_persists_binary_split_plan(database, tmp_path):
    repo = PostgresRepository(tmp_path, database)
    model = Model(truncate=True)
    service = MemoryService(repo, config(), model, SyntheticEmbedding(), PostgresIndex(database))
    accepted = service.memory_write(from_='owner', text='用户提出新的具体计划。' * 120)
    key = accepted['request_id']
    result = service.process_write(key)
    assert result['status'] == 'done', result
    assert model.calls.count('unit') > 1
    assert repo.iter_blocks(['unit'])
    repo.close()


def test_model_repairs_invalid_evidence_without_widening_scope():
    class Repair:
        calls = 0
        def complete(self, request):
            self.calls += 1
            if self.calls == 2:
                assert 'validation_error' in request['messages'][-1]['content']
            return {'units': [{'body': '用户计划发布。', 'time': {'precision': 'unknown', 'value': None},
                              'topics': [], 'facts': ['c9:1' if self.calls == 1 else 'c1:1']}]}
    model = Repair()
    output = TaskRunner(model, config()).run('unit', {'items': [{'ref': 'c1:1', 'body': '计划'}]}, {},
        allowed_refs=['c1:1'], read_ref=lambda ref: {'body': '计划'})
    assert model.calls == 2 and output['units'][0]['facts'] == ['c1:1']


def test_operational_worklog_can_produce_no_placeholder_unit():
    class Skip:
        def complete(self, request):
            assert request['output_schema']['properties']['units']['minItems'] == 0
            return {'units': [], 'coverage': [{'item': 0, 'new': [], 'previous': [], 'ignored': 'operational_context'}],
                    'memory': {'summary': '准备阅读代码，尚无实际修改。', 'facts': ['c1:1']}}
    material = {'items': [{'ref': 'c1:1', 'body': '准备阅读代码。'}], 'material_kind': 'agent_worklog',
                'topics': [], 'context': {'recorded': [], 'memory': {'summary': '', 'facts': []}, 'memory_chars': 1000}}
    result = TaskRunner(Skip(), config()).run('unit', material, {}, allowed_refs=['c1:1'],
                                             read_ref=lambda ref: {'body': '准备阅读代码。'})
    assert result['units'] == []


def test_provider_discovery_is_deferred_and_failure_does_not_poison_recovery():
    from memu.runtime import DeferredEmbedding
    calls = []
    class Adapter:
        capabilities = {'dimension': 768}
        def close(self):
            pass
    def factory():
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError('temporary provider outage')
        return Adapter()
    embedding = DeferredEmbedding(factory)
    assert calls == []
    with pytest.raises(TimeoutError):
        embedding.capabilities
    assert embedding.capabilities['dimension'] == 768 and len(calls) == 2


def test_claude_bookkeeping_and_tools_are_not_user_messages():
    records = [
        {'type': 'queue-operation', 'content': 'dequeue'},
        {'type': 'user', 'message': {'content': '请修复启动失败。'}},
        {'type': 'assistant', 'message': {'content': [{'type': 'thinking', 'signature': 'opaque-signature'}]}},
        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'call', 'name': 'Bash', 'input': {'command': 'check'}}]}},
        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'call', 'content': '启动验证通过'}]}},
        {'type': 'last-prompt', 'lastPrompt': '请修复启动失败。'},
        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'nav', 'name': 'Bash', 'input': {'command': 'ls -la'}}]}},
        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'nav', 'content': 'navigation-only-marker'}]}},
    ]
    snapshots = [{'object_id': str(i), 'raw': json.dumps({'sessionId': 'session', 'uuid': str(i), **row}).encode()}
                 for i, row in enumerate(records)]
    grouped = group_records(snapshots)
    material = [material_content(row['raw']) for row in grouped]
    text = '\n'.join(row['text'] for row in material)
    assert text.count('请修复启动失败') == 1
    assert 'opaque-signature' not in text and 'dequeue' not in text
    assert '工具返回（不是用户发言）' in text and '启动验证通过' in text
    # Read results remain bounded evidence for the model, with their tool role
    # preserved even when Claude's transport wraps them in a user message.
    navigation = [event for row in material for event in row['message_layout']
                  if 'navigation-only-marker' in '\n'.join(
                      row['text'].splitlines()[event['start_line'] - 1:event['end_line']])]
    assert len(navigation) == 1
    assert navigation[0]['role'] == 'tool' and navigation[0]['event_kind'] == 'tool_result'
    assert navigation[0]['tool_purpose'] == 'read'
    assert 'ls -la' not in text  # Command arguments stay in the raw audit.
    assert 'opaque-signature' in json.dumps(material, ensure_ascii=False)  # Raw audit survives.


def test_incremental_index_preserves_other_file_and_literal_grep(database):
    from test_index import fixture, embedding
    index = PostgresIndex(database)
    files, blocks, vectors = fixture(index)
    with index._connect() as conn:
        before = {row['ref']: row['id'] for row in conn.execute('SELECT ref,id FROM blocks')}
    chosen = files[0]['alias']
    delta_files = [file for file in files if file['alias'] == chosen]
    delta_blocks = [block for block in blocks if Ref.parse(block['ref']).alias == chosen]
    delta_vectors = {ref: value for ref, value in vectors.items() if Ref.parse(ref).alias == chosen}
    index.sync(delta_files, delta_blocks, delta_vectors, full=False)
    with index._connect() as conn:
        after = {row['ref']: row['id'] for row in conn.execute('SELECT ref,id FROM blocks')}
    assert after == before
    assert index.search([embedding([1, 0])], {'topics': ['memory']})[0]['ref'] == 'u1:2'
    assert index.grep('已测试', {'sources': ['agent1']})[0]['ref'] == 'u1:2'
    assert index.grep('%') == []
    assert {row['ref'] for row in index.dependent_blocks(['a1@2-3'])} == {'u1:2'}
    assert {row['ref'] for row in index.dependent_blocks(['a1@12-13'])} == {'s1:2', 'u1:3'}


def test_claude_turn_survives_page_boundary_and_bridge_restart(tmp_path):
    from urllib.parse import urlsplit
    from memu.memulife import MemULifeBridge
    source = {'adapter_id': 'ai.local-logs', 'source_profile': 'claude-code',
              'source_instance_id': 'selected', 'source_namespace': 'local'}
    def record(n, role, content):
        return {'object_id': str(n), 'raw': json.dumps({'sessionId': 'one', 'uuid': str(n),
                'type': role, 'message': {'content': content}}).encode()}
    pages = [
        {'records': [record(1, 'user', '修复并验证启动。'), record(2, 'assistant', [
            {'type': 'tool_use', 'id': 'call', 'name': 'Bash', 'input': {'command': 'verify'}}])],
         'cursor': {'page': 1}, 'has_more': True, 'warnings': []},
        {'records': [record(3, 'user', [{'type': 'tool_result', 'tool_use_id': 'call', 'content': '验证通过'}]),
                     record(4, 'assistant', '启动已修复，验证通过。')],
         'cursor': {'page': 2}, 'has_more': False, 'warnings': []},
    ]
    class Reader:
        def read(self, mode, cursor):
            return pages[cursor.get('page', 0) if cursor else 0]
    class Client:
        url = urlsplit('http://core.invalid')
        materials = []
        def write(self, identity, material):
            self.materials.append(material)
            return {'is_error': False, 'result': {'status': 'accepted', 'request_id': 'one', 'sources': ['c1']}}
    client = Client()
    args = (client, 'owner', source, {'scopes': ['selected']}, tmp_path / 'outbox.json')
    first = MemULifeBridge(*args).collect(Reader(), max_pages=1)
    assert first['status'] == 'more' and client.materials == []
    result = MemULifeBridge(*args).collect(Reader(), resume=True, max_pages=1)
    assert result['status'] == 'done' and result['write_results'][0]['status'] == 'accepted'
    assert len(client.materials) == 1
    text = client.materials[0]['text']
    assert '修复并验证启动' in text and '验证通过' in text
    assert '工具返回（不是用户发言）' in text
    assert list((tmp_path / 'outbox.materials').iterdir()) == []


from memu.refs import Ref
