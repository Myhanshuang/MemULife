"""Model-facing views of opaque payloads; stored Source and chunk plans stay intact."""
from copy import deepcopy
import hashlib
import re


_HEX = re.compile(r'(?<![0-9A-Za-z])(?:0[xX])?[0-9A-Fa-f]{1024,}(?![0-9A-Za-z])')
_ANSWERS = re.compile(r'"(.*?)"="(.*?)"(?=,\s*".*?"=|\. You can now continue|$)', re.S)


def model_material(task, material):
    result = deepcopy(material)
    if task not in {'digest', 'unit'} or not isinstance(result, dict):
        return result
    for item in result.get('items', []):
        if not isinstance(item, dict) or not isinstance(item.get('body'), str):
            continue
        if item.get('provenance', {}).get('event_kind') == 'user_answer':
            # The question was written by the assistant. Only its answer is a
            # user statement; retain the same evidence locator and original bytes.
            pairs = _ANSWERS.findall(item['body'])
            if pairs:
                item['question_context'] = [question for question, _ in pairs]
                item['user_answers'] = [answer for _, answer in pairs]
                item['body'] = '用户回答（逐字原话）：\n' + '\n'.join(item['user_answers'])
                item['attribution_rule'] = 'question_context 是助手问题背景，不能归属为用户陈述或确认。'
                continue
        span = item.get('source_slice')
        if not isinstance(span, dict) or not str(item.get('ref', '')).startswith('c'):
            continue
        opaque = []

        def describe(match):
            raw = match.group()
            opaque.append({'encoding': 'hex', 'characters': len(raw),
                           'sha256_utf8': hashlib.sha256(raw.encode()).hexdigest(),
                           'source_ref': item['ref'], 'offset_origin': span['ref'],
                           'start': span['start'] + match.start(), 'end': span['start'] + match.end()})
            return f'〔原文中的十六进制数据片段 {len(opaque)}〕'

        prepared = _HEX.sub(describe, item['body'])
        if opaque:
            item['body'] = prepared
            item['opaque_data'] = opaque
    return result
