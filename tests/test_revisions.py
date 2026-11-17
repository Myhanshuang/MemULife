"""Focused revision invariants with synthetic records; never writes user memory."""
import json

import pytest

from memu.config import Config
from memu.index import InMemoryIndex, PostgresIndex
from memu.mcp import MCPApplication
from memu.operations import CoreApplication
from memu.postgres_repository import PostgresRepository
from memu.service import MemoryService
from memu.storage import Repository
from test_reliability import database  # noqa: F401
from test_service import SyntheticEmbedding


WHEN = {'precision': 'day', 'value': '2026-11-17'}


def fixture(repo, index=None):
    config = Config.model_validate({'access': {'whitelist': {'writer': ['read', 'write'], 'reader': ['read']}}})
    embedding = SyntheticEmbedding()
    service = MemoryService(repo, config, embedder=embedding, index=index or InMemoryIndex())
    source = repo.save_source('original evidence\nsecond evidence', 'writer', WHEN)
    refs = repo.append_entries('unit', [
        {'time': WHEN, 'body': body, 'facts': [source], 'topics': [], 'relations': []}
        for body in ('CV experiment\noriginal result', 'another experiment', 'aaa')], 'writer')
    return service, refs, source, embedding


def change(service, ref, old='CV experiment', new='CV experiment belongs to the vision project', **kwargs):
    current = service.repo.read(ref)
    if kwargs.get('entry_id'):
        current = next(e for e in current['entries'] if e['entry_id'] == kwargs['entry_id'])
    return {'ref': ref, 'expected_revision': current['edit_revision'], 'old_text': old, 'new_text': new, **kwargs}


def revise(service, changes, key='one'):
    return service.memory_write(from_='writer', mode='revise', reason='User linked the two experiments',
                                changes=changes, idempotency_key=key)


def test_line_positions_atomic_conflicts_history_and_incremental_vectors(tmp_path):
    service, refs, source, embedding = fixture(Repository(tmp_path))
    service.sync_index()
    embedding.calls.clear()
    path = service.repo.root / service.repo.read(refs[0])['path']
    original = path.read_bytes().splitlines(keepends=True)
    sibling = service.repo.read(refs[1])
    request = change(service, refs[0], new='A vision project attempt\nwith a new conclusion\nthird line',
                     add_relations=[{'rel': '相关', 'target': refs[1]}], add_topics=['vision'])
    result = revise(service, [request])
    assert result['status'] == 'done'
    edited = path.read_bytes().splitlines(keepends=True)
    assert len(edited) == len(original)
    assert edited[sibling['start_line'] - 1:] == original[sibling['start_line'] - 1:]
    assert service.repo.read(refs[1])['edit_revision'] == sibling['edit_revision']
    assert service.repo.read(source)['body'] == 'original evidence\nsecond evidence'
    assert revise(service, [request]) == result
    assert revise(service, [dict(request, new_text='different')])['code'] == 'idempotency_conflict'
    assert revise(service, [request], 'stale')['conflicts'][0]['code'] == 'revision_changed'
    assert service.repo.state['topic_registry']['definitions']['vision']['pinned_members'] == [refs[0]]
    before = path.read_bytes()
    ambiguous = change(service, refs[2], old='aa', new='valid?')
    failed = revise(service, [change(service, refs[1], old='another', new='revised'), ambiguous], 'atomic')
    assert failed['conflicts'][0]['code'] == 'old_text_ambiguous' and path.read_bytes() == before
    with pytest.raises(ValueError):
        revise(service, [change(service, refs[1], old='another', add_refs=['c99999:1-2'])], 'badref')
    assert path.read_bytes() == before
    history = service.memory_read(from_='writer', refs=[refs[0]], history=True)
    audit = json.loads(history['results'][0]['body'])
    assert audit['before']['body'] == 'CV experiment\noriginal result'
    assert audit['after']['body'].startswith('A vision project attempt')
    service.sync_index()
    assert [item['text'] for call in embedding.calls for item in call] == [service.repo.read(refs[0])['body']]
    assert service.memory_search(from_='writer', query='new conclusion')['results'][0]['ref'] == refs[0]
    assert not service.repo.state['unit_index_invalidated']


def test_runtime_protection_exact_roundtrip_and_deleted_evidence(tmp_path, monkeypatch):
    service, refs, _, _ = fixture(Repository(tmp_path))
    runtime = service.repo.write_view('runtime', 'vision', [{'body': 'Original overview', 'time': WHEN, 'facts': refs[:1]}])
    entry = service.repo.read(runtime)['entries'][0]
    request = change(service, runtime, old='Original overview', new='Curated | overview\r\nwith ; delimiters\u2028unchanged',
                     entry_id=entry['entry_id'], add_refs=[refs[1]])
    revise(service, [request])
    pinned = service.repo.read(runtime)['entries'][0]
    assert pinned['protected'] and pinned['body'] == request['new_text']
    service.repo.write_view('runtime', 'vision', [{'body': 'Automatic rewrite', 'time': WHEN, 'facts': refs[:2]}])
    assert service.repo.read(runtime)['entries'] == [pinned]
    denied = revise(service, [change(service, runtime, old='Curated', new='Changed', entry_id=entry['entry_id'])], 'deny')
    assert denied['conflicts'][0]['code'] == 'entry_protected'
    read = service.memory_read(from_='writer', refs=[runtime], entry_id=entry['entry_id'])['results'][0]
    assert read['entry_id'] == entry['entry_id'] and read['edit_revision'] == pinned['edit_revision']
    with service.repo.transaction():
        service._pin_topic('vision', [refs[2]])
        service._queue_view('runtime', 'vision', rebuild=True, immediate=True)
    job = next(dict(value) for value in service.repo.state['pending_views'].values() if value['ref'] == runtime)
    def rollup(task, material, scope, allowed, **kwargs):
        assert allowed == [refs[2]] and 'old_view' not in material
        return {'entries': [{'body': 'Automatic third event', 'time': WHEN, 'facts': [refs[2]]}]}
    monkeypatch.setattr(service, '_task', rollup)
    service._refresh_view(job, job['generation'], service.repo.state['deletion_epoch'])
    kept = next(e for e in service.repo.read(runtime)['entries'] if e['entry_id'] == entry['entry_id'])
    assert kept['body'] == pinned['body'] and kept['facts'] == pinned['facts']
    appended = revise(service, [change(service, runtime, old='', new='Another protected insight', add_refs=[refs[2]])], 'append')
    assert appended['changed'][0]['entry_id'] != entry['entry_id']
    service.repo.blank(refs[0])
    service.repo.write_view('runtime', 'vision', [], {'active': False})
    current = service.repo.read(runtime)
    assert current['active'] and current['invalid_refs'] == [refs[0]]
    preserved = next(e for e in current['entries'] if e['entry_id'] == entry['entry_id'])
    assert preserved['body'] == pinned['body'] and preserved['facts'] == pinned['facts']


def test_runtime_explicit_unlock_and_aba_version(tmp_path):
    service, refs, _, _ = fixture(Repository(tmp_path))
    runtime = service.repo.write_view('runtime', 'vision', [{'body': 'one', 'time': WHEN, 'facts': refs[:1]}])
    entry = service.repo.read(runtime)['entries'][0]
    revise(service, [change(service, runtime, old='one', new='two', entry_id=entry['entry_id'])])
    revise(service, [change(service, runtime, old='two', new='one', entry_id=entry['entry_id'], protect=False)], 'unlock')
    current = service.repo.read(runtime)['entries'][0]
    assert not current['protected'] and current['entry_id'] == entry['entry_id']
    assert current['edit_revision'] != entry['edit_revision']
    service.repo.write_view('runtime', 'vision', [{'body': 'automatic', 'time': WHEN, 'facts': refs[:1]}])
    assert service.repo.read(runtime)['entries'][0]['entry_id'] == entry['entry_id']


def test_reimport_keeps_curated_unit_and_evidence_in_place(tmp_path):
    service, refs, source, _ = fixture(Repository(tmp_path))
    repo = service.repo
    revise(service, [change(service, refs[0])])
    protected = repo.read(refs[0])
    sibling = repo.read(refs[2])
    source_bytes = (repo.root / repo.read(source)['path']).read_bytes()
    path = repo.root / protected['path']
    lines = path.read_bytes().splitlines(keepends=True)
    with repo.transaction():
        repo.state['writes']['projection'] = {'request_id': 'projection', 'source': 'writer',
            'fingerprint': 'fixture', 'state': 'done', 'source_refs': [source], 'metadata': {}, 'result': {'units': refs}}
        repo.save_state()
    retired = service.retire_projections(['projection'])
    assert retired['protected_units'] == 1 and retired['units'] == 2
    assert repo.read(refs[0])['body'] == protected['body'] and repo.read(refs[0])['protected']
    after = path.read_bytes().splitlines(keepends=True)
    assert len(after) == len(lines)
    assert after[:protected['end_line']] == lines[:protected['end_line']]
    assert not after[sibling['start_line'] - 1].strip()
    assert (repo.root / repo.read(source)['path']).read_bytes() == source_bytes


def test_mcp_contract_access_and_internal_metadata(tmp_path):
    service, refs, _, _ = fixture(Repository(tmp_path))
    app = CoreApplication(service)
    tools = app.tools('writer')['tools']
    schema = next(t['inputSchema'] for t in tools if t['name'] == 'memory_write')
    assert schema['properties']['mode']['enum'] == ['ingest', 'revise']
    args = {'from': 'reader', 'mode': 'revise', 'reason': 'test', 'idempotency_key': 'one',
            'changes': [change(service, refs[0])]}
    assert app.call('reader', 'memory_write', args)['result']['error']['code'] == 'access_denied'
    assert app.call('writer', 'memory_write', dict(args, **{'from': 'writer', 'text': 'mixed'}))['is_error']
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': 'memory_write', 'arguments': {'from': 'writer', 'metadata': {'projection': {}}}}}
    assert MCPApplication(app).handle(request, 'writer')['result']['structuredContent']['error']['code'] == 'reserved_metadata'
    assert MCPApplication(app, write_enabled=False).handle(request, 'writer')['result']['structuredContent']['error']['code'] == 'tool_disabled'


def test_revision_required_fields_match_schema_and_report_safe_diagnostics(monkeypatch):
    from jsonschema import Draft202012Validator
    logs = []
    monkeypatch.setattr('memu.operations.event', lambda name, **fields: logs.append((name, fields)))
    monkeypatch.setattr('memu.mcp.event', lambda name, **fields: logs.append((name, fields)))

    class Service:
        config = Config.model_validate({'access': {'whitelist': {'agent1': ['read', 'write']}}})
        calls = []

        def memory_write(self, **arguments):
            self.calls.append(arguments)
            return {'status': 'done'}

    service = Service()
    core = CoreApplication(service)
    schema = next(t['inputSchema'] for t in core.tools('agent1')['tools'] if t['name'] == 'memory_write')
    validator = Draft202012Validator(schema)
    payload = {'from': 'agent1', 'mode': 'revise', 'idempotency_key': 'test-only-key',
               'changes': [{'ref': 'u12:3', 'expected_revision': 'test-version',
                            'old_text': 'private-old-text', 'new_text': 'private-new-text'}]}
    assert not validator.is_valid(payload)
    response = MCPApplication(core).handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'memory_write', 'arguments': payload}}, 'agent1')['result']
    error = response['structuredContent']['error']
    assert response['isError'] and error['code'] == 'invalid_arguments'
    assert error['details'][0]['field'] == '$' and error['details'][0]['code'] == 'revision_required'
    assert error['message'].endswith('reason') and len(error['trace_id']) == 32
    assert service.calls == []
    assert [name for name, _ in logs] == ['operation_rejected', 'mcp_tool_rejected']
    assert all(row['request_id'] == error['trace_id'] for _, row in logs)
    rendered = json.dumps(logs)
    assert all(value not in rendered for value in ('private-old-text', 'private-new-text', 'test-only-key', 'u12:3'))
    for field in ('reason', 'changes', 'idempotency_key'):
        complete = dict(payload, reason='Authorized correction')
        complete.pop(field)
        assert not validator.is_valid(complete)
        assert field in core.call('agent1', 'memory_write', complete)['result']['error']['message']
    complete = dict(payload, reason='Authorized correction')
    validator.validate(complete)
    assert not core.call('agent1', 'memory_write', complete)['is_error']
    assert len(service.calls) == 1
    assert validator.is_valid({'from': 'agent1', 'text': 'Normal ingest remains supported'})


def test_revision_nested_argument_error_reports_the_field():
    class Service:
        def memory_write(self, **arguments):
            raise AssertionError('Invalid input must never reach the write implementation')
    result = CoreApplication(Service()).call('agent1', 'memory_write', {
        'from': 'agent1', 'mode': 'revise', 'reason': 'Authorized correction', 'idempotency_key': 'test',
        'changes': [{'ref': 'u12:3', 'old_text': 'old', 'new_text': 'new'}]})
    error = result['result']['error']
    assert error['details'][0]['field'] == 'changes.0.expected_revision'
    assert 'Field required' in error['message']


def test_postgres_revision_rollback_recovery_and_index(database, tmp_path, monkeypatch):
    repo = PostgresRepository(tmp_path, database)
    service, refs, _, embedding = fixture(repo, PostgresIndex(database))
    service.sync_index()
    embedding.calls.clear()
    old = repo.read(refs[0])
    request = change(service, refs[0])
    original = service._mark_views
    def fail(*args, **kwargs):
        raise RuntimeError('injected failure after staging the Unit')
    monkeypatch.setattr(service, '_mark_views', fail)
    with pytest.raises(RuntimeError, match='injected failure'):
        revise(service, [request])
    assert repo.read(refs[0]) == old and not dict(repo.state['edit_heads'])
    monkeypatch.setattr(service, '_mark_views', original)
    result = revise(service, [request])
    service.sync_index()
    assert len(embedding.calls) == 1 and not dict(repo.state['unit_index_invalidated'])
    day = repo.write_view('day', '2026-11-17', [{'body': 'automatic summary', 'time': WHEN, 'facts': refs[:1]}])
    entry = repo.read(day)['entries'][0]
    revised_day = revise(service, [change(service, day, old=entry['body'], new='curated summary',
                                         entry_id=entry['entry_id'])], 'day')
    assert revised_day['status'] == 'done'
    assert repo.read(day)['entries'][0]['protected']
    repo.close()
    reopened = PostgresRepository(tmp_path, database)
    restored = MemoryService(reopened, service.config)
    assert revise(restored, [request]) == result
    assert reopened.read(refs[0])['edit_revision'] == result['changed'][0]['edit_revision']
    assert reopened.read(refs[0])['protected']
    reopened.write_view('day', '2026-11-17', [])
    kept = reopened.read(day)['entries'][0]
    assert kept['protected'] and kept['body'] == 'curated summary' and kept['entry_id'] == entry['entry_id']
    reopened.close()
