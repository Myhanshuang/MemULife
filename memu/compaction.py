"""Bounded rolling context over a durable, per-import record ledger.

Only the compact memory and a few relevant ledger excerpts enter a prompt.
Completed outputs and coverage stay on disk; neither old raw chunks nor the
entire output history are concatenated into subsequent requests.
"""

from copy import deepcopy
import hashlib
import json
import re
import unicodedata

from .refs import Ref, merge_refs
from .task_material import model_material


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def size(value):
    return len(encoded(value))


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def settings(config):
    # Honor the configured input window independently of the output allowance.
    # Unit item limits and durable truncation splits bound output separately.
    # Characters are a heuristic, not a claimed token conversion.
    window = config.build.task_batch_chars
    context = min(config.build.compact_context_chars, window // 4)
    return {'version': 1, 'context_chars': context,
            'input_chars': window - context - 32,
            'unit_items': config.build.unit_batch_items,
            'digest_threshold_chars': min(config.build.digest_threshold_chars, window - context - 32), 'stages': {}}


def stage_plan(batches):
    return fingerprint(batches)


def new_stage(plan):
    return {'plan': plan, 'next': 0, 'memory': {'summary': '', 'facts': []},
            'ledger': [], 'coverage': []}


def _words(text):
    # Keep lookup cheap even when an original chunk contains long opaque bytes.
    text = unicodedata.normalize('NFKC', text).lower()
    words = set(re.findall(r'[a-z][a-z0-9_]{1,40}', text))
    for part in re.findall(r'[\u3400-\u9fff]+', text):
        words.update(part[i:i+2] for i in range(len(part) - 1))
    return words


def _origins(items):
    result = set()
    for item in items:
        for message in item.get('messages', []):
            if message.get('id'):
                result.add(Ref.parse(item['ref']).alias + ':' + str(message['id']))
    return sorted(result)


def context_for(stage, task, batch, budget):
    memory_budget = max(64, (budget - 80) // 2)
    context = {'memory': deepcopy(stage['memory']), 'recorded': [], 'memory_chars': memory_budget}
    if size(context) > budget:
        raise ValueError('Saved compact memory exceeds the frozen context budget')
    material = model_material(task, {'items': batch})
    words = _words(' '.join(item.get('body', '') for item in material['items']))
    origins = set(_origins(batch))
    ranked = sorted(enumerate(stage['ledger']), reverse=True, key=lambda pair: (
        bool(origins.intersection(pair[1].get('origins', []))),
        len(words.intersection(_words(pair[1]['entry']['body']))), pair[0]))
    for _, record in ranked:
        entry = record['entry']
        # Excerpts are explicitly marked; full validated entries remain in the ledger.
        candidate = {'id': record['id'], 'body': entry['body'][:480],
                     'excerpt': len(entry['body']) > 480, 'time': entry['time'], 'facts': entry['facts']}
        proposed = {**context, 'recorded': [*context['recorded'], candidate]}
        if size(proposed) <= budget:
            context = proposed
        if len(context['recorded']) == 4:
            break
    return context


def evidence_refs(batch, context):
    return list(dict.fromkeys([item['ref'] for item in batch] +
                              [ref for item in batch for ref in item.get('supporting_refs', [])] +
                              context['memory']['facts'] +
                              [ref for entry in context['recorded'] for ref in entry['facts']]))


def normalize_cited_coverage(output, material):
    """Repair bookkeeping only when actual citations prove full current coverage.

    The model chooses the events and their evidence. Missing evidence, outputs
    based only on prior context, and previous/ignored claims still need the
    ordinary validation path; this never adds facts or hides uncovered inputs.
    """
    entries = output.get('entries', output.get('units'))
    coverage = []
    used = set()
    for position, item in enumerate(material['items']):
        ref = Ref.parse(item['ref'])
        cited = [index for index, entry in enumerate(entries)
                 if any(ref.contains(fact) for fact in entry['facts'])]
        if not cited:
            return
        used.update(cited)
        coverage.append({'item': position, 'new': cited, 'previous': []})
    if used == set(range(len(entries))):
        output['coverage'] = coverage


def validate_coverage(output, material):
    """Every current input must map to new output or an explicitly shown old record."""
    context, items = material['context'], material['items']
    entries = output.get('entries', output.get('units'))
    if size(output['memory']) > context['memory_chars'] or not output['memory']['summary'].strip():
        raise ValueError('Compact memory exceeds its character budget or is blank')
    previous = {record['id'] for record in context['recorded']}
    seen, used = set(), set()
    for coverage in output['coverage']:
        item = coverage['item']
        if item in seen or not 0 <= item < len(items):
            raise ValueError('Compact coverage repeats or invents an input item')
        seen.add(item)
        ignored = coverage.get('ignored') == 'operational_context'
        if ignored and (material.get('material_kind') != 'agent_worklog' or coverage['new'] or coverage['previous']):
            raise ValueError('Only worklog context may be explicitly excluded from event extraction')
        if not coverage['new'] and not coverage['previous'] and not ignored:
            raise ValueError('Compact coverage must not silently discard material')
        if any(record not in previous for record in coverage['previous']):
            raise ValueError('Compact coverage cites an unseen previous record')
        for index in coverage['new']:
            if not 0 <= index < len(entries):
                raise ValueError('Compact coverage invents an output entry')
            if not any(Ref.parse(items[item]['ref']).contains(ref) for ref in entries[index]['facts']):
                raise ValueError('New compact output must cite its current input')
            used.add(index)
    if seen != set(range(len(items))) or used != set(range(len(entries))):
        raise ValueError('Compact coverage must account for every input and output')


def _entry_key(entry):
    # Collapse only an identical event with identical evidence. Different speakers,
    # dates, evidence, negations or corrections are never merged by this guard.
    value = deepcopy(entry)
    value['body'] = value['body'].strip()
    value['facts'] = sorted(merge_refs(value['facts']))
    if 'topics' in value:
        value['topics'] = sorted(value['topics'])
    return fingerprint(value)


def advance(stage, task, batch, output):
    updated = deepcopy(stage)
    entries = output['entries' if task == 'digest' else 'units']
    known = {_entry_key(record['entry']): record['id'] for record in updated['ledger']}
    ids, added = [], []
    for entry in entries:
        key = _entry_key(entry)
        identity = known.get(key)
        if identity is None:
            identity = ('D' if task == 'digest' else 'U') + str(len(updated['ledger']) + 1)
            record = {'id': identity, 'entry': deepcopy(entry), 'origins': _origins(batch), 'refs': []}
            updated['ledger'].append(record)
            added.append(record)
            known[key] = identity
        ids.append(identity)
    coverage = []
    for row in sorted(output['coverage'], key=lambda value: value['item']):
        item = batch[row['item']]
        coverage.append({'ref': item['ref'],
                         **({'source_slice': deepcopy(item['source_slice'])} if 'source_slice' in item else {}),
                         'records': list(dict.fromkeys([ids[index] for index in row['new']] + row['previous'])),
                         **({'ignored': row['ignored']} if row.get('ignored') else {})})
    updated['coverage'].append({'batch': fingerprint(batch), 'items': coverage})
    updated['memory'] = deepcopy(output['memory'])
    updated['next'] += 1
    return updated, added
