"""PostgreSQL acquisition control; jobs, receipts and checkpoints update independently."""
from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..control import TrackedDict
from ..persistence import private_save

SCHEMA = '''
CREATE TABLE IF NOT EXISTS memu_acquisition_records (
    root text NOT NULL, namespace text NOT NULL, key text NOT NULL, value jsonb NOT NULL,
    PRIMARY KEY(root,namespace,key)
);
CREATE TABLE IF NOT EXISTS memu_acquisition_receipts (
    root text NOT NULL, job_id text NOT NULL, kind text NOT NULL, key text NOT NULL, value jsonb NOT NULL,
    PRIMARY KEY(root,job_id,kind,key)
);
CREATE TABLE IF NOT EXISTS memu_acquisition_watch_items (
    root text NOT NULL, watch_id text NOT NULL, object_id text NOT NULL, fingerprint text NOT NULL,
    PRIMARY KEY(root,watch_id,object_id)
);
'''
NAMESPACES = ('sources', 'jobs', 'subscriptions')


class ReceiptList(list):
    """Append-only receipt positions; a status change dirties only that receipt."""
    def __init__(self, store, job, values):
        self.store, self.job = store, job
        for index, value in enumerate(values):
            list.append(self, self.wrap(index, value))

    def wrap(self, index, value):
        identity = (self.job, 'writes', f'{index:012d}')
        return TrackedDict(value, lambda: self.store.dirty_receipts.add(identity))

    def append(self, value):
        index = len(self)
        list.append(self, self.wrap(index, value))
        self.store.dirty_receipts.add((self.job, 'writes', f'{index:012d}'))

    def extend(self, values):
        for value in values:
            self.append(value)

    def __deepcopy__(self, memo):
        return [deepcopy(value, memo) for value in self]


class ReceiptMap(dict):
    def __init__(self, store, job, values):
        self.store, self.job = store, job
        for key, value in values.items():
            dict.__setitem__(self, key, self.wrap(key, value))

    def wrap(self, key, value):
        return TrackedDict(value, lambda: self.store.dirty_receipts.add((self.job, 'push', key)))

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, self.wrap(key, value))
        self.store.dirty_receipts.add((self.job, 'push', key))

    def __deepcopy__(self, memo):
        return {key: deepcopy(value, memo) for key, value in self.items()}


class Records(dict):
    def __init__(self, store, namespace, values):
        self.store, self.namespace = store, namespace
        for key, value in values.items():
            dict.__setitem__(self, key, store.wrap(namespace, key, value))

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, self.store.wrap(self.namespace, key, value))
        self.store.dirty_records.add((self.namespace, key))
        if self.namespace == 'jobs':
            self.store.reset_receipts.add(key)
            self.store.mark_receipts(key)

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        self.store.dirty_records.add((self.namespace, key))
        if self.namespace == 'jobs':
            self.store.reset_receipts.add(key)

    def __deepcopy__(self, memo):
        return {key: deepcopy(value, memo) for key, value in self.items()}


def _snapshot(db, root):
    state = {'version': 1, **{name: {} for name in NAMESPACES}}
    for row in db.execute('SELECT namespace,key,value FROM memu_acquisition_records WHERE root=%s', (root,)):
        if row['namespace'] in NAMESPACES:
            state[row['namespace']][row['key']] = row['value']
    receipts = defaultdict(list)
    for row in db.execute('SELECT job_id,kind,key,value FROM memu_acquisition_receipts WHERE root=%s '
                          'ORDER BY job_id,kind,key', (root,)):
        job = state['jobs'].get(row['job_id'])
        if job is None:
            raise ValueError('Receipt belongs to an unknown acquisition job')
        if row['kind'] == 'writes':
            receipts[row['job_id']].append(row['value'])
        else:
            job.setdefault('receipts', {})[row['key']] = row['value']
    for key, job in state['jobs'].items():
        job.setdefault('progress', {})['write_results'] = receipts[key]
        job.setdefault('receipts', {})
    return state


class AcquisitionStore:
    def __init__(self, root, dsn):
        self.root = str(Path(root).resolve())
        self.lock = threading.RLock()
        self.db = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        self.db.execute(SCHEMA)
        self.dirty_records, self.dirty_receipts, self.reset_receipts = set(), set(), set()
        marker = Path(root) / 'control.postgres.json'
        old = Path(root) / 'control.json'
        identity = self.db.execute("SELECT value FROM memu_acquisition_records WHERE root=%s "
                                   "AND namespace='state' AND key='identity'", (self.root,)).fetchone()
        if marker.exists() and (not identity or json.loads(marker.read_text()) != identity['value']):
            self.db.close()
            raise ValueError('Authoritative acquisition PostgreSQL state is missing or belongs to another store')
        if identity:
            value = _snapshot(self.db, self.root)
        else:
            value = json.loads(old.read_text()) if old.exists() else {'version': 1}
        if value.get('version') != 1:
            raise ValueError('Unsupported acquisition state version')
        self.state = {'version': 1, **{name: Records(self, name, value.get(name, {})) for name in NAMESPACES}}
        if not identity:
            legacy = [*([old] if old.exists() else []), *(Path(root) / 'outbox').glob('*.json')]
            identity = {'value': {'root': self.root, 'id': uuid4().hex, 'version': 1,
                                 'legacy_files': {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                                                  for path in legacy}}}
            self.dirty_records.update((name, key) for name in NAMESPACES for key in self.state[name])
            for key in self.state['jobs']:
                self.mark_receipts(key)
            with self.db.transaction():
                self.save()
                for path in (Path(root) / 'outbox').glob('*.json'):
                    self.save_checkpoint(path.stem, json.loads(path.read_text()))
                self.db.execute("INSERT INTO memu_acquisition_records VALUES (%s,'state','identity',%s)",
                                (self.root, Jsonb(identity['value'])))
        if not marker.exists():
            private_save(marker, identity['value'])
        # Finish interrupted cleanup only for the exact bytes migrated. A newer
        # legacy file is never mistaken for the retired snapshot.
        for path in [old, *(Path(root) / 'outbox').glob('*.json')]:
            expected = identity['value'].get('legacy_files', {}).get(str(path.relative_to(root)))
            if expected and path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
                path.unlink()

    def wrap(self, namespace, key, value):
        changed = lambda: self.dirty_records.add((namespace, key))
        if namespace != 'jobs':
            return TrackedDict(value, changed)
        progress = value.get('progress', {})
        row = TrackedDict({**value, 'progress': {k: v for k, v in progress.items() if k != 'write_results'},
                           'receipts': {}}, changed)
        dict.__setitem__(row['progress'], 'write_results', ReceiptList(self, key, progress.get('write_results', [])))
        dict.__setitem__(row, 'receipts', ReceiptMap(self, key, value.get('receipts', {})))
        return row

    def mark_receipts(self, key):
        job = self.state['jobs'][key]
        self.dirty_receipts.update((key, 'writes', f'{i:012d}')
                                  for i in range(len(job['progress'].get('write_results', []))))
        self.dirty_receipts.update((key, 'push', batch) for batch in job['receipts'])

    def save(self):
        with self.lock, self.db.transaction():
            for job in self.reset_receipts:
                self.db.execute('DELETE FROM memu_acquisition_receipts WHERE root=%s AND job_id=%s', (self.root, job))
            for namespace, key in self.dirty_records:
                value = self.state[namespace].get(key)
                if value is None:
                    self.db.execute('DELETE FROM memu_acquisition_records WHERE root=%s AND namespace=%s AND key=%s',
                                    (self.root, namespace, key))
                    continue
                if namespace == 'jobs':
                    value = {**value, 'receipts': {}, 'progress':
                             {k: v for k, v in value.get('progress', {}).items() if k != 'write_results'}}
                self.db.execute('INSERT INTO memu_acquisition_records VALUES (%s,%s,%s,%s) '
                                'ON CONFLICT(root,namespace,key) DO UPDATE SET value=excluded.value',
                                (self.root, namespace, key, Jsonb(value)))
            for job_id, kind, key in self.dirty_receipts:
                job = self.state['jobs'].get(job_id)
                if job is None:
                    continue
                value = job['progress']['write_results'][int(key)] if kind == 'writes' else job['receipts'][key]
                self.db.execute('INSERT INTO memu_acquisition_receipts VALUES (%s,%s,%s,%s,%s) '
                                'ON CONFLICT(root,job_id,kind,key) DO UPDATE SET value=excluded.value',
                                (self.root, job_id, kind, key, Jsonb(value)))
        self.dirty_records.clear()
        self.dirty_receipts.clear()
        self.reset_receipts.clear()

    def load_checkpoint(self, key):
        with self.lock:
            row = self.db.execute("SELECT value FROM memu_acquisition_records WHERE root=%s "
                                  "AND namespace='outboxes' AND key=%s", (self.root, key)).fetchone()
            return row['value'] if row else None

    def watch_items(self, watch_id, identities):
        if not identities:
            return {}
        with self.lock:
            return {row['object_id']: row['fingerprint'] for row in self.db.execute(
                'SELECT object_id,fingerprint FROM memu_acquisition_watch_items '
                'WHERE root=%s AND watch_id=%s AND object_id=ANY(%s)',
                (self.root, watch_id, list(identities)))}

    def save_watch_items(self, watch_id, values):
        with self.lock, self.db.transaction():
            for identity, fingerprint in values.items():
                self.db.execute('INSERT INTO memu_acquisition_watch_items VALUES (%s,%s,%s,%s) '
                    'ON CONFLICT(root,watch_id,object_id) DO UPDATE SET fingerprint=excluded.fingerprint',
                    (self.root, watch_id, identity, fingerprint))

    def commit_watch_items(self, staged, watch_id):
        with self.lock, self.db.transaction():
            self.db.execute('INSERT INTO memu_acquisition_watch_items '
                'SELECT root,%s,object_id,fingerprint FROM memu_acquisition_watch_items '
                'WHERE root=%s AND watch_id=%s '
                'ON CONFLICT(root,watch_id,object_id) DO UPDATE SET fingerprint=excluded.fingerprint',
                (watch_id, self.root, staged))
            self.db.execute('DELETE FROM memu_acquisition_watch_items WHERE root=%s AND watch_id=%s',
                (self.root, staged))

    def save_checkpoint(self, key, value):
        with self.lock:
            self.db.execute("INSERT INTO memu_acquisition_records VALUES (%s,'outboxes',%s,%s) "
                            'ON CONFLICT(root,namespace,key) DO UPDATE SET value=excluded.value',
                            (self.root, key, Jsonb(value)))

    def close(self):
        self.db.close()


def load_control(root, dsn=None):
    root = Path(root).resolve()
    if not (root / 'control.postgres.json').exists():
        return json.loads((root / 'control.json').read_text())
    if not dsn:
        raise ValueError('PostgreSQL is required for acquisition control')
    with psycopg.connect(dsn, row_factory=dict_row) as db:
        return _snapshot(db, str(root))


def save_control(root, value, dsn=None, *, clear_outboxes=False):
    """Offline maintenance only: replace a complete, explicitly edited control snapshot."""
    if not (Path(root) / 'control.postgres.json').exists():
        private_save(Path(root) / 'control.json', value)
        return
    store = AcquisitionStore(root, dsn)
    try:
        with store.lock, store.db.transaction():
            store.db.execute('DELETE FROM memu_acquisition_receipts WHERE root=%s', (store.root,))
            namespaces = [*NAMESPACES, *(['outboxes'] if clear_outboxes else [])]
            store.db.execute('DELETE FROM memu_acquisition_records WHERE root=%s AND namespace=ANY(%s)',
                             (store.root, namespaces))
            store.state = {'version': 1, **{name: Records(store, name, {}) for name in NAMESPACES}}
            for name in NAMESPACES:
                for key, row in value.get(name, {}).items():
                    store.state[name][key] = row
            store.save()
    finally:
        store.close()
