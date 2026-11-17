"""Lazy PostgreSQL control records with row-level change tracking.

Nested mutations mark only their owning record. Transaction rollback copies
only records actually changed, never the whole repository or completed jobs.
"""
from collections import OrderedDict
from collections.abc import MutableMapping
from copy import deepcopy

_MISSING = object()


def tracked(value, changed):
    if isinstance(value, dict):
        return TrackedDict(value, changed)
    if isinstance(value, list):
        return TrackedList(value, changed)
    return value


class TrackedDict(dict):
    def __init__(self, value, changed):
        self.changed = changed
        dict.__init__(self, ((k, tracked(v, changed)) for k, v in value.items()))

    def __deepcopy__(self, memo):
        return {k: deepcopy(v, memo) for k, v in self.items()}

    def __setitem__(self, key, value):
        self.changed()
        dict.__setitem__(self, key, tracked(value, self.changed))

    def __delitem__(self, key):
        self.changed()
        dict.__delitem__(self, key)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key, *default):
        if key not in self:
            if default:
                return default[0]
            raise KeyError(key)
        value = self[key]
        del self[key]
        return value

    def clear(self):
        for key in list(self):
            del self[key]


class TrackedList(list):
    def __init__(self, value, changed):
        self.changed = changed
        list.__init__(self, (tracked(v, changed) for v in value))

    def __deepcopy__(self, memo):
        return [deepcopy(v, memo) for v in self]

    def append(self, value):
        self.changed()
        list.append(self, tracked(value, self.changed))

    def extend(self, values):
        for value in values:
            self.append(value)

    def __setitem__(self, key, value):
        self.changed()
        list.__setitem__(self, key, [tracked(v, self.changed) for v in value]
                         if isinstance(key, slice) else tracked(value, self.changed))

    def __delitem__(self, key):
        self.changed()
        list.__delitem__(self, key)

    def insert(self, key, value):
        self.changed()
        list.insert(self, key, tracked(value, self.changed))

    def pop(self, index=-1):
        self.changed()
        return list.pop(self, index)

    def clear(self):
        self.changed()
        list.clear(self)

    def remove(self, value):
        self.changed()
        list.remove(self, value)

    def sort(self, *args, **kwargs):
        self.changed()
        list.sort(self, *args, **kwargs)

    def __iadd__(self, values):
        self.extend(values)
        return self


class RowMap(MutableMapping):
    def __init__(self, repository, namespace, *, catalog=False):
        self.repo, self.namespace, self.catalog = repository, namespace, catalog
        self.cache = OrderedDict()
        self.dirty = set()
        self.removed = set()

    def _remember(self, key, value):
        self.cache[key] = tracked(value, lambda: self.touch(key))
        return self.cache[key]

    def touch(self, key):
        for frame in self.repo._frames:
            identity = (self.namespace, key)
            if identity not in frame:
                frame[identity] = (self, deepcopy(self.cache[key]) if key in self.cache else _MISSING,
                                   key in self.dirty, key in self.removed)
        self.dirty.add(key)

    def __getitem__(self, key):
        with self.repo.lock():
            if key in self.removed:
                raise KeyError(key)
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            if self.catalog:
                row = self.repo.db.execute('SELECT payload FROM memu_catalog WHERE alias=%s', (key,)).fetchone()
            else:
                row = self.repo.db.execute('SELECT value AS payload FROM memu_control WHERE namespace=%s AND key=%s',
                                           (self.namespace, key)).fetchone()
            if row is None:
                raise KeyError(key)
            return self._remember(key, row['payload'])

    def __setitem__(self, key, value):
        with self.repo.lock():
            self.get(key)
            self.touch(key)
            self.removed.discard(key)
            self._remember(key, deepcopy(value))

    def __delitem__(self, key):
        with self.repo.lock():
            self[key]
            self.touch(key)
            self.removed.add(key)

    def __iter__(self):
        with self.repo.lock():
            if self.catalog:
                rows = self.repo.db.execute('SELECT alias AS key FROM memu_catalog ORDER BY alias').fetchall()
            else:
                rows = self.repo.db.execute('SELECT key FROM memu_control WHERE namespace=%s ORDER BY key',
                                           (self.namespace,)).fetchall()
            keys = dict.fromkeys([r['key'] for r in rows] + list(self.dirty))
            return iter([key for key in keys if key not in self.removed])

    def __len__(self):
        return sum(1 for _ in self)

    def __deepcopy__(self, memo):
        return {k: deepcopy(v, memo) for k, v in self.items()}

    def flush(self):
        for key in self.dirty:
            if self.catalog:
                if key in self.removed:
                    self.repo.db.execute('DELETE FROM memu_catalog WHERE alias=%s', (key,))
                    continue
                value = self.cache[key]
                self.repo.db.execute('''INSERT INTO memu_catalog(alias,path,kind,revision,payload)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT(alias) DO UPDATE SET
                    path=excluded.path,kind=excluded.kind,revision=excluded.revision,payload=excluded.payload''',
                    (key, value['path'], value['kind'], value['revision'], self.repo.json(value)))
            elif key in self.removed:
                self.repo.db.execute('DELETE FROM memu_control WHERE namespace=%s AND key=%s', (self.namespace, key))
            else:
                self.repo.db.execute('''INSERT INTO memu_control(namespace,key,value) VALUES (%s,%s,%s)
                    ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value''',
                    (self.namespace, key, self.repo.json(self.cache[key])))
                if self.namespace == 'writes':
                    from .refs import Ref
                    row = self.cache[key]
                    refs = row.get('source_refs', []) + row.get('digest_refs', []) + row.get('result', {}).get('units', [])
                    self.repo.db.execute('DELETE FROM memu_write_inputs WHERE request_id=%s', (key,))
                    for alias in {Ref.parse(ref).alias for ref in refs}:
                        self.repo.db.execute('INSERT INTO memu_write_inputs(request_id,alias) VALUES (%s,%s)', (key, alias))

    def committed(self):
        for key in self.removed:
            self.cache.pop(key, None)
        self.dirty.clear()
        self.removed.clear()
        # References retained by a running task must remain tracked. Eviction is
        # intentionally left to startup; completed job payloads are compacted.


class StateMap(MutableMapping):
    def __init__(self, repo):
        self.repo = repo
        self.scalars = repo.mapping('state')
        self.children = {key: repo.mapping(key) for key in (
            'writes', 'pending_views', 'staged_vectors', 'runtime_changed', 'dirty_files', 'worklog_evidence',
            'projection_updates', 'projection_receipts', 'conversation_turns', 'conversation_units',
            'edit_batches', 'edit_heads', 'runtime_pins', 'view_pins', 'unit_pins', 'unit_index_invalidated')}
        self.topics = TopicMap(repo)

    def __getitem__(self, key):
        if key == 'topic_registry':
            return self.topics
        return self.children[key] if key in self.children else self.scalars[key]

    def __setitem__(self, key, value):
        if key == 'topic_registry':
            for field in list(self.topics):
                del self.topics[field]
            self.topics.update(value)
        elif key in self.children:
            mapping = self.children[key]
            mapping.clear()
            mapping.update(value)
        else:
            self.scalars[key] = value

    def __delitem__(self, key):
        if key in self.children:
            self.children[key].clear()
        else:
            del self.scalars[key]

    def __iter__(self):
        return iter([*self.scalars, *self.children, 'topic_registry'])

    def __len__(self):
        return len(self.scalars) + len(self.children) + 1


class TopicMap(MutableMapping):
    def __init__(self, repo):
        self.children = {key: repo.mapping('topics_' + key) for key in ('definitions', 'seen')}
        self.scalars = repo.mapping('topics_state')

    def __getitem__(self, key):
        return self.children[key] if key in self.children else self.scalars[key]

    def __setitem__(self, key, value):
        if key in self.children:
            self.children[key].clear()
            self.children[key].update(value)
        else:
            self.scalars[key] = value

    def __delitem__(self, key):
        if key in self.children:
            self.children[key].clear()
        else:
            del self.scalars[key]

    def __iter__(self):
        return iter([*self.children, *self.scalars])

    def __len__(self):
        return len(self.children) + len(self.scalars)
