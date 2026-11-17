"""Focused regressions for the real bulk-import blockers; no inference or database."""
import json

import zstandard

from memu.acquisition.adapters.local.logs import CodexAdapter
from memu.acquisition.sources import local_ai_content as content
from memu.memulife import page_materials


SOURCE = {'adapter_id': 'ai.local-logs', 'source_profile': 'codex',
          'source_instance_id': 'selected-source', 'source_namespace': 'local'}


def record(kind, payload, index, source='cli'):
    return {'object_id': str(index), 'metadata': {'file': 'session.jsonl', 'session_id': 'session',
            'session_source': source}, 'raw': json.dumps({'type': kind,
            'timestamp': '2026-11-15T01:00:00Z', 'payload': payload}).encode()}


def message(role, text, index, source='cli'):
    return record('response_item', {'type': 'message', 'role': role, 'id': str(index),
                                  'content': text}, index, source)


def test_guardian_is_excluded_before_reading_the_body(tmp_path):
    directory = tmp_path / 'sessions'; directory.mkdir()
    header = json.dumps({'type': 'session_meta', 'payload': {'id': 'internal',
                        'source': {'subagent': {'other': 'guardian'}}}}).encode() + b'\n'
    path = directory / 'review.jsonl.zst'
    path.write_bytes(zstandard.ZstdCompressor().compress(header + b'not even a valid record\n'))
    reader = CodexAdapter({'root': str(tmp_path)})
    page = reader.read('backfill')
    assert not page.records and not page.has_more
    assert page.warnings == ['internal_session_excluded']
    assert reader.read('backfill', page.cursor).records == []


def test_subagent_native_origin_survives_reader_pages_and_cannot_be_user_speech(tmp_path):
    directory = tmp_path / 'sessions'; directory.mkdir()
    source = {'subagent': {'thread_spawn': {'parent_thread_id': 'parent'}}}
    rows = [{'type': 'session_meta', 'payload': {'id': 'child', 'source': source}},
            json.loads(message('user', '检查并报告结果', 1, source)['raw']),
            json.loads(message('assistant', '助手报告：检查发现保存失败。', 2, source)['raw'])]
    (directory / 'child.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    reader = CodexAdapter({'root': str(tmp_path)})
    first = reader.read('backfill', limit=1)
    second = reader.read('backfill', first.cursor, limit=100)
    snapshots = content.group_records(second.records, 'codex')
    material = content.material_content(snapshots[0]['raw'], 'codex', snapshots[0]['metadata'])
    assert material['message_layout'][0]['event_kind'] == 'delegation'
    assert all(e['role'] == 'assistant' for e in material['message_layout'])
    assert '父代理委派' in material['text'] and '用户原话' not in material['text']


def test_raw_audit_spills_preserve_one_turn_across_pages(monkeypatch):
    monkeypatch.setattr(content, 'MAX_AUDIT_CHARS', 1600)
    monkeypatch.setattr(content, 'MAX_AUDIT_RECORDS', 5)
    rows = [message('user', '修复保存，并保留已有数据。', 0)]
    rows += [record('response_item', {'type': 'function_call', 'name': 'Write', 'call_id': str(i),
             'arguments': json.dumps({'file_path': '/tmp/example', 'content': 'x' * 1900})}, i)
             for i in range(1, 15)]
    rows.append(message('assistant', '助手报告：保存已修复，已有数据保留。', 15))

    def collect(page_size):
        carry, requests = None, []
        for start in range(0, len(rows), page_size):
            result = page_materials(SOURCE, {'records': rows[start:start+page_size],
                'cursor': {'offset': start+page_size}, 'warnings': [], 'has_more': start+page_size < len(rows)}, carry)
            carry = result.get('carry')
            requests.extend(result['requests'])
        assert carry is None
        semantic = [m for m in requests if not m['metadata']['content']['audit_only']]
        assert all(not m['text'] for m in requests if m['metadata']['content']['audit_only'])
        assert len(semantic) == 1
        assert semantic[0]['metadata']['content']['unit_limit'] == 1
        assert '修复保存' in semantic[0]['text'] and '已有数据保留' in semantic[0]['text']
        assert 'x' * 100 not in semantic[0]['text']
        originals = [r for m in requests for r in m['metadata']['memulife']['transport_audit']['records']
                     if not r.get('semantic_carry')]
        assert [(r['object_id'], r['raw_utf8']) for r in originals] == [(r['object_id'], r['raw'].decode()) for r in rows]
        return semantic[0]

    full, paged = collect(100), collect(3)
    assert full['idempotency_key'] == paged['idempotency_key']
    assert full['text'] == paged['text']


def test_legacy_review_snapshot_quarantines_the_verdict_too():
    rows = [message('user', 'The following is the Codex agent history whose request action you are assessing. evidence', 0),
            message('assistant', '{"decision":"allow"}', 1)]
    for row in rows:
        row['metadata'].pop('session_source')
    for snapshot in content.group_records(rows, 'codex'):
        material = content.material_content(snapshot['raw'], 'codex', snapshot['metadata'])
        assert material['audit_only'] and not material['message_layout']


def test_claude_sidechain_keeps_identity_across_audit_spills(monkeypatch):
    monkeypatch.setattr(content, 'MAX_AUDIT_CHARS', 600)
    values = [{'type': 'user', 'message': {'content': '父代理要求检查保存结果。'}}]
    values += [{'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'id': str(i), 'name': 'Write', 'input': {'content': 'x' * 1200}}]}}
        for i in range(4)]
    values += [{'type': 'assistant', 'message': {'content': '已发现保存失败。'}}]
    rows = [{'object_id': str(i), 'metadata': {'session_id': 'parent', 'sidechain': True},
             'raw': json.dumps({**v, 'sessionId': 'parent', 'agentId': 'child', 'isSidechain': True}).encode()}
            for i,v in enumerate(values)]
    results = content.group_records(rows, 'claude-code')
    materials = [content.material_content(r['raw'], 'claude-code', r['metadata']) for r in results]
    semantic = [m for m in materials if not m['audit_only']]
    assert len(semantic) == 1 and '已发现保存失败' in semantic[0]['text']
    assert all(e['role'] != 'user' and e['branch'] == 'child' for e in semantic[0]['message_layout'])
    assert all(r['metadata']['audit_only_part'] for r in results[:-1])


def test_codex_tool_image_payload_is_audit_only_while_result_text_survives():
    rows = [message('user', '查看返回结果', 0), record('response_item', {
        'type': 'custom_tool_call_output', 'call_id': 'images', 'output': [
            {'type': 'input_text', 'text': '渲染完成，共 10 张图片。'},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,private-image-bytes'}]}, 1)]
    result = content.group_records(rows, 'codex')[0]
    material = content.material_content(result['raw'], 'codex', result['metadata'])
    assert '渲染完成' in material['text']
    assert 'private-image-bytes' not in material['text']
    assert any('private-image-bytes' in row['raw_utf8'] for row in material['transport_audit']['records'])


def test_context_only_page_advances_without_creating_a_source():
    rows = [record('session_meta', {'id': 'session', 'source': 'cli'}, 0),
            message('developer', 'configuration only', 1),
            message('user', '# AGENTS.md instructions\nconfiguration only', 2)]
    result = page_materials(SOURCE, {'records': rows, 'cursor': {'offset': 3},
                                    'warnings': [], 'has_more': False})
    assert result['requests'] == [] and result['cursor'] == {'offset': 3}
    assert 'carry' not in result
