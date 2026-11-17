"""Lossless task slices over immutable Source text, preferring message boundaries."""
from bisect import bisect_right
import json
import re

from .refs import Ref


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')))


def text_portions(record, limit, messages=None):
    body, ref = record['body'], Ref.parse(record['ref'])
    if not body:
        return []
    starts = [0] + [match.end() for match in re.finditer('\n', body)]

    def item(start, end, context):
        # Source refs retain their original physical lines, including when several
        # slices share one very long line. Offsets distinguish those exact slices.
        first = ref.start + bisect_right(starts, start) - 1
        last = ref.start + bisect_right(starts, end - 1) - 1
        address = f'{ref.alias}:{first}-{last}' if record['kind'] == 'source' else record['ref']
        value = {'ref': address, 'body': body[start:end],
                 'source_slice': {'ref': record['ref'], 'start': start, 'end': end}}
        if context:
            value['messages'] = context
        return value

    groups = []
    if messages:
        previous = 0
        for index, message in enumerate(messages):
            line = message['start_line'] - ref.start
            next_line = messages[index + 1]['start_line'] - ref.start if index + 1 < len(messages) else len(starts)
            if next_line <= 0 or line >= len(starts):
                continue
            start = starts[max(0, line)]
            # Keep the conversation heading with its first message.
            if not groups:
                start = 0
            end = starts[next_line] if next_line < len(starts) else len(body)
            if start != previous or end <= start:
                raise ValueError('Conversation message boundaries do not cover the source')
            context = {key: message[key] for key in ('id', 'role', 'occurred_at', 'time_basis', 'event_kind') if key in message}
            groups.append((start, end, [context]))
            previous = end
        if not groups or previous != len(body):
            raise ValueError('Conversation message boundaries do not cover the source')
    else:
        # Without structured messages, prefer complete paragraphs, then lines.
        boundaries = [match.end() for match in re.finditer(r'\n[ \t]*\n', body)] + [len(body)]
        start = 0
        for end in boundaries:
            if end > start:
                groups.append((start, end, []))
            start = end

    result, pending = [], None
    def fits(start, end, context):
        return encoded_size([item(start, end, context)]) <= limit

    def flush():
        nonlocal pending
        if pending is not None:
            result.append(item(*pending))
            pending = None

    for start, end, context in groups:
        if pending is not None and fits(pending[0], end, pending[2] + context):
            pending = (pending[0], end, pending[2] + context)
            continue
        flush()
        if fits(start, end, context):
            pending = (start, end, context)
            continue
        # An oversized message is split without truncation or inserted newlines.
        # Account for escaping, locators and message context as well as its text.
        while start < end:
            low, high = start, min(end, start + limit)
            while low < high:
                middle = (low + high + 1) // 2
                if fits(start, middle, context):
                    low = middle
                else:
                    high = middle - 1
            if low == start:
                raise ValueError('Task budget cannot fit one character and its source context')
            stop = low
            if stop < end:
                floor = start + max(1, (stop - start) // 2)
                for pattern in (r'\n[ \t]*\n', r'\n', r'[。！？.!?](?:[ \t]+|(?=[^\x00-\x7f]))', r'\s+'):
                    matches = list(re.finditer(pattern, body[floor:stop]))
                    if matches:
                        stop = floor + matches[-1].end()
                        break
            result.append(item(start, stop, context))
            start = stop
    flush()
    return result


def split_batch(batch):
    """Deterministic binary split, preserving exact source offsets and locators."""
    from copy import deepcopy
    if len(batch) > 1:
        middle = len(batch) // 2
        return [batch[:middle], batch[middle:]]
    item = batch[0]
    body = item.get('body', '')
    if not body or len(body) < 256 or 'source_slice' not in item:
        raise ValueError('Output remains too large at the minimum evidence boundary')
    middle = len(body) // 2
    nearby = body.rfind('\n', len(body) // 3, 2 * len(body) // 3)
    if nearby >= 0:
        middle = nearby + 1
    ref = Ref.parse(item['ref'])
    span = item['source_slice']
    result = []
    for start, end in ((0, middle), (middle, len(body))):
        part = deepcopy(item)
        part['body'] = body[start:end]
        part['source_slice'] = {**span, 'start': span['start'] + start, 'end': span['start'] + end}
        if ref.kind == 'c':
            first = ref.start + body[:start].count('\n')
            last = ref.start + body[:end-1].count('\n')
            part['ref'] = f'{ref.alias}:{first}-{last}'
        result.append([part])
    return result
