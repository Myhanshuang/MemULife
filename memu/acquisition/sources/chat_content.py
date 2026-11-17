"""Bounded conversation windows over selected adapter text, with lossless raw audit."""
import base64
from dataclasses import asdict, is_dataclass
from datetime import datetime
import hashlib
import json

ADAPTERS = {'qq.napcat', 'wechat.localdb'}
VERSION = 'chat-window-v1'
MAX_MESSAGES = 40
MAX_CHARS = 12000
WINDOW_SECONDS = 600


def _wire(row):
    row = asdict(row) if is_dataclass(row) else dict(row)
    if isinstance(row['raw'], bytes):
        row['raw'] = base64.b64encode(row['raw']).decode()
    row['attachments'] = [dict(item) for item in row.get('attachments', [])]
    for item in row['attachments']:
        if isinstance(item.get('data'), bytes):
            item['data'] = base64.b64encode(item['data']).decode()
    return row


def _unpack(row):
    row = _wire(row)
    try:
        value = json.loads(base64.b64decode(row['raw']))
    except (ValueError, UnicodeDecodeError):
        return [row]
    return value['records'] if isinstance(value, dict) and value.get('format') == VERSION else [row]


def group_records(records):
    groups, current, key, size = [], [], None, 0
    for row in (item for record in records for item in _unpack(record)):
        meta = row.get('metadata', {})
        conversation = meta.get('conversation_id')
        try:
            bucket = int(datetime.fromisoformat(row['occurred_at'].replace('Z', '+00:00')).timestamp()) // WINDOW_SECONDS
        except (KeyError, TypeError, ValueError):
            bucket = None
        current_key = (conversation, bucket)
        # Non-message records stay separately auditable and never mix conversations.
        valid = conversation and (row.get('text') or row.get('attachments')) and not row.get('deleted')
        length = len(row.get('text') or '')
        if current and (not valid or current_key != key or len(current) >= MAX_MESSAGES or size + length > MAX_CHARS):
            groups.append(current); current, size = [], 0
        if not valid:
            groups.append([row]); continue
        current.append(row); size += length; key = current_key
    if current:
        groups.append(current)
    result = []
    for group in groups:
        if (not group[0].get('metadata', {}).get('conversation_id') or group[0].get('deleted') or
                not any(row.get('text') or row.get('attachments') for row in group)):
            result.extend(group); continue
        group.sort(key=lambda r: (r.get('occurred_at') or '', r['object_id']))
        text, layout, attachments = [], [], []
        wechat = group[0]['metadata'].get('platform') == 'wechat'
        if wechat:
            first = group[0]['metadata']
            conversation_name = first.get('conversation_name') or first['conversation_id']
            text.extend(['# 微信会话：' + ' '.join(conversation_name.split()), ''])
        for row in group:
            if not row.get('text') and not row.get('attachments'):
                continue
            actor = row.get('metadata', {}).get('actor', {})
            name = actor.get('display_name') or actor.get('source_actor_id') or '身份未知'
            if wechat:
                name = '发言人：' + ' '.join(name.split())
                if actor.get('role') == 'self' and name != '发言人：我自己':
                    name += '（我自己）'
                if actor.get('source_actor_id'):
                    name += ' [微信 ID：' + ' '.join(actor['source_actor_id'].split()) + ']'
            start = len(text) + 1
            text.append(f"## {name} | {row.get('occurred_at') or '时间未知'}")
            text.extend((row.get('text') or '〔消息附件〕').splitlines()); text.append('')
            layout.append({'id': row['object_id'], 'role': 'speaker', 'actor': actor,
                           'occurred_at': row.get('occurred_at'), 'start_line': start, 'end_line': len(text)})
            for attachment in row.get('attachments', []):
                attachments.append({**attachment, 'attachment_id': row['object_id'] + '/' + attachment['attachment_id']})
        originals = [(row['object_id'], hashlib.sha256(base64.b64decode(row['raw'])).hexdigest()) for row in group]
        raw_hash = hashlib.sha256(json.dumps(originals, separators=(',', ':')).encode()).hexdigest()
        conversation = group[0]['metadata']['conversation_id']
        raw = json.dumps({'format': VERSION, 'records': group}, ensure_ascii=False, separators=(',', ':')).encode()
        result.append({'object_id': 'chat:' + conversation + ':' + group[0]['object_id'], 'raw': raw,
                       'text': '\n'.join(text), 'media_type': 'application/json', 'attachments': attachments,
                       'occurred_at': group[0].get('occurred_at'), 'revision': raw_hash,
                       'metadata': {'conversation_id': conversation, 'original_object_ids': [r[0] for r in originals],
                                    'original_raw_sha256': raw_hash, 'message_layout': layout,
                                    'projection_version': 'wechat-speakers-v2' if wechat else VERSION,
                                    'material_kind': 'chat_window'}})
    return result
