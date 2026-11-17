"""Typed observations from selected local AI records; no generic JSON-as-prose fallback."""
from datetime import datetime, timezone
import hashlib
import json
import re
from html import unescape

PROFILES = {'claude-code', 'codex', 'gemini-cli', 'opencode', 'cursor'}
CONTEXT_PREFIXES = ('This session is being continued from a previous conversation',
                    '# AGENTS.md instructions', '<environment_context>', '<permissions instructions>',
                    '<system-reminder>', '<developer_instructions>')
REFERENCE_TOOLS = {'read', 'read_file', 'glob', 'grep', 'ls', 'list_dir', 'list_files',
                   'todowrite', 'taskupdate', 'taskcreate', 'tasklist', 'taskget'}


def stamp(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value / 1000 if value > 100_000_000_000 else value,
                                          timezone.utc).isoformat()
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.isoformat() if parsed.tzinfo else None
    except (ValueError, OverflowError, OSError):
        pass
    return None


def text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(str(part.get('text', '')) for part in value if isinstance(part, dict)
                         and part.get('type') in {'text', 'input_text', 'output_text'})
    return ''


def parsed(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def encoded(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def observation(row, value, role, kind, body, *, timestamp=None, native_id=None, **fields):
    meta = row.get('metadata', {})
    return {'role': role, 'kind': kind, 'body': body,
            'id': str(native_id or value.get('uuid') or value.get('id') or row.get('object_id', '')),
            'occurred_at': stamp(timestamp if timestamp is not None else value.get('timestamp')),
            'session_id': value.get('sessionId') or meta.get('session_id'),
            'branch': value.get('agentId') or meta.get('forked_from_id'),
            'cwd': value.get('cwd') or meta.get('cwd'), **fields}


def message_events(row, value, role, body, *, timestamp=None, native_id=None, mirror=False):
    if role not in {'user', 'assistant'} or not body.strip():
        return []
    if body.lstrip().startswith(CONTEXT_PREFIXES):
        return []
    if role == 'user' and body.lstrip().startswith('<task-notification>'):
        # Only embedded result bytes are evidence; referenced output files are never followed.
        parts = [unescape(found.group(1)) for key in ('summary', 'event', 'result', 'output')
                 if (found := re.search(rf'<{key}>(.*?)</{key}>', body, re.S))]
        if not parts:
            return []
        return [observation(row, value, 'tool', 'notification', '\n'.join(parts),
                            timestamp=timestamp, native_id=native_id, outcome='reported')]
    if role == 'assistant' and re.match(
            r"^(?:You've hit your limit|You have hit your limit|API Error:|Rate limit reached)", body):
        return []
    progress = role == 'assistant' and bool(re.match(
        r"(?is)^(?:Let me|I'll|I will|我先|接下来).{0,60}(?:read|inspect|look at|读取|查看|浏览)", body)) and len(body) < 400
    return [observation(row, value, role, 'progress' if progress else 'message', body, timestamp=timestamp,
                        native_id=native_id, mirror=mirror)]


def claude(row, value):
    if value.get('isMeta') or value.get('isCompactSummary') or value.get('isApiErrorMessage'):
        return []
    role = value.get('type')
    if role not in {'user', 'assistant'}:
        return []
    message = value.get('message', {})
    content = message.get('content', []) if isinstance(message, dict) else []
    if isinstance(content, str):
        content = [{'type': 'text', 'text': content}]
    events = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        if kind == 'text':
            events += message_events(row, value, role, block.get('text', ''))
        elif kind == 'tool_use':
            events.append(observation(row, value, 'assistant', 'tool_call', encoded(block.get('input', {})),
                                      call_id=block.get('id'), tool=block.get('name', ''), outcome='attempt'))
        elif kind == 'tool_result':
            events.append(observation(row, value, 'tool', 'tool_result', text(block.get('content', '')),
                                      call_id=block.get('tool_use_id'),
                                      outcome='error' if block.get('is_error') else 'returned'))
    return events


def codex(row, value):
    if 'item' in value:
        item = value['item']
        if not isinstance(item, dict):
            return []
        value = {**item, 'timestamp': item.get('timestamp', value.get('timestamp'))}
    kind, payload = value.get('type'), value.get('payload', {})
    if not isinstance(payload, dict):
        return []
    if kind == 'event_msg' and payload.get('type') in {'user_message', 'agent_message'}:
        role = 'user' if payload['type'] == 'user_message' else 'assistant'
        return message_events(row, value, role, text(payload.get('message', '')), mirror=True,
                              native_id=payload.get('id'))
    if kind != 'response_item':
        return []
    subtype = payload.get('type')
    if subtype == 'message' or (subtype is None and payload.get('role')):
        if payload.get('channel') == 'analysis':
            return []
        result = message_events(row, value, payload.get('role'), text(payload.get('content', '')),
                                native_id=payload.get('id'))
        if payload.get('role') == 'assistant' and payload.get('phase', payload.get('channel')) == 'commentary':
            for event in result:
                event['kind'] = 'progress'
        return result
    if subtype in {'function_call', 'custom_tool_call', 'local_shell_call'}:
        args = payload.get('arguments', payload.get('input', payload.get('action', {})))
        return [observation(row, value, 'assistant', 'tool_call', encoded(parsed(args)),
                            call_id=payload.get('call_id', payload.get('id')),
                            tool=payload.get('name', 'shell' if subtype == 'local_shell_call' else ''),
                            outcome='attempt')]
    if subtype in {'function_call_output', 'custom_tool_call_output', 'local_shell_call_output'}:
        output = parsed(payload.get('output', ''))
        # Typed tool output may include large image data URLs. Only explicit
        # text is an observation here; binary media stays in the original audit.
        body = text(output) if isinstance(output, list) else encoded(output)
        return [observation(row, value, 'tool', 'tool_result', body,
                            call_id=payload.get('call_id', payload.get('id')),
                            outcome='error' if isinstance(output, dict) and output.get('exit_code') not in {None, 0}
                            else 'returned')]
    return []


def gemini_message(row, value):
    if value.get('isSummary') or value.get('type') in {'summary', 'info', 'error', 'system'}:
        return []
    role = {'user': 'user', 'gemini': 'assistant', 'assistant': 'assistant'}.get(value.get('type'))
    events = message_events(row, value, role, text(value.get('content', '')))
    for tool in value.get('toolCalls', []):
        if not isinstance(tool, dict):
            continue
        events.append(observation(row, value, 'assistant', 'tool_call', encoded(tool.get('args', {})),
                                  call_id=tool.get('id'), tool=tool.get('name', ''), outcome='attempt'))
        if tool.get('result') is not None:
            events.append(observation(row, value, 'tool', 'tool_result', encoded(tool['result']),
                                      call_id=tool.get('id'), tool=tool.get('name', ''),
                                      timestamp=tool.get('timestamp', value.get('timestamp')),
                                      outcome='error' if tool.get('status') == 'error' else 'returned'))
    return events


def gemini(row, value):
    if any(key in value for key in ('$set', '$rewindTo')):
        return []  # Context rewrites are not new events.
    if '$patch' in value:
        patch = value['$patch']; events = []
        changed = ([patch] if 'id' in patch else []) + patch.get('updates', [])
        existing = {m.get('id'): m for m in row.get('metadata', {}).get('semantic_messages', [])}
        for update in changed:
            base = existing.get(update.get('id'))
            if not base:
                continue
            known_tools = {tool.get('id'): tool for tool in base.get('toolCalls', [])}
            tools = [{**known_tools.get(tool.get('id'), {}), **tool,
                      'timestamp': tool.get('timestamp', value.get('timestamp'))}
                     for tool in update.get('toolCalls', []) if isinstance(tool, dict)]
            message = {**base, 'timestamp': value.get('timestamp'),
                       'content': update.get('content', ''), 'toolCalls': tools}
            events.extend(gemini_message(row, message))
        return events
    if isinstance(value.get('messages'), list):
        return [e for m in value['messages'] if isinstance(m, dict) for e in gemini_message(row, m)]
    return gemini_message(row, value)


def opencode(row, value):
    table = row.get('metadata', {}).get('table', '')
    if table.startswith('session') and table != 'session_message':
        return []
    data = parsed(value.get('data', value))
    if not isinstance(data, dict) or data.get('synthetic') or data.get('type') in {'compaction', 'reasoning', 'snapshot', 'patch'}:
        return []
    occurred = stamp(data.get('time', {}).get('created')) if isinstance(data.get('time'), dict) else None
    occurred = occurred or stamp(value.get('time_created'))
    role = data.get('role') or row.get('metadata', {}).get('parent_role') or value.get('type')
    if data.get('type') == 'tool':
        state = data.get('state', {})
        if state.get('status') not in {'completed', 'error'}:
            return []
        return [observation(row, value, 'tool', 'tool_result', encoded(state.get('output', state.get('error', ''))),
                            timestamp=(state.get('time') or {}).get('end', occurred),
                            call_id=data.get('callID'), tool=data.get('tool', ''),
                            outcome='error' if state['status'] == 'error' else 'returned')]
    return message_events(row, value, role, text(data.get('text', data.get('content', ''))), timestamp=occurred)


def cursor(row, value):
    # Internal formats evolve. Explicit bubbles are accepted; opaque composer state is audit only.
    bubbles = value.get('conversation', value.get('bubbles')) if isinstance(value, dict) else None
    if isinstance(bubbles, list):
        return [e for bubble in bubbles if isinstance(bubble, dict) for e in cursor(row, bubble)]
    if not isinstance(value, dict) or value.get('isSummary') or value.get('isThought'):
        return []
    role = value.get('role') or {1: 'user', 2: 'assistant'}.get(value.get('type'))
    return message_events(row, value, role, text(value.get('text', value.get('content', ''))),
                          timestamp=value.get('createdAt', value.get('timestamp')), native_id=value.get('bubbleId'))


def events(profile, row, value):
    return {'claude-code': claude, 'codex': codex, 'gemini-cli': gemini,
            'opencode': opencode, 'cursor': cursor}[profile](row, value)


def reference_call(item):
    from .local_ai_tools import classify
    return classify(item) in {'read', 'poll'}


def event_identity(event):
    return hashlib.sha256(json.dumps({k: event.get(k) for k in ('id', 'kind', 'role', 'body', 'call_id',
                                                              'occurred_at', 'session_id', 'branch')},
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()
