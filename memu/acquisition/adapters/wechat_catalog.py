"""Read conversation identities and display names without opening message bodies."""

from contextlib import contextmanager
import sqlite3
import time

from .base import AdapterError
from .local.sqlite import columns


@contextmanager
def _metadata_db(adapter, path, deadline):
    with adapter.access.sqlite(path) as db:
        db.execute('PRAGMA temp_store=MEMORY')
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64 * 1024)
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        yield db


def contact_names(adapter, identities):
    """Resolve only the selected page's identities, never other message bodies."""
    wanted = sorted({value for value in identities if isinstance(value, str) and value})
    names = {}
    deadline = time.monotonic() + 3
    for descriptor in adapter.snapshot_manifest.get('databases', []):
        if descriptor.get('source_path') != 'contact/contact.db' or not wanted:
            continue
        with _metadata_db(adapter, adapter.access.root / descriptor['path'], deadline) as db:
            fields = columns(db, 'contact')
            if 'username' not in fields:
                continue
            labels = [field for field in ('remark', 'nick_name', 'alias') if field in fields]
            selected = ','.join('"' + field + '"' for field in ['username', *labels])
            for start in range(0, len(wanted), 500):
                batch = wanted[start:start + 500]
                placeholders = ','.join('?' for _ in batch)
                query = f'SELECT {selected} FROM contact WHERE username IN ({placeholders})'
                for row in db.execute(query, batch):
                    names[row[0]] = next((' '.join(name.split())[:256] for name in row[1:]
                                         if isinstance(name, str) and name.strip()), row[0])
    return names


def conversation_catalog(adapter):
    """List every readable chat in the bound snapshot; this is not an import scope."""
    shards = adapter._manifest()
    if len(shards) > 64:
        raise AdapterError('import_browse_budget_exceeded')
    deadline = time.monotonic() + 3
    names = {}
    known = {adapter.account_id, *adapter.conversations}
    for descriptor in adapter.snapshot_manifest.get('databases', []):
        if descriptor.get('source_path') != 'contact/contact.db':
            continue
        with _metadata_db(adapter, adapter.access.root / descriptor['path'], deadline) as db:
            fields = columns(db, 'contact')
            if 'username' not in fields:
                continue
            labels = [field for field in ('remark', 'nick_name', 'alias') if field in fields]
            selected = ','.join('"' + field + '"' for field in ['username', *labels])
            for index, row in enumerate(db.execute(f'SELECT {selected} FROM contact')):
                if index >= 10000 or time.monotonic() > deadline:
                    raise AdapterError('import_browse_budget_exceeded')
                if isinstance(row[0], str) and row[0]:
                    known.add(row[0])
                    names[row[0]] = next((name.strip()[:256] for name in row[1:]
                                         if isinstance(name, str) and name.strip()), row[0])
    tables = set()
    for shard in shards:
        with _metadata_db(adapter, adapter.access.root / shard['path'], deadline) as db:
            tables.update(row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'Msg_*'"))
            if 'user_name' not in columns(db, 'Name2Id'):
                raise AdapterError('incompatible_schema')
            for row in db.execute('SELECT user_name FROM Name2Id'):
                if isinstance(row[0], str) and row[0]:
                    known.add(row[0])
                if len(known) > 10000 or time.monotonic() > deadline:
                    raise AdapterError('import_browse_budget_exceeded')
    return [
        {'id': 'conversations:' + value,
         'name': '我自己' if value == adapter.account_id else names.get(
             value, '文件传输助手' if value == 'filehelper' else value),
         'kind': 'group' if value.endswith('@chatroom') else 'private',
         'message_count': None, 'first_at': None, 'last_at': None}
        for value in sorted(known) if adapter._table(value) in tables
    ]
