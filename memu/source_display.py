"""Conversation presentation from saved Source lines; never reconstruct erased text."""
import re
from .file_browsing import file_descriptors


def source_display(repo, block, *, offset=0, limit=20000):
    if block['kind'] != 'source':
        return None
    row = repo.catalog.get(block['alias'])
    metadata = repo.full_metadata(row['metadata']) if hasattr(repo, 'full_metadata') else row['metadata']
    origin = metadata.get('memulife', {})
    files = file_descriptors(repo, row, block['ref'], metadata)
    original = next((item for item in files if item['file_id'] == 'original'), None)
    if not original and files and metadata.get('content', {}).get('audit_only'):
        original = files[0]
    if original:
        return {'title': original['name'], 'mode': 'attachment', 'messages': []}
    layout = (metadata.get('content', {}).get('message_layout') or origin.get('message_layout')
              or origin.get('snapshot_metadata', {}).get('message_layout') or [])
    text = block['body']
    lines = text.splitlines(keepends=True)
    first = block['start_line']
    positions = [0]
    for line in lines:
        positions.append(positions[-1] + len(line))
    title = next((line.lstrip('# ').strip() for line in text.splitlines()[:3] if line.startswith('# ')), '')
    messages = []
    for index, entry in enumerate(layout):
        start = entry.get('start_line')
        end = entry.get('end_line') or (layout[index + 1]['start_line'] - 1 if index + 1 < len(layout) else block['end_line'])
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        begin, finish = max(first, start) - first, min(block['end_line'], end) - first + 1
        if not 0 <= begin < finish <= len(lines):
            continue
        low, high = positions[begin], positions[finish]
        if high <= offset or low >= offset + limit:
            continue
        message = text[max(low, offset):min(high, offset + limit)]
        name, role = '', entry.get('role', 'unknown')
        # Strip only the renderer's envelope, within known message boundaries.
        if offset <= low:
            heading = re.search(r'^## (.+)\n', message, re.M)
            if heading:
                label = heading[1].split(' | ')[0]
                if role == 'speaker':
                    name = label.removeprefix('发言人：').split(' [微信 ID：')[0]
                    role = 'user' if entry.get('actor', {}).get('role') == 'self' else 'speaker'
                message = message[heading.end():]
                if origin.get('adapter_id') == 'browser.ai-history':
                    message = re.sub(r'^消息 ID：[^\n]*\n[^\n]*时间[^\n]*\n\n?', '', message)
        if message.strip() and entry.get('event_kind') == 'tool_result' and entry.get('call_id') and offset <= low:
            message = '调用 ID：' + str(entry['call_id']) + '\n' + message
        if message.strip():
            messages.append({'role': role, 'name': name, 'time': entry.get('occurred_at'),
                             'body': message.strip(), 'continued': offset > low})
    return {'title': title[:160] or '来源内容', 'mode': 'conversation' if layout else 'document', 'messages': messages}
