"""Allowlisted SQLite/JSON profiles; source databases are never copied or written."""

from __future__ import annotations

import base64
import json
import sqlite3
from copy import deepcopy

from ..base import AdapterError, Page, Snapshot
from .common import (
    LOCAL_CONFIG_SCHEMA,
    LocalAccess,
    digest,
    encode,
    native_id,
    text_parts,
)


# Fixed chat column names are intentionally separate from database discovery.
OPEN_CODE_TABLES = {
    "session": (
        "id",
        "project_id",
        "parent_id",
        "directory",
        "title",
        "version",
        "time_created",
        "time_updated",
        "time_archived",
        "summary",
    ),
    "message": ("id", "session_id", "time_created", "time_updated", "data"),
    "part": ("id", "message_id", "session_id", "time_created", "time_updated", "data"),
    "session_v2": (
        "id",
        "project_id",
        "parent_id",
        "directory",
        "title",
        "version",
        "time_created",
        "time_updated",
        "data",
    ),
    "session_message": (
        "id",
        "session_id",
        "type",
        "seq",
        "time_created",
        "time_updated",
        "data",
    ),
}


def columns(connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def json_value(value):
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode()}
    return value


def parse_data(value):
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    return value


class SQLiteSource:
    adapter_id = "local.sqlite"

    def __init__(self, config: dict):
        self.config = config
        self.access = LocalAccess(config)
        self._status = "ready"

    def health(self):
        return {"status": self._status, "read_only": True, "foreground_actions": False}

    def fetch_attachment(self, ref):
        return self.access.fetch_attachment(ref)

    def _validate(self, mode, limit):
        if mode not in {"discover", "live", "poll", "backfill", "reconcile"}:
            raise AdapterError("capability_unavailable")
        if not 1 <= limit <= 1000:
            raise AdapterError("invalid_limit")


class OpenCodeAdapter(SQLiteSource):
    adapter_id = "local.opencode"
    config_schema = LOCAL_CONFIG_SCHEMA | {
        "properties": LOCAL_CONFIG_SCHEMA["properties"]
        | {
            "database": {"type": "string", "minLength": 1},
            "profile": {"enum": ["auto", "sqlite-v1", "sqlite-v2", "legacy-json"]},
        }
    }

    def __init__(self, config):
        super().__init__(config)
        self.database = self.access.checked(
            self.access.root / config.get("database", "opencode.db")
        )
        self.profile = config.get("profile", "auto")
        if self.profile not in {"auto", "sqlite-v1", "sqlite-v2", "legacy-json"}:
            raise AdapterError("incompatible_profile")

    def _tables(self, connection):
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        available = {}
        for table, allowed in OPEN_CODE_TABLES.items():
            if table not in names:
                continue
            present = columns(connection, table)
            required = {"id"} | (
                {"data", "session_id"}
                if table in {"message", "part", "session_message"}
                else set()
            )
            if not required <= present:
                raise AdapterError("incompatible_schema")
            available[table] = [name for name in allowed if name in present]
        if (
            self.profile == "sqlite-v1"
            and not {"session", "message", "part"} <= available.keys()
        ):
            raise AdapterError("incompatible_schema")
        if (
            self.profile == "sqlite-v2"
            and not {"session_v2", "session_message"} <= available.keys()
        ):
            raise AdapterError("incompatible_schema")
        if not available:
            raise AdapterError("incompatible_schema")
        return available

    def probe(self):
        profiles = []
        fingerprint = None
        if self.profile != "legacy-json" and self.database.exists():
            with self.access.sqlite(self.database) as connection:
                tables = self._tables(connection)
            fingerprint = digest(encode(tables))
            if "message" in tables:
                profiles.append("sqlite-v1")
            if "session_message" in tables:
                profiles.append("sqlite-v2")
        if self._legacy_files():
            profiles.append("legacy-json")
        if not profiles:
            raise AdapterError("source_offline", retryable=True)
        return {
            "adapter_id": self.adapter_id,
            "status": "ready",
            "profiles": profiles,
            "schema_fingerprint": fingerprint,
            "device_verified": False,
            "capabilities": {
                "poll": True,
                "backfill": True,
                "reconcile": True,
                "read_only": True,
                "subscribe": False,
            },
            "conditions": [
                "allowlisted chat rows only",
                "full census catches in-place edits",
                "migration scopes retained independently until aliases verified",
            ],
        }

    def _legacy_files(self):
        if self.profile not in {"auto", "legacy-json"}:
            return []
        return self.access.files(
            (
                "storage/session/**/*.json",
                "storage/message/**/*.json",
                "storage/part/**/*.json",
            )
        )

    def read(self, mode, cursor=None, limit=50):
        self._validate(mode, limit)
        state = deepcopy(cursor or {})
        binding = f"{self.access.root}:{self.profile}"
        if state.get("binding") not in {None, binding}:
            raise AdapterError("source_binding_mismatch")
        state["binding"] = binding
        if state.get("complete"):
            state = {"binding": binding, "cycle": state.get("cycle", 0) + 1}
        records = []
        if (
            self.profile != "legacy-json"
            and self.database.exists()
            and not state.get("db_complete")
            and (
                not getattr(self, "import_selection", None)
                or str(self.database.relative_to(self.access.root))
                in self.import_selection.scopes
            )
        ):
            try:
                with self.access.sqlite(self.database) as connection:
                    tables = self._tables(connection)
                    ordered = list(tables)
                    start = int(state.get("table_index", 0))
                    for index in range(start, len(ordered)):
                        table = ordered[index]
                        selected = ",".join(f'"{name}"' for name in tables[table])
                        after = state.get("after", "") if index == start else ""
                        rows = connection.execute(
                            f'SELECT {selected} FROM "{table}" WHERE id > ? ORDER BY id LIMIT ?',
                            (after, limit - len(records) + 1),
                        ).fetchall()
                        room = limit - len(records)
                        for row in rows[:room]:
                            snapshot = self._row(table, dict(row))
                            if table == 'part' and 'message' in tables and row['message_id']:
                                parent = connection.execute('SELECT data FROM message WHERE id=?', (row['message_id'],)).fetchone()
                                parent_data = parse_data(parent['data']) if parent else None
                                if isinstance(parent_data, dict):
                                    snapshot.metadata['parent_role'] = parent_data.get('role')
                            records.append(snapshot)
                            state["after"] = row["id"]
                        state["table_index"] = index
                        if len(rows) > room:
                            return Page(records, state, True, False)
                        state.pop("after", None)
                        state["table_index"] = index + 1
                    state["db_complete"] = True
            except sqlite3.Error:
                raise AdapterError("stale_snapshot", retryable=True) from None
        legacy = self._legacy_files()
        for path in legacy:
            key = str(path.relative_to(self.access.root))
            if key <= state.get("legacy_after", ""):
                continue
            if len(records) == limit:
                return Page(records, state, True, False)
            raw = self.access.read_file(path)
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise AdapterError("incompatible_schema") from None
            if not isinstance(value, dict) or not native_id(value.get("id")):
                raise AdapterError("incompatible_schema")
            kind = path.relative_to(self.access.root).parts[1]
            records.append(
                Snapshot(
                    f"legacy:{kind}:{value['id']}",
                    raw,
                    text_parts(value),
                    metadata={"profile": "legacy-json", "file": key},
                )
            )
            state["legacy_after"] = key
        if not records and not legacy and not self.database.exists():
            raise AdapterError("source_offline", retryable=True)
        state["complete"] = True
        return Page(records, state, False, True)

    def _row(self, table, row):
        value = {key: json_value(value) for key, value in row.items()}
        raw = encode(value)
        if len(raw) > self.access.max_bytes:
            raise AdapterError("record_too_large")
        data = parse_data(row.get("data"))
        role = data.get("role", "unknown") if isinstance(data, dict) else "unknown"
        if role not in {"user", "assistant", "system"}:
            role = "unknown"
        return Snapshot(
            f"{table}:{row['id']}",
            raw,
            text_parts(data),
            metadata={
                "database": str(self.database.relative_to(self.access.root)),
                "table": table,
                "native_id": str(row["id"]),
                "session_id": row.get("session_id"),
                "source_seq": row.get("seq"),
                "actor": {"source_actor_id": None, "role": role, "display_name": None},
            },
        )


class CursorAdapter(SQLiteSource):
    adapter_id = "local.cursor"
    config_schema = LOCAL_CONFIG_SCHEMA | {
        "properties": LOCAL_CONFIG_SCHEMA["properties"]
        | {
            "databases": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
        }
    }
    prefixes = ("composerData:", "bubbleId:")

    def _databases(self):
        configured = self.config.get("databases")
        if configured is not None:
            if not isinstance(configured, list) or not configured:
                raise AdapterError("database_scope_required")
            return [self.access.checked(self.access.root / path) for path in configured]
        return self.access.files(
            (
                "User/globalStorage/state.vscdb",
                "User/workspaceStorage/*/state.vscdb",
                "globalStorage/state.vscdb",
                "workspaceStorage/*/state.vscdb",
            )
        )

    def _tables(self, connection):
        tables = []
        for table in ("ItemTable", "cursorDiskKV"):
            present = columns(connection, table)
            if present and not {"key", "value"} <= present:
                raise AdapterError("incompatible_schema")
            if present:
                tables.append(table)
        if not tables:
            raise AdapterError("incompatible_schema")
        return tables

    def probe(self):
        databases = self._databases()
        if not databases:
            raise AdapterError("source_offline", retryable=True)
        fingerprints = []
        for path in databases:
            with self.access.sqlite(path) as connection:
                tables = self._tables(connection)
                recognized = sum(
                    connection.execute(
                        f"SELECT count(*) FROM \"{table}\" WHERE key GLOB 'composerData:*' OR key GLOB 'bubbleId:*'"
                    ).fetchone()[0]
                    for table in tables
                )
                fingerprints.append(digest(encode(tables)))
                if not recognized:
                    raise AdapterError("incompatible_schema")
        return {
            "adapter_id": self.adapter_id,
            "status": "partial_coverage",
            "profile": "internal-kv-v1",
            "schema_fingerprints": fingerprints,
            "device_verified": False,
            "capabilities": {
                "poll": True,
                "backfill": True,
                "reconcile": True,
                "read_only": True,
            },
            "conditions": [
                "experimental internal storage",
                "composerData/bubbleId records only",
                "unknown keys never treated as empty complete history",
            ],
        }

    def read(self, mode, cursor=None, limit=50):
        self._validate(mode, limit)
        state = deepcopy(cursor or {})
        binding = str(self.access.root)
        if state.get("binding") not in {None, binding}:
            raise AdapterError("source_binding_mismatch")
        state["binding"] = binding
        if state.get("complete"):
            state = {"binding": binding, "cycle": state.get("cycle", 0) + 1}
        records = []
        databases = self._databases()
        if not databases:
            raise AdapterError("source_offline", retryable=True)
        for path in databases:
            relative = str(path.relative_to(self.access.root))
            try:
                with self.access.sqlite(path) as connection:
                    for table in self._tables(connection):
                        scope = f"{relative}:{table}"
                        if scope < state.get("scope", ""):
                            continue
                        after = (
                            state.get("after", "")
                            if scope == state.get("scope")
                            else ""
                        )
                        rows = connection.execute(
                            f"SELECT key,value FROM \"{table}\" WHERE key>? AND (key GLOB 'composerData:*' OR key GLOB 'bubbleId:*') ORDER BY key LIMIT ?",
                            (after, limit - len(records) + 1),
                        ).fetchall()
                        room = limit - len(records)
                        for row in rows[:room]:
                            raw_value = row["value"]
                            value = parse_data(raw_value)
                            if not isinstance(value, dict):
                                raise AdapterError("incompatible_schema")
                            raw = encode(
                                {"key": row["key"], "value": json_value(raw_value)}
                            )
                            if len(raw) > self.access.max_bytes:
                                raise AdapterError("record_too_large")
                            # A provider-native key is the only cross-workspace identity.
                            records.append(
                                Snapshot(
                                    row["key"],
                                    raw,
                                    text_parts(value),
                                    metadata={
                                        "database": relative,
                                        "profile": "internal-kv-v1",
                                    },
                                )
                            )
                            state.update(scope=scope, after=row["key"])
                        if len(rows) > room:
                            return Page(
                                records, state, True, False, ["experimental_profile"]
                            )
            except sqlite3.Error:
                raise AdapterError("stale_snapshot", retryable=True) from None
        state["complete"] = True
        self._status = "partial_coverage"
        return Page(records, state, False, False, ["experimental_profile"])
