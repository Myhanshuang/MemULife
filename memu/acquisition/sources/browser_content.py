"""Deterministic readable message material; retain transport bytes as source audit metadata."""
import json
from datetime import datetime, timezone, timedelta
from .messages import clean_message
from ..adapters.base import AdapterError

FORMAT = 'memulife.browser-conversation.v2'


def legacy_gemini(value):
    """Decode reviewed hNvQHb fields, without scanning arbitrary nested strings/times."""
    def at(row, *indices):
        for index in indices:
            if not isinstance(row, list) or len(row) <= index: return None
            row = row[index]
        return row

    def stamp(pair):
        if not isinstance(pair, list) or len(pair) != 2 or any(type(v) is not int for v in pair): return None
        seconds, nanos = pair
        if not 0 <= seconds <= 253402300799 or not 0 <= nanos < 10**9: return None
        try:
            return (datetime.fromtimestamp(seconds, timezone.utc) + timedelta(milliseconds=nanos // 1000000)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        except (ValueError, OverflowError): return None

    turns = []
    responses = value.get('responses', [])
    if not isinstance(responses, list) or not responses: raise AdapterError('source_record_invalid')
    for response in responses:
        found = False
        if not isinstance(response, str): raise AdapterError('source_record_invalid')
        for line in response.splitlines():
            if not line.lstrip().startswith('['): continue
            for frame in json.loads(line):
                if at(frame, 0) == 'wrb.fr' and at(frame, 1) == 'hNvQHb':
                    payload = json.loads(at(frame, 2))
                    if not isinstance(at(payload, 0), list): raise AdapterError('source_record_invalid')
                    turns.extend(payload[0]); found = True
        if not found: raise AdapterError('source_record_invalid')
    messages, seen = [], {}
    def add(identity, role, text, date):
        if not isinstance(text, str) or not text: return
        message = {'id': identity, 'role': role, 'text': text, 'occurred_at': date}
        clean_message(message)
        if identity in seen:
            if seen[identity] != message: raise AdapterError('message_identity_conflict')
            return
        seen[identity] = message; messages.append(message)
    for turn in reversed(turns):
        rid = at(turn, 0, 1)
        if not isinstance(rid, str) or not rid: raise AdapterError('source_record_invalid')
        add('turn:' + rid + ':user', 'user', at(turn, 2, 0, 0), stamp(at(turn, 4)))
        candidates, timing = at(turn, 3, 0) or [], at(turn, 3, 2) or []
        for candidate in candidates:
            cid = at(candidate, 0)
            if not isinstance(cid, str) or not cid: raise AdapterError('source_record_invalid')
            matches = [row for row in timing if at(row, 3) == cid]
            add('candidate:' + cid, 'assistant', at(candidate, 1, 0), stamp(at(matches[0], 4)) if len(matches) == 1 else None)
    return {**value, 'format': FORMAT, 'messages': messages,
            'time_basis': {'user': 'native_turn_time', 'assistant': 'native_candidate_time'}}


def material_content(raw, provider, object_id):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if isinstance(value, dict) and value.get('format') == 'memulife.browser-response-set.v1' and value.get('provider') == 'gemini':
        value = legacy_gemini(value)
    if not isinstance(value, dict) or value.get('format') not in {FORMAT, 'memulife.browser-messages.v1'}:
        return None
    if value.get('provider') != provider or value.get('native_conversation_id') != object_id and not object_id.startswith('selected-messages:'):
        raise AdapterError('source_binding_mismatch')
    messages = value.get('messages')
    if not isinstance(messages, list) or not messages or len(messages) > 20000:
        raise AdapterError('source_record_invalid')
    clean = [clean_message(message) for message in messages]
    if len({message['id'] for message in clean}) != len(clean):
        raise AdapterError('message_identity_conflict')
    roles = {'user': '用户', 'assistant': 'AI 助手', 'system': '系统', 'tool': '工具', 'unknown': '未知角色'}
    bases = {'native_turn_time': '平台轮次时间', 'native_candidate_time': '平台候选回复记录时间', 'native_message_time': '平台消息时间'}
    timing = value.get('time_basis', {})
    if not isinstance(timing, dict) or any(v not in bases for v in timing.values()):
        raise AdapterError('source_record_invalid')
    blocks, layout, line = [], [], 3
    stamps = []
    for number, message in enumerate(clean, 1):
        stamp = message['occurred_at']
        if stamp: stamps.append(stamp)
        basis = bases.get(timing.get(message['role'], timing.get('default')), '平台消息时间')
        block = '\n'.join([f'## {number}. {roles[message["role"]]}', f'消息 ID：{message["id"]}',
                           f'{basis}：{stamp}' if stamp else '消息时间：未知', '', message['text']])
        blocks.append(block)
        layout.append({'start_line': line, 'id': message['id'], 'role': message['role'],
                       'occurred_at': stamp, 'time_basis': basis})
        line += len((block + '\n\n').splitlines())
    span = ({'precision': 'instant', 'value': min(stamps)} if len(set(stamps)) == 1 else
            {'precision': 'range', 'value': {'from': min(stamps), 'to': max(stamps)}}) if stamps else {'precision': 'unknown', 'value': None}
    # A partly undated conversation cannot be assigned wholly to its known dates.
    if len(stamps) != len(clean): span = {'precision': 'unknown', 'value': None}
    audit = None
    if value['format'] == FORMAT:
        responses = value.get('responses')
        if not isinstance(responses, list) or not responses or any(not isinstance(row, str) for row in responses):
            raise AdapterError('source_record_invalid')
        audit = {'format': 'memulife.browser-response-set.v1', 'provider': provider,
                 'native_conversation_id': value['native_conversation_id'], 'responses': responses}
    return {'text': f'# {provider} 原始会话\n\n' + '\n\n'.join(blocks) + '\n',
            'time': span, 'audit': audit, 'message_count': len(clean), 'message_layout': layout}
