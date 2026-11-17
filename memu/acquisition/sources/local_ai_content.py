"""Bounded work episodes with raw audit and separately attributed evidence."""
import base64
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime
import hashlib
import json
import re

from .local_ai_tools import ANSWER_PREFIXES, classify, describe, result_excerpt

from .local_ai_events import events, event_identity, parsed
from .local_ai_origin import reviewer_prompt, session_kind

VERSION = 'local-ai-v4'
GENERATION = 4
MAX_TOOL_EVENTS = 8
MAX_ASSISTANT_EVENTS = 3
MAX_AUDIT_CHARS = 512 * 1024
MAX_AUDIT_RECORDS = 200
CARRY_FORMAT = 'memu-local-ai-carry-v1'


def unpack(row):
    row = asdict(row) if is_dataclass(row) else dict(row)
    raw = row['raw']
    if isinstance(raw, str):
        raw = base64.b64decode(raw, validate=True)
    value = json.loads(raw)
    if isinstance(value, dict) and value.get('format') == 'local-ai-events-v1':
        return value['records']
    if isinstance(value, dict) and value.get('format') == 'claude-code-events-v1':
        ids = row.get('metadata', {}).get('original_object_ids', [])
        return [{'object_id': ids[n] if n < len(ids) else hashlib.sha256(original.encode()).hexdigest(),
                 'metadata': row.get('metadata', {}), 'raw_utf8': original}
                for n, original in enumerate(value['records'])]
    meta = row.get('metadata', {})
    metadata = {k: meta[k] for k in ('file', 'database', 'table', 'session_id', 'forked_from_id',
                'turn_id', 'native_id', 'parent_role', 'semantic_messages', 'cwd', 'ordinal', 'tool_names',
                'session_source', 'originator', 'sidechain') if k in meta}
    return [{'object_id': row['object_id'], 'revision': row.get('revision'),
             'occurred_at': row.get('occurred_at'), 'metadata': metadata, 'raw_utf8': raw.decode('utf-8')}]


def _events(profile, row):
    value = json.loads(row['raw_utf8'])
    if not isinstance(value, dict):
        return []
    if row.get('semantic_carry') and value.get('format') == CARRY_FORMAT:
        return value['events']
    kind = session_kind(row.get('metadata', {}).get('session_source'))
    if profile == 'claude-code' and value.get('isSidechain', row.get('metadata', {}).get('sidechain')):
        kind = 'delegated'
    if kind == 'internal_review':
        return []
    if profile == 'cursor' and 'key' in value:
        value = parsed(value.get('value'))
        if not isinstance(value, dict):
            return []
    result = events(profile, row, value)
    for event in result:
        if event['role'] == 'user':
            if profile == 'codex' and reviewer_prompt(event['body']):
                return []
            if kind == 'delegated':
                event['role'], event['kind'] = 'assistant', 'delegation'
        if event['kind'] == 'tool_result':
            event['tool'] = event.get('tool') or row.get('metadata', {}).get('tool_names', {}).get(event.get('call_id'), '')
            answer = parsed(event['body'])
            if ((event.get('tool') == 'AskUserQuestion' and event['body'].startswith(ANSWER_PREFIXES)) or
                    (event.get('tool', '').split('.')[-1] == 'request_user_input' and
                     isinstance(answer, dict) and answer.get('answers'))):
                event['role'], event['kind'] = 'user', 'user_answer'
    return result


def _chain(group):
    chain, count = '', 0
    for row in group:
        if row.get('semantic_carry'):
            value = json.loads(row['raw_utf8'])
            chain, count = value['raw_chain'], value['raw_records']
        else:
            chain = hashlib.sha256(json.dumps([chain, row['object_id'],
                hashlib.sha256(row['raw_utf8'].encode()).hexdigest()]).encode()).hexdigest()
            count += 1
    return chain, count


def _snapshot(group, profile, *, audit=False):
    values = [e for row in group for e in _events(profile, row)]
    primary = [e for e in values if e['role'] == 'user' or e['kind'] == 'delegation']
    first = primary[0] if primary else next(iter(values), {})
    meta = group[0].get('metadata', {})
    session = [profile, first.get('session_id', meta.get('session_id')),
               first.get('branch', meta.get('forked_from_id')), meta.get('file', meta.get('database'))]
    conversation = hashlib.sha256(json.dumps(session).encode()).hexdigest()
    turn = 'turn:' + hashlib.sha256(json.dumps([conversation, first.get('id', group[0]['object_id'])]).encode()).hexdigest()
    checksum, count = _chain(group)
    parts = [part for row in group if row.get('semantic_carry')
             for part in json.loads(row['raw_utf8']).get('audit_parts', [])]
    records = [r for r in group if not r.get('semantic_carry')] if audit else group
    raw = json.dumps({'format': 'local-ai-events-v1', 'profile': profile, 'records': records},
                     ensure_ascii=False, separators=(',', ':')).encode()
    return {'object_id': turn + (':audit:' + checksum if audit else ''), 'revision': checksum, 'raw': raw,
            'occurred_at': first.get('occurred_at'), 'media_type': 'application/json', 'attachments': [],
            'metadata': {**meta, 'conversation_id': conversation, 'turn_id': turn,
                         'original_raw_sha256': checksum, 'raw_record_count': count,
                         'audit_only_part': audit, 'audit_parts': parts,
                         'original_object_ids': [r['object_id'] for r in records]}}


def _carry(group, profile, audit):
    items = [e for row in group for e in _events(profile, row)]
    candidates, _ = _candidates(items)
    # Preserve tool pairing/classification across raw audit chunks without code or diffs.
    calls = {}
    for e in items:
        if e['kind'] == 'tool_call':
            calls[e.get('call_id')] = {**e, 'body': '', 'call_class': e.get('call_class') or classify(e),
                                      'call_description': e.get('call_description') or describe(e)}
    chain, count = _chain(group)
    value = {'format': CARRY_FORMAT, 'events': list(calls.values())[-128:] + _bounded(candidates),
             'raw_chain': chain, 'raw_records': count,
             'audit_parts': audit['metadata']['audit_parts'] + [audit['object_id']]}
    first_value = json.loads(group[0]['raw_utf8'])
    value.update({k: first_value[k] for k in ('sessionId', 'agentId', 'isSidechain') if k in first_value})
    return {'object_id': group[0]['object_id'], 'metadata': group[0].get('metadata', {}),
            'semantic_carry': True, 'raw_utf8': json.dumps(value, ensure_ascii=False, separators=(',', ':'))}


def group_records(records, profile='claude-code'):
    """Keep a real user turn across pages. Context replay never starts a new turn."""
    expanded = [item for record in records for item in unpack(record)]
    # Resolve tools before splitting so an answer can start its own decision turn.
    call_tools = {}
    for row in expanded:
        values = _events(profile, row)
        for event in values:
            call_key = (event.get('session_id'), event.get('branch'), event.get('call_id'))
            if event['kind'] == 'tool_call' and event.get('call_id'):
                call_tools[call_key] = event['tool']
            elif event['kind'] == 'tool_result' and call_key in call_tools:
                row.setdefault('metadata', {}).setdefault('tool_names', {})[event['call_id']] = call_tools[call_key]
    result, current, signature, size, last_user = [], [], None, 0, None
    for row in expanded:
        value = json.loads(row['raw_utf8']); meta = row.get('metadata', {})
        if not isinstance(value, dict):
            value = {}
        key = (value.get('sessionId') or meta.get('session_id'), value.get('agentId') or meta.get('forked_from_id'),
               bool(value.get('isSidechain')), meta.get('file', meta.get('database')))
        primary = [e for e in _events(profile, row) if e['role'] == 'user' or e['kind'] == 'delegation']
        user = primary[0] if primary else None
        mirrored_pair = False
        if (user and last_user and
                (user.get('body_sha256') or hashlib.sha256(user['body'].encode()).hexdigest()) ==
                (last_user.get('body_sha256') or hashlib.sha256(last_user['body'].encode()).hexdigest()) and
                user.get('mirror') != last_user.get('mirror')):
            first, second = user.get('occurred_at'), last_user.get('occurred_at')
            mirrored_pair = bool(first and second and abs((datetime.fromisoformat(first) -
                                                          datetime.fromisoformat(second)).total_seconds()) < 10)
        if current and (key != signature or (user and not mirrored_pair)):
            result.append(_snapshot(current, profile)); current, size, last_user = [], 0, None
        if current and any(not r.get('semantic_carry') for r in current) and (
                size + len(row['raw_utf8']) > MAX_AUDIT_CHARS or len(current) >= MAX_AUDIT_RECORDS):
            audit = _snapshot(current, profile, audit=True)
            result.append(audit)
            current = [_carry(current, profile, audit)]
            size = len(current[0]['raw_utf8'])
        current.append(row); size += len(row['raw_utf8']); signature = key
        if user:
            last_user = user
    if current:
        result.append(_snapshot(current, profile))
    return result


def _candidates(items):
    def body_key(e):
        return e.get('body_sha256') or hashlib.sha256(e['body'].encode()).hexdigest()
    canonical = {(e['role'], body_key(e)) for e in items if e['kind'] in {'message', 'progress'} and not e.get('mirror')}
    calls, seen, excluded, candidates = {}, set(), Counter(), []
    for event in items:
        e = dict(event); identity = e.get('event_id') or event_identity(e)
        if identity in seen or (e.get('mirror') and (e['role'], body_key(e)) in canonical):
            excluded['duplicate'] += 1; continue
        seen.add(identity)
        call_key = (e.get('session_id'), e.get('branch'), e.get('call_id'))
        if e['kind'] == 'tool_call':
            calls[call_key] = e
            excluded['tool_call_context'] += 1
            continue
        if e['kind'] == 'progress':
            excluded['progress'] += 1; continue
        call = calls.get(call_key)
        if e['kind'] == 'tool_result' and not e.get('prepared'):
            e['tool'] = e.get('tool') or (call or {}).get('tool', '')
            if e.get('tool') == 'AskUserQuestion' and e['body'].startswith(ANSWER_PREFIXES):
                e['role'], e['kind'] = 'user', 'user_answer'
            elif e.get('outcome') != 'error' and (call and (call.get('call_class') or classify(call)) == 'poll'):
                excluded['poll'] += 1; continue
            elif not call and re.match(r'^Task #[0-9]+ (?:created|updated|completed) successfully', e['body']):
                excluded['task_status'] += 1; continue
            if e['kind'] == 'tool_result':
                e['tool_purpose'] = (call.get('call_class') or classify(call)) if call else 'unknown'
                if not call and not e.get('tool'):
                    e['outcome'] = 'unpaired'
                e['body'] = ((call or {}).get('call_description', describe(call)) + '\n' + result_excerpt(e['body'])).strip()
                e['prepared'] = True
        if not e['body'].strip():
            excluded['empty'] += 1; continue
        e['event_id'] = identity
        e.setdefault('body_sha256', body_key(e))
        limit = 24000 if e['role'] == 'user' else 4000 if e['kind'] == 'delegation' else 1600
        if e['role'] != 'tool' and len(e['body']) > limit and not e.get('excerpted'):
            e['body'] = e['body'][:limit//2] + '\n〔节选；完整发言留在审计〕\n' + e['body'][-limit//2:]
            e['excerpted'] = True
        candidates.append(e)
    return candidates, excluded


def _bounded(candidates):
    tools = [i for i,e in enumerate(candidates) if e['role'] == 'tool']
    assistants = [i for i,e in enumerate(candidates) if e['role'] == 'assistant']
    # Users and explicit choices have their own budget; tool evidence is bounded.
    ranked_tools = sorted(tools, key=lambda i: (candidates[i].get('outcome') == 'error',
        bool(re.search(r'(?i)error|failed|conflict|accuracy|passed', candidates[i]['body'])), i), reverse=True)
    keep_tools = set(ranked_tools[:MAX_TOOL_EVENTS])
    keep_assistants = set(([assistants[0]] + assistants[-2:]) if len(assistants) > MAX_ASSISTANT_EVENTS else assistants)
    return [e for i,e in enumerate(candidates) if e['role'] == 'user' or
            (e['role'] == 'tool' and i in keep_tools) or (e['role'] == 'assistant' and i in keep_assistants)]


def material_content(raw, profile='claude-code', metadata=None):
    metadata = metadata or {}
    records = unpack({'object_id': 'selected', 'raw': raw, 'metadata': metadata})
    internal = session_kind(metadata.get('session_source')) == 'internal_review'
    items = [e for row in records for e in _events(profile, row)]
    # Old snapshots may predate native provenance. Quarantine the whole review,
    # including its assistant verdict, when its exact reviewer prompt is present.
    if profile == 'codex' and not metadata.get('session_source'):
        for row in records:
            if row.get('semantic_carry'):
                continue
            value = json.loads(row['raw_utf8'])
            internal |= any(reviewer_prompt(e['body']) for e in events(profile, row, value) if e['role'] == 'user')
    candidates, excluded = _candidates([] if internal or metadata.get('audit_only_part') else items)
    chosen = _bounded(candidates)
    excluded['bounded_events'] += len(candidates) - len(chosen)
    lines, layout = [f'# {profile} 用户轮次', ''], []
    cwd = next((e['cwd'] for e in candidates if e.get('cwd')), None)
    if cwd:
        lines.extend(['工作目录：' + cwd, ''])
    for e in chosen:
        start = 1 if not layout else len(lines) + 1
        label = {'user': '用户原话', 'assistant': '助手报告（未独立验证）', 'tool': '工具返回（不是用户发言）'}[e['role']]
        if e['kind'] == 'delegation':
            label = '父代理委派的任务（不是用户发言）'
        lines.append(f"## {label} | {e.get('occurred_at') or '时间未知'} | {e['kind']} {e.get('tool', '')}")
        if e['role'] == 'tool':
            lines.append('证据状态：' + e.get('outcome', 'returned'))
        body = e['body']
        lines.extend(body.splitlines()); lines.append('')
        layout.append({k: e.get(k) for k in ('id', 'role', 'occurred_at', 'outcome', 'tool', 'tool_purpose', 'session_id', 'branch', 'event_id')} |
                      {'event_kind': e['kind'], 'start_line': start, 'end_line': len(lines),
                       'time_basis': 'event_timestamp' if e.get('occurred_at') else 'unknown'})
    conversation = metadata.get('conversation_id') or hashlib.sha256(json.dumps(
        [profile, [(r.get('metadata', {}).get('session_id'), r.get('metadata', {}).get('file')) for r in records[:1]]]).encode()).hexdigest()
    return {'text': '\n'.join(lines) if layout else '',
            'message_layout': layout, 'message_count': len(layout), 'material_kind': 'agent_worklog',
            'audit_only': not any(e['event_kind'] != 'delegation' for e in layout),
            'projection_version': VERSION, 'evidence_contract': 'local-ai-v1',
            'unit_limit': 1, 'sequential_relations': True, 'conversation_id': conversation,
            'turn_id': metadata.get('turn_id'),
            'projection_counts': {'raw_records': metadata.get('raw_record_count', len(records)), 'eligible_events': len(layout),
                                  'user_decisions': sum(e['event_kind'] == 'user_answer' for e in layout),
                                  'context_or_unknown_records': sum(not _events(profile, row) for row in records), **excluded},
            'transport_audit': {'format': 'local-ai-originals-v2', 'records': records}}
