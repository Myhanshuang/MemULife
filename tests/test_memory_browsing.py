"""Read-only management browser: provenance, dates, literal search and HTTP boundaries."""
import base64
from copy import deepcopy
import http.client
import json
import threading

import pytest

from memu.acquisition.http_api import AcquisitionHTTPServer
from memu.acquisition.service import AcquisitionService
from memu.core_api import CoreHTTPServer
from memu.core_client import RemoteCore
from memu.errors import AccessDenied
from test_service import service_fixture


@pytest.fixture
def memories(tmp_path):
    service, model, embedding, _ = service_fixture(tmp_path)
    repo = service.repo
    origin = {'adapter_id': 'ai.local-logs', 'source_profile': 'codex', 'source_instance_id': 'connection-one',
              'source_namespace': '我的 Codex', 'object_id': 'session-one',
              'transport_audit': {'format': 'adapter-original-v1', 'raw': base64.b64encode(
                  '{"role":"user","text":"原件 Alpha.*"}'.encode()).decode()}}
    time = {'precision': 'instant', 'value': '2026-11-16T18:00:00Z'}
    with repo.transaction():
        alias = repo.save_source('Alpha.* 原文\nSecond line', 'owner', time, {'memulife': origin})
        source = alias
        detail = repo.append_entries('detail', [{'body': 'digest', 'time': time, 'facts': [source]}], 'owner')[0]
        unit = repo.append_entries('unit', [{'body': 'Alpha.* 记忆', 'time': time, 'facts': [detail]}], 'owner')[0]
        other_alias = repo.save_source('AlphaZZ OTHER', 'owner', {'precision': 'unknown', 'value': None},
            {'memulife': {**origin, 'source_profile': 'claude-code', 'source_instance_id': 'connection-two'}})
        other = other_alias
        day = repo.write_view('day', '2026-11-17', [{'body': 'Alpha.* 日汇总', 'time': time, 'facts': [unit]}])
        month = repo.write_view('month', '2026-11', [{'body': 'Alpha.* 月汇总', 'time': time, 'facts': [unit]}])
        runtime = repo.write_view('runtime', 'memory', [{'body': 'Alpha.* Runtime', 'time': time, 'facts': [unit]}])
        repo.state['pending_views'].clear()
        repo.save_state()
    return service, {'source': source, 'unit': unit, 'other': other, 'day': day, 'month': month, 'runtime': runtime}, model, embedding


def test_browse_filters_provenance_and_does_not_invoke_models(memories):
    service, refs, model, embedding = memories
    before = deepcopy(model.calls), deepcopy(embedding.calls)
    result = service.memory_browse('owner', adapter='ai.local-logs', profile='codex', start='2026-11-17', end='2026-11-17')
    assert {item['ref'] for item in result['items']} == {refs['source'], refs['unit']}
    assert all(item['date_start'] == '2026-11-17' for item in result['items'])
    assert all(item['origins'][0]['source_instance_id'] == 'connection-one' for item in result['items'])
    assert not service.memory_browse('owner', start='2026-11-16', end='2026-11-16')['items']
    assert [item['ref'] for item in service.memory_browse('owner', source_id='connection-two')['items']] == [refs['other']]
    assert before == (model.calls, embedding.calls)


def test_literal_search_all_layers_summary_scope_and_case(memories):
    service, refs, *_ = memories
    result = service.memory_browse('owner', layers=['source', 'unit', 'summary', 'runtime'], query='Alpha.*')
    assert {item['ref'] for item in result['items']} == set(refs.values()) - {refs['other']}
    assert not service.memory_browse('owner', layers=['source', 'unit'], query='alpha.*')['items']
    assert not service.memory_browse('owner', layers=['source'], query='Alpha.+')['items']
    result = service.memory_browse('owner', layers=['summary'], start='2026-11-01', end='2026-11-02')
    assert [item['ref'] for item in result['items']] == [refs['month']]
    assert result['items'][0]['date_end'] == '2026-11-30'


def test_paging_is_complete_and_rejects_changed_filters(memories):
    service, *_ = memories
    all_items = service.memory_browse('owner', layers=['unit', 'source', 'summary', 'runtime'])['items']
    items, cursor = [], ''
    while True:
        result = service.memory_browse('owner', layers=['unit', 'source', 'summary', 'runtime'], limit=1, cursor=cursor)
        items += result['items']
        cursor = result['next_cursor']
        if not cursor:
            break
        with pytest.raises(ValueError):
            service.memory_browse('owner', query='different', cursor=cursor)
    assert [item['ref'] for item in items] == [item['ref'] for item in all_items]


def test_original_detail_pagination_and_deleted_source_protection(memories):
    service, refs, *_ = memories
    original = service.memory_browse_detail('owner', ref=refs['source'], original=True)
    assert original['original_available'] and '原件 Alpha.*' in original['body']
    assert 'raw_ref' not in original['body']
    alias = refs['source'].split(':')[0]
    service.repo.blank(alias + ':1-1')
    surviving = service.memory_browse_detail('owner', ref=alias + ':2-2', original=True)
    assert not surviving['original_available'] and surviving['body'] == 'Second line'
    assert '原件' not in json.dumps(service.memory_browse('owner'))
    with pytest.raises(ValueError):
        service.memory_browse_detail('owner', ref=refs['source'], original=True)
    with service.repo.transaction():
        long_alias = service.repo.save_source('中' * 21000, 'owner', {'precision': 'unknown', 'value': None})
    first = service.memory_browse_detail('owner', ref=long_alias)
    second = service.memory_browse_detail('owner', ref=long_alias, offset=first['next_offset'], revision=first['revision'])
    assert len(first['body']) == 20000 and len(second['body']) == 1000 and second['next_offset'] is None
    with pytest.raises(ValueError):
        service.memory_browse_detail('owner', ref=long_alias, offset=20000, revision='stale')


def test_suppressed_views_and_authorization(memories):
    service, refs, *_ = memories
    with service.repo.transaction():
        service.repo.state['pending_views']['blocked-day'] = {'ref': refs['day'], 'blocked': True, 'state': 'queued'}
        service.repo.save_state()
    assert not service.memory_browse('owner', layers=['day'])['items']
    with pytest.raises(ValueError):
        service.memory_browse_detail('owner', ref=refs['day'])
    for identity in ['reader', 'agent2', 'unknown']:
        with pytest.raises(AccessDenied):
            service.memory_browse(identity)
        with pytest.raises(AccessDenied):
            service.memory_browse_detail(identity, ref=refs['source'])


@pytest.mark.parametrize('kind', ['unit', 'day', 'month', 'runtime'])
def test_management_edit_protection_and_background_rebuild(memories, kind):
    service, refs, model, embedding = memories
    ref = refs[kind]
    entry = service.memory_browse_edit('owner', ref=ref)['entries'][0]
    before_calls = deepcopy(model.calls), deepcopy(embedding.calls)
    args = {'ref': ref, 'expected_revision': entry['edit_revision'], 'old_text': entry['body'],
            'new_text': '手动整理 | 原样\r\n保留分隔符\u2028和引用', 'protect': True}
    if entry.get('entry_id'):
        args['entry_id'] = entry['entry_id']
    def submit(key, value):
        return service.memory_write(from_='owner', mode='revise', reason='用户编辑',
                                    idempotency_key=key, changes=[value])
    result = submit('edit', args)
    assert result['status'] == 'done' and result['views'] == 'queued'
    assert submit('edit', args) == result
    assert before_calls == (model.calls, embedding.calls)
    current = service.memory_browse_edit('owner', ref=ref)['entries'][0]
    assert current['body'] == args['new_text'] and current['protected']
    again = dict(args, expected_revision=current['edit_revision'], old_text=current['body'], new_text='再次编辑')
    assert submit('denied', again)['conflicts'][0]['code'] == 'entry_protected'
    assert submit('again', dict(again, allow_protected=True))['status'] == 'done'
    assert submit('stale', dict(again, allow_protected=True))['conflicts'][0]['code'] == 'revision_changed'
    if kind != 'unit':
        record = service.repo.read(ref)
        service.repo.write_view(kind, record['scope'], [{'body': '自动覆盖', 'time': record['entries'][0]['time'],
                                                       'facts': [refs['unit']]}])
        with service.repo.transaction():
            service._queue_view(kind, record['scope'], rebuild=True, immediate=True, blocked=True)
    current = service.memory_browse_edit('owner', ref=ref)['entries'][0]
    assert current['body'] == '再次编辑' and current['protected']
    assert service.memory_browse_detail('owner', ref=ref)['body'] == '再次编辑'
    history = service.memory_read(from_='owner', refs=[ref], history=True)['results']
    assert len(history) == 2
    with pytest.raises(AccessDenied):
        service.memory_browse_edit('reader', ref=ref)
    with pytest.raises(ValueError):
        service.memory_browse_edit('owner', ref=refs['source'])


def test_media_browse_resolves_evidence_and_exact_revision_attachments(memories):
    service, refs, *_ = memories
    repo = service.repo
    time = {'precision': 'instant', 'value': '2026-11-17T00:00:00Z'}
    origin = {'adapter_id': 'browser.ai-history', 'source_profile': 'chatgpt',
              'source_instance_id': 'media-connection', 'object_id': 'conversation', 'raw_sha256': 'revision-one'}
    with repo.transaction():
        source = repo.save_source('带图片的聊天', 'owner', time, {'memulife': origin})
        image = repo.save_media(b'fixture-image', 'owner', time, 'image/png', filename='photo.png', metadata={'memulife': origin})
        repo.save_media(b'other-revision', 'owner', time, 'image/png', metadata={'memulife': {**origin, 'raw_sha256': 'revision-two'}})
        unit = repo.append_entries('unit', [{'body': '图片记忆', 'time': time, 'facts': [source]}], 'owner')[0]
    assert [m['ref'] for m in service.memory_browse_detail('owner', ref=unit)['media']] == [image]
    assert [m['ref'] for m in service.memory_browse_detail('owner', ref=source)['media']] == [image]
    result = service.memory_browse('owner', layers=['source'], query='photo.png')
    assert [i['ref'] for i in result['items']] == [image]
    repo.blank(image)
    assert service.memory_browse_detail('owner', ref=unit)['media'] == []


@pytest.mark.parametrize('target', ['unit', 'source'])
def test_management_deletion_keeps_line_numbers_and_rebuilds_dependent_views(memories, target, monkeypatch):
    from memu.errors import RevisionConflict
    from memu.refs import Ref
    service, refs, *_ = memories
    repo = service.repo
    time = repo.read(refs['unit'])['time']
    with repo.transaction():
        shared = repo.append_entries('unit', [{'body': '共享证据', 'time': time, 'facts': [refs['source']]}], 'owner')[0]
        retained = repo.append_entries('unit', [{'body': '独立记忆', 'time': time, 'facts': [refs['other']]}], 'owner')[0]
    preserved = repo.read(retained)
    unit_file = repo.root / preserved['path']
    source_file = repo.root / repo.read(refs['source'])['path']
    unit_lines, source_lines = unit_file.read_text().splitlines(), source_file.read_text().splitlines()
    plan = service.memory_browse_delete_preview('owner', ref=refs[target])
    assert plan['counts']['unit'] == 2 and plan['counts']['source'] == 1
    assert service.memory_browse_detail('owner', ref=refs['unit'])['body']
    with pytest.raises(AccessDenied):
        service.memory_browse_delete('reader', ref=refs[target], revision=plan['revision'])
    with pytest.raises(RevisionConflict):
        service.memory_browse_delete('owner', ref=refs[target], revision='0' * 64)
    calls = []
    def background_only(*args, **kwargs):
        calls.append(True)
        raise AssertionError('Deletion must return before derivative rebuild work')
    before = deepcopy(memories[2].calls), deepcopy(memories[3].calls)
    with monkeypatch.context() as patch:
        patch.setattr(service, 'sync_index', background_only)
        patch.setattr(service, '_reconcile_topics', background_only)
        patch.setattr(service.index, 'vectors', background_only)
        result = service.memory_browse_delete('owner', ref=refs[target], revision=plan['revision'])
    assert not calls and before == (memories[2].calls, memories[3].calls)
    assert not result['index_ready'] and repo.state['index_dirty'] and service.changed.is_set()
    assert {refs['unit'], refs['source'], shared} <= set(result['deleted'])
    assert repo.read(retained)['start_line'] == preserved['start_line']
    assert repo.read(retained)['body'] == preserved['body']
    assert len(unit_file.read_text().splitlines()) == len(unit_lines)
    assert len(source_file.read_text().splitlines()) == len(source_lines)
    assert all(not line.strip() for line in source_file.read_text().splitlines())
    for name in ('day', 'month', 'runtime'):
        pending = service._pending(refs[name])
        assert pending['blocked'] and pending['mode'] == 'rebuild'
        assert refs[name] not in {r['ref'] for r in service.memory_browse('owner', layers=[name])['items']}
    assert Ref.parse(retained).alias == Ref.parse(refs['unit']).alias
    service.sync_index()
    service.refresh_due(force=True)
    for name in ('day', 'month', 'runtime'):
        assert not service._pending(refs[name])
        rebuilt = repo.read(refs[name])['body']
        assert 'Alpha.*' not in rebuilt and '共享证据' not in rebuilt
        assert '独立记忆' in rebuilt


def test_deferred_media_redaction_rebuilds_vectors_after_restart(memories, monkeypatch):
    from memu.service import MemoryService
    from memu.storage import Repository
    service, _, model, embedding = memories
    repo = service.repo
    with repo.transaction():
        media = repo.save_media(b'original-media', 'owner', {'precision': 'unknown', 'value': None},
                               'video/mp4', duration=10)
    def media_items(ref):
        body = (repo.root / repo.catalog[ref]['path']).read_bytes().decode()
        return [(ref, {'kind': 'text', 'text': body})]
    monkeypatch.setattr(service, '_media_items', media_items)
    service.sync_index()
    old_vector = deepcopy(service.index.vectors()[media])
    monkeypatch.setattr('memu.media.redact_media', lambda *_: (b'redacted-media', 'video/mp4'))
    ref = media + '@1-2'
    plan = service.memory_browse_delete_preview('owner', ref=ref)
    service.memory_browse_delete('owner', ref=ref, revision=plan['revision'])
    assert repo.state['media_index_invalidated'][media] == repo.revision(media)
    assert service.index.vectors()[media] == old_vector
    resumed = MemoryService(Repository(repo.root), service.config, model, embedding, service.index)
    monkeypatch.setattr(resumed, '_media_items', media_items)
    resumed.sync_index()
    assert resumed.index.vectors()[media] != old_vector
    assert resumed.repo.state['media_index_invalidated'] == {}


def test_source_conversation_uses_live_lines_and_pages_without_repeating_headers(memories):
    service, *_ = memories
    from memu.acquisition.sources.browser_content import material_content
    import json
    text = '用户内容 <img onerror=bad()>'
    raw = json.dumps({'format': 'memulife.browser-messages.v1', 'provider': 'gemini', 'native_conversation_id': 'conversation',
                     'messages': [{'id': 'one', 'role': 'user', 'text': text, 'occurred_at': None},
                                  {'id': 'two', 'role': 'assistant', 'text': '回复' * 11000, 'occurred_at': None}]}).encode()
    content = material_content(raw, 'gemini', 'conversation')
    with service.repo.transaction():
        ref = service.repo.save_source(content['text'], 'owner', {'precision': 'unknown', 'value': None},
            {'memulife': {'adapter_id': 'browser.ai-history', 'message_layout': content['message_layout']}})
    first = service.memory_browse_detail('owner', ref=ref)
    assert first['display']['mode'] == 'conversation'
    assert first['display']['messages'][0]['body'] == text
    assert first['display']['messages'][0]['role'] == 'user'
    second = service.memory_browse_detail('owner', ref=ref, offset=first['next_offset'], revision=first['revision'])
    assert second['display']['messages'][0]['continued']
    assert first['display']['messages'][1]['body'] + second['display']['messages'][0]['body'] == '回复' * 11000


@pytest.mark.parametrize('name,mime,external', [('工作簿.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', False),
                                             ('文档.pdf', 'application/pdf', True)])
def test_original_file_attachment_preserves_bytes_and_respects_erased_lines(memories, name, mime, external):
    from hashlib import sha256
    service, *_ = memories
    repo = service.repo
    raw = b'\x00original-file-fixture\xff\n'
    storage = {'raw': base64.b64encode(raw).decode()}
    if external:
        path = '.meta/source-originals/' + sha256(raw).hexdigest() + '.bin'
        (repo.root / path).parent.mkdir(parents=True, exist_ok=True)
        (repo.root / path).write_bytes(raw)
        storage = {'raw_ref': path}
    with repo.transaction():
        ref = repo.save_source('拆开的内容\n保留的内容', 'owner', {'precision': 'unknown', 'value': None}, {
            'content': {'material_kind': 'local_file'}, 'memulife': {'adapter_id': 'filesystem.typora',
            'raw_media_type': mime, 'raw_size_bytes': len(raw), 'snapshot_metadata': {'path': 'folder/' + name},
            'transport_audit': {'format': 'file-original-v1', **storage}}})
    detail = service.memory_browse_detail('owner', ref=ref)
    assert detail['display']['mode'] == 'attachment'
    assert detail['media'][0]['name'] == name and detail['media'][0]['kind'] == 'file'
    result = service.memory_browse_file('owner', ref=ref)
    assert result['name'] == name and result['mime_type'] == mime
    assert base64.b64decode(result['data_base64']) == raw
    with pytest.raises(AccessDenied):
        service.memory_browse_file('reader', ref=ref)
    with pytest.raises(ValueError):
        service.memory_browse_file('owner', ref=ref, file_id='../../.env')
    alias = ref.split(':')[0]
    repo.blank(alias + ':1')
    remaining = alias + ':2-2'
    assert service.memory_browse_detail('owner', ref=remaining)['media'] == []
    with pytest.raises(ValueError):
        service.memory_browse_file('owner', ref=remaining)


def test_non_media_chat_attachment_uses_saved_original(memories):
    service, *_ = memories
    raw = b'PDF-attachment-fixture'
    with service.repo.transaction():
        ref = service.repo.save_source('聊天内容', 'owner', {'precision': 'unknown', 'value': None}, {'memulife': {
            'adapter_id': 'browser.ai-history', 'attachments': [{'attachment_id': 'doc-one', 'metadata': {'name': '附件.pdf'}}],
            'attachment_originals': [{'attachment_id': 'doc-one', 'media_type': 'application/pdf', 'data_base64': base64.b64encode(raw).decode()}]}})
    detail = service.memory_browse_detail('owner', ref=ref)
    assert detail['display']['mode'] != 'attachment'
    attachment = detail['media'][0]
    assert attachment['name'] == '附件.pdf' and attachment['size_bytes'] == len(raw)
    assert base64.b64decode(service.memory_browse_file('owner', ref=ref, file_id=attachment['file_id'])['data_base64']) == raw


def test_media_browse_immich_legacy_copy_uses_remote_reference_and_honors_redactions(memories):
    service, *_ = memories
    repo = service.repo
    time = {'precision': 'unknown', 'value': None}
    origin = {'adapter_id': 'immich.assets', 'source_instance_id': 'immich-one', 'object_id': 'asset:asset-one',
              'snapshot_metadata': {'asset_id': 'asset-one', 'owner_id': 'owner-one', 'deployment_id': 'server-one'}}
    with repo.transaction():
        image = repo.save_media(b'legacy-copy', 'owner', time, 'image/png', metadata={'memulife': origin})
        video = repo.save_media(b'video-fixture', 'owner', time, 'video/mp4', duration=10)
    media = service.memory_browse_detail('owner', ref=image)['media'][0]
    assert media['kind'] == 'immich' and media['asset_id'] == 'asset-one'
    # Legacy repositories may contain partial media tombstones.
    with repo.transaction():
        repo.state.setdefault('media_blanked_ranges', []).append({'alias': video, 'start': 1, 'end': 3})
        repo.save_state()
    assert service.memory_browse_detail('owner', ref=video)['media'] == []
    assert service.memory_browse_detail('owner', ref=video + '@3-5')['media'][0]['ref'] == video + '@3-5'
    with pytest.raises(ValueError, match='Redacted media'):
        service.media_resource('owner', 'memu://media/' + video)


@pytest.mark.parametrize('filters', [dict(layers=['detail']), dict(layers=[]), dict(query=['bad']),
    dict(start='2026-02-30'), dict(start='2026-11-18', end='2026-11-17'), dict(limit=1000), dict(cursor='%%%')])
def test_invalid_filters(memories, filters):
    with pytest.raises(ValueError):
        memories[0].memory_browse('owner', **filters)


def test_audit_only_worklogs_are_hidden_without_filtering_real_quotes_or_file_attachments(memories):
    service, *_ = memories
    repo = service.repo
    placeholder = '# codex 原始审计（没有可提取的新事件）'
    time = {'precision': 'unknown', 'value': None}
    with repo.transaction():
        audit = repo.save_source(placeholder, 'owner', time,
            {'content': {'material_kind': 'agent_worklog', 'audit_only': True}})
        quote = repo.save_source(placeholder, 'owner', time,
            {'content': {'material_kind': 'agent_worklog', 'audit_only': False}})
        attachment = repo.save_source('# 文件原件', 'owner', time,
            {'content': {'material_kind': 'local_file', 'audit_only': True}})
    refs, cursor = [], ''
    while True:
        page = service.memory_browse('owner', layers=['source'], limit=1, cursor=cursor)
        refs.extend(item['ref'] for item in page['items'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert audit not in refs and quote in refs and attachment in refs
    assert [item['ref'] for item in service.memory_browse('owner', layers=['source'], query='原始审计')['items']] == [quote]
    for original in (False, True):
        with pytest.raises(ValueError, match='Memory unavailable'):
            service.memory_browse_detail('owner', ref=audit, original=original)
    assert service.memory_browse_detail('owner', ref=quote)['body'] == placeholder
    assert repo.read(audit)['body'] == placeholder  # No rewriting or renumbering historical evidence.


def test_management_http_through_real_core(memories, tmp_path):
    service, refs, *_ = memories
    with service.repo.transaction():
        media = service.repo.save_media(b'fixture-image', 'owner', {'precision': 'unknown', 'value': None}, 'image/png')
    tokens = {'owner': 'o' * 40, 'reader': 'r' * 40}
    core = CoreHTTPServer(service, service.config.core.model_copy(update={'port': 0}), tokens)
    core_thread = threading.Thread(target=core.run); core_thread.start()
    remote = RemoteCore(f'http://localhost:{core.server_port}', tokens)
    acquisition = AcquisitionService(tmp_path / 'acquisition', remote, service.config)
    server = AcquisitionHTTPServer(acquisition, service.config.core.model_copy(update={'port': 0}), tokens)
    thread = threading.Thread(target=server.run); thread.start()
    def request(path, body=None, identity='owner'):
        conn = http.client.HTTPConnection('localhost', server.server_port, timeout=5)
        conn.request('POST' if body is not None else 'GET', path, json.dumps(body) if body is not None else None,
                     {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tokens.get(identity, '')})
        reply = conn.getresponse(); value = reply.read(); conn.close()
        return reply.status, value
    try:
        for path in ['/manage/memory', '/manage/memory/summaries', '/manage/memory/topics', '/manage/memory/ai/chatgpt']:
            assert request(path)[0] == 200
        status, body = request('/v1/manage/memory', {'layers': ['unit'], 'source_id': 'connection-one'})
        assert status == 200 and json.loads(body)['items'][0]['ref'] == refs['unit']
        assert request('/v1/manage/memory/detail', {'ref': refs['source'], 'original': True})[0] == 200
        assert request('/v1/manage/memory', {}, 'reader')[0] == 403
        assert request('/v1/manage/memory', {}, 'anonymous')[0] == 401
        assert request('/v1/manage/memory', {'layers': ['bad']})[0] == 422
        assert request('/v1/manage/memory/detail', {'ref': 'c999999:1'})[0] == 422
        status, body = request('/v1/manage/memory/media', {'ref': media})
        assert status == 200 and base64.b64decode(json.loads(body)['data_base64']) == b'fixture-image'
        assert request('/v1/manage/memory/media', {'ref': media}, 'reader')[0] == 403
        assert request('/v1/manage/memory/edit', {'ref': refs['source']})[0] == 422
        assert request('/v1/manage/memory/edit', {'ref': refs['unit']}, 'reader')[0] == 403
        status, body = request('/v1/manage/memory/edit', {'ref': refs['unit']})
        assert status == 200
        entry = json.loads(body)['entries'][0]
        edit = {'reason': '页面手动修改', 'idempotency_key': 'http-edit', 'changes': [{
            'ref': refs['unit'], 'expected_revision': entry['edit_revision'],
            'old_text': entry['body'], 'new_text': '手动维护的记忆', 'protect': True}]}
        assert request('/v1/manage/memory/revise', edit, 'reader')[0] == 403
        status, body = request('/v1/manage/memory/revise', edit)
        assert status == 200 and json.loads(body)['status'] == 'done'
        assert service.repo.read(refs['unit'])['protected']
        assert request('/v1/manage/memory/revise', dict(edit, idempotency_key='stale'))[0] == 409
        assert request('/v1/manage/memory/delete-preview', {'ref': refs['unit']}, 'reader')[0] == 403
        status, body = request('/v1/manage/memory/delete-preview', {'ref': refs['unit']})
        assert status == 200
        plan = json.loads(body)
        assert request('/v1/manage/memory/delete', {'ref': refs['unit'], 'revision': '0' * 64})[0] == 409
        assert request('/v1/manage/memory/delete', {'ref': refs['unit'], 'revision': plan['revision']})[0] == 200
        assert request('/v1/manage/memory/detail', {'ref': refs['unit']})[0] == 422
    finally:
        server.request_stop(); thread.join(5); server.close(); acquisition.close()
        core.request_stop(); core_thread.join(5); core.close()
