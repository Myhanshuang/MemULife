"""Stable edit fingerprints and protected memory entries; no model decisions."""
from copy import deepcopy
import hashlib
import json
import uuid

ENTRY_PREFIX = '!memu-entry-v2 '
EDITABLE_VIEWS = {'day', 'month', 'runtime'}


def pins_for(repo, alias):
    namespace = 'runtime_pins' if repo.catalog[alias]['metadata'].get('view') == 'runtime' else 'view_pins'
    return repo.state.get(namespace, {}).get(alias, {})


def visible_record(record, pending=None):
    if not pending or not pending.get('blocked'):
        return record
    entries = [entry for entry in record.get('entries', []) if entry.get('protected')]
    if not entries:
        return None
    return dict(record, entries=entries, body='\n'.join(entry['body'] for entry in entries),
                facts=list(dict.fromkeys(f for e in entries for f in e['facts'] if f not in e['invalid_refs'])),
                withheld_unprotected=True)


def edit_revision(value, head=None):
    fields = {key: value.get(key) for key in ('body', 'facts', 'relations', 'topics', 'time', 'protected')}
    fields['edit_head'] = head
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def entry_line(entry):
    value = {key: entry[key] for key in ('body', 'facts', 'relations', 'topics', 'time', 'source')}
    # ASCII JSON also escapes Unicode line separators; exactly one physical line.
    return ENTRY_PREFIX + json.dumps(value, ensure_ascii=True, separators=(',', ':')) + ' ;;\n'


def view_entries(repo, alias, entries, header):
    ids = header.get('entry_ids', [])
    pins = pins_for(repo, alias)
    result = []
    for index, raw in enumerate(entries):
        entry = deepcopy(raw)
        entry['entry_id'] = ids[index] if index < len(ids) else uuid.uuid5(
            uuid.NAMESPACE_URL, f'memu:{alias}:{index}:{edit_revision(entry)}').hex
        entry['protected'] = entry['entry_id'] in pins
        head = repo.state.get('edit_heads', {}).get(alias + '/' + entry['entry_id'], {})
        entry['edit_revision'] = edit_revision(entry, head.get('transaction_id'))
        invalid = []
        for ref in entry.get('facts', []):
            try:
                target = repo.read(ref)
                if target['kind'] != 'unit':
                    invalid.append(ref)
            except ValueError:
                invalid.append(ref)
        entry['invalid_refs'] = invalid
        result.append(entry)
    return result


def preserve_view(repo, alias, entries):
    """Every view writer goes through this boundary, including auto rebuilds."""
    old = repo.read(alias)['entries'] if alias and repo._exists(repo.catalog[alias]['path']) else []
    pins = pins_for(repo, alias) if alias else {}
    result, used = [], set()
    covered = {ref for entry in pins.values() for ref in entry['facts']}
    for raw in entries:
        entry = deepcopy(raw)
        identity = entry.get('entry_id')
        if identity in pins or (not identity and entry.get('facts') and set(entry['facts']) <= covered):
            continue
        if not identity:
            match = next((v for v in old if v['entry_id'] not in used and v['entry_id'] not in pins
                          and v['facts'] == entry.get('facts', []) and v['time'] == entry.get('time')), None)
            identity = match['entry_id'] if match else uuid.uuid4().hex
        if identity in used:
            raise ValueError('Duplicate view entry ID')
        entry['entry_id'] = identity
        result.append(entry)
        used.add(identity)
    result.extend(deepcopy(list(pins.values())))
    return result
