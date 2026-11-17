"""Read authorized, unlocked WeChat 4 message snapshots produced by a local helper.

This module does not extract keys, automate the client, or modify source files.
Snapshot freshness is an explicit helper assertion, separate from SQLite integrity.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone

from .base import AdapterError, Attachment, Page, Snapshot
from .local.common import LOCAL_CONFIG_SCHEMA, LocalAccess, digest, encode, native_id
from .local.sqlite import columns, json_value


MESSAGE_COLUMNS = (
    "local_id",
    "server_id",
    "sort_seq",
    "local_type",
    "real_sender_id",
    "create_time",
    "message_content",
    "compress_content",
    "packed_info_data",
)
REQUIRED_COLUMNS = {
    "local_id",
    "sort_seq",
    "local_type",
    "real_sender_id",
    "message_content",
}


class WeChatAdapter:
    adapter_id = "wechat.localdb"
    config_schema = LOCAL_CONFIG_SCHEMA | {
        "required": ["root", "account_id"],
        "properties": LOCAL_CONFIG_SCHEMA["properties"]
        | {
            "account_id": {"type": "string", "minLength": 1},
            "conversations": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "snapshot_manifest": {"type": "string", "minLength": 1},
            "allow_foreground_actions": {"const": False},
        },
    }

    def __init__(self, config: dict):
        self.config = config
        if config.get("allow_foreground_actions", False):
            raise AdapterError("foreground_actions_not_allowed")
        self.access = LocalAccess(config)
        self.account_id = native_id(config.get("account_id"))
        if not self.account_id:
            raise AdapterError("account_binding_required")
        self.conversations = config.get("conversations", [])
        if (
            not isinstance(self.conversations, list)
            or not all(isinstance(x, str) and x for x in self.conversations)
        ):
            raise AdapterError("conversation_scope_required")
        self.manifest = self.access.root / config.get(
            "snapshot_manifest", "snapshots.json"
        )
        self._status = "ready"

    def _manifest(self):
        try:
            value = json.loads(self.access.read_file(self.manifest))
        except (ValueError, UnicodeDecodeError):
            raise AdapterError("incompatible_snapshot_manifest") from None
        if (
            not isinstance(value, dict)
            or value.get("format") != "memulife.wechat-snapshots.v1"
        ):
            raise AdapterError("incompatible_snapshot_manifest")
        if value.get("account_id") != self.account_id:
            raise AdapterError("account_binding_mismatch")
        self.snapshot_manifest = value
        self._snapshot_attachments = value.get('attachments', {})
        shards = value.get("shards")
        if not isinstance(shards, list) or not shards:
            raise AdapterError("source_offline", retryable=True)
        seen = set()
        for shard in shards:
            if not isinstance(shard, dict) or not all(
                isinstance(shard.get(key), str) and shard[key]
                for key in ("shard_id", "path", "db_generation")
            ):
                raise AdapterError("incompatible_snapshot_manifest")
            if shard["shard_id"] in seen:
                raise AdapterError("duplicate_shard_identity")
            seen.add(shard["shard_id"])
            self.access.checked(self.access.root / shard["path"])
        return sorted(shards, key=lambda item: item["shard_id"])

    def _freshness(self, shard):
        warnings = []
        if (
            shard.get("wal_applied") is not True
            or shard.get("used_previous_snapshot") is not False
            or shard.get("freshness_state") != "fresh"
            or shard.get("validation_status") != "ok"
        ):
            warnings.append("stale_snapshot")
        warnings.extend(
            str(code)
            for code in shard.get("warning_codes", [])
            if isinstance(code, str)
        )
        return warnings

    def _table(self, conversation):
        # Only the fixed WeChat 4 table-name algorithm is accepted; no caller SQL.
        return "Msg_" + hashlib.md5(conversation.encode()).hexdigest()

    def _schema(self, connection, conversation):
        table = self._table(conversation)
        available = columns(connection, table)
        if not available:
            return None
        if not REQUIRED_COLUMNS <= available or "user_name" not in columns(
            connection, "Name2Id"
        ):
            raise AdapterError("incompatible_schema")
        selected = sorted(available)
        return table, selected

    def probe(self):
        shards = self._manifest()
        conversations = self.conversations
        if not conversations:
            from .wechat_catalog import conversation_catalog
            conversations = [row['id'].removeprefix('conversations:')
                             for row in conversation_catalog(self)]
        descriptors, warnings = [], []
        for shard in shards:
            warnings.extend(self._freshness(shard))
            try:
                with self.access.sqlite(self.access.root / shard["path"]) as connection:
                    check = connection.execute("PRAGMA quick_check(1)").fetchone()[0]
                    if check != "ok":
                        raise AdapterError("incompatible_schema")
                    schemas = [
                        schema
                        for conversation in conversations
                        if (schema := self._schema(connection, conversation))
                    ]
                    fingerprint = digest(encode(schemas))
                    expected = shard.get("schema_fingerprint")
                    if expected is not None and expected != fingerprint:
                        raise AdapterError("incompatible_schema")
                    descriptors.append(
                        {
                            "shard_id": shard["shard_id"],
                            "db_generation": shard["db_generation"],
                            "schema_fingerprint": fingerprint,
                            "conversation_tables": len(schemas),
                        }
                    )
            except sqlite3.Error:
                raise AdapterError("stale_snapshot", retryable=True) from None
        self._status = "stale_snapshot" if warnings else "ready"
        return {
            "adapter_id": self.adapter_id,
            "status": self._status,
            "profile": "wechat4-msg-v1",
            "account_id": self.account_id,
            "shards": descriptors,
            "warnings": sorted(set(warnings)),
            "device_verified": False,
            "capabilities": {
                "poll": True,
                "backfill": True,
                "reconcile": True,
                "read_only": True,
                "send_messages": False,
                "foreground_actions": False,
                "moments": False,
                "official_account_articles": False,
            },
            "conditions": [
                "requires separately authorized unlocked snapshot helper",
                "covers only locally synchronized messages",
                "Windows client device validation pending",
            ],
        }

    def health(self):
        return {
            "status": self._status,
            "foreground_actions": False,
            "send_messages": False,
        }

    def read(self, mode: str, cursor: dict | None = None, limit: int = 50) -> Page:
        if not self.conversations:
            raise AdapterError('conversation_scope_required')
        if mode not in {"discover", "live", "poll", "backfill", "reconcile"}:
            raise AdapterError("capability_unavailable")
        if not 1 <= limit <= 1000:
            raise AdapterError("invalid_limit")
        state = deepcopy(cursor or {})
        if state.get("account_id") not in {None, self.account_id}:
            raise AdapterError("account_binding_mismatch")
        if state.get("complete"):
            state = {"cycle": state.get("cycle", 0) + 1}
        state["account_id"] = self.account_id
        records, warnings = [], []
        shards = self._manifest()
        inventory = digest(
            encode([(shard["shard_id"], shard["db_generation"]) for shard in shards])
        )
        if state.get("inventory") not in {None, inventory}:
            state.pop("scopes", None)
            state["scope_index"] = 0
            warnings.append("snapshot_inventory_changed")
        state["inventory"] = inventory
        scopes = [
            (shard, conversation)
            for shard in shards
            for conversation in sorted(set(self.conversations))
        ]
        start = int(state.get("scope_index", 0))
        for index in range(start, len(scopes)):
            shard, conversation = scopes[index]
            warnings.extend(self._freshness(shard))
            scoped_key = f"{shard['shard_id']}:{conversation}"
            path = self.access.root / shard["path"]
            try:
                stat = self.access.checked(path).stat()
                physical_identity = f"{stat.st_dev}:{stat.st_ino}"
                with self.access.sqlite(path) as connection:
                    schema = self._schema(connection, conversation)
                    if schema is None:
                        # Conversations need not exist in every message shard.
                        # Selection validation already checks the full inventory.
                        state["scope_index"] = index + 1
                        continue
                    table, selected = schema
                    scoped = state.setdefault("scopes", {}).setdefault(scoped_key, {})
                    generation = shard["db_generation"]
                    if scoped.get("generation") not in {None, generation} or scoped.get(
                        "identity"
                    ) not in {None, physical_identity}:
                        scoped.clear()
                        warnings.append("snapshot_generation_changed")
                    scoped.update(generation=generation, identity=physical_identity)
                    if "through" not in scoped:
                        maximum = connection.execute(
                            f'SELECT sort_seq,local_id FROM "{table}" ORDER BY sort_seq DESC,local_id DESC LIMIT 1'
                        ).fetchone()
                        scoped["through"] = (
                            [str(x) for x in maximum] if maximum else ["-1", "-1"]
                        )
                    through = [int(x) for x in scoped["through"]]
                    after = [int(x) for x in scoped.get("after", ["-1", "-1"])]
                    selected_sql = ",".join(
                        '"' + name.replace('"', '""') + '"' for name in selected
                    )
                    selection = getattr(self, "import_selection", None)
                    time_sql, time_args = "", []
                    if selection:
                        for value, operator in (
                            (selection.start, ">="),
                            (selection.end, "<"),
                        ):
                            if value:
                                time_sql += f" AND create_time {operator} ?"
                                time_args.append(value.timestamp())
                    query = (
                        f'SELECT {selected_sql} FROM "{table}" '
                        "WHERE (sort_seq>? OR (sort_seq=? AND local_id>?)) "
                        "AND (sort_seq<? OR (sort_seq=? AND local_id<=?)) "
                        + time_sql
                        + " ORDER BY sort_seq,local_id LIMIT ?"
                    )
                    room = limit - len(records)
                    rows = connection.execute(
                        query,
                        (
                            after[0],
                            after[0],
                            after[1],
                            through[0],
                            through[0],
                            through[1],
                            *time_args,
                            room + 1,
                        ),
                    ).fetchall()
                    actors = {}
                    for sender_id in {row['real_sender_id'] for row in rows[:room]}:
                        actor = connection.execute(
                            "SELECT user_name FROM Name2Id WHERE rowid=?",
                            (sender_id,),
                        ).fetchone()
                        actors[sender_id] = actor[0] if actor else None
                    from .wechat_catalog import contact_names
                    names = contact_names(self, [conversation, *actors.values()]) if rows else {}
                    for row in rows[:room]:
                        record = self._row(
                            dict(row), shard, conversation, actors[row['real_sender_id']], names
                        )
                        records.append(record)
                        warnings.extend(record.metadata["warnings"])
                        scoped["after"] = [str(row["sort_seq"]), str(row["local_id"])]
                    state["scope_index"] = index
                    if len(rows) > room:
                        self._status = (
                            "stale_snapshot"
                            if "stale_snapshot" in warnings
                            else "backfilling"
                        )
                        return Page(records, state, True, False, sorted(set(warnings)))
                    state["scope_index"] = index + 1
            except (OSError, sqlite3.Error):
                # A temporarily unavailable shard is never an empty successful scan.
                raise AdapterError("stale_snapshot", retryable=True) from None
        state["complete"] = True
        self._status = "stale_snapshot" if "stale_snapshot" in warnings else "ready"
        return Page(records, state, False, not warnings, sorted(set(warnings)))

    def _row(self, row, shard, conversation, actor_id, names=None):
        names = names or {}
        raw_row = {key: json_value(value) for key, value in row.items()}
        for key in ("local_id", "server_id", "sort_seq", "real_sender_id"):
            if key in raw_row:
                raw_row[key] = native_id(raw_row[key])
        raw = encode(
            {
                "shard_id": shard["shard_id"],
                "db_generation": shard["db_generation"],
                "conversation": conversation,
                "row": raw_row,
            }
        )
        if len(raw) > self.access.max_bytes:
            raise AdapterError("record_too_large")
        server_id = native_id(row.get("server_id"))
        identity = (
            f"server:{server_id}"
            if server_id and server_id != "0"
            else (
                f"local:{shard['shard_id']}:{shard['db_generation']}:{row['local_id']}"
            )
        )
        content = row.get("message_content")
        text_warnings = []
        if isinstance(content, bytes):
            try:
                if content.startswith(b"\x28\xb5\x2f\xfd"):
                    import zstandard

                    size = zstandard.frame_content_size(content)
                    if (
                        size
                        not in {
                            zstandard.CONTENTSIZE_UNKNOWN,
                            zstandard.CONTENTSIZE_ERROR,
                        }
                        and size > self.access.max_bytes
                    ):
                        raise AdapterError("decompressed_budget_exceeded")
                    content = zstandard.ZstdDecompressor().decompress(
                        content, max_output_size=self.access.max_bytes
                    )
                content = content.decode("utf-8")
            except (ValueError, UnicodeError):
                content = None
                text_warnings.append("text_decode_unavailable")
        from .wechat_content import project_message

        body, attachments = project_message(content, row["local_type"])
        if actor_id is None:
            text_warnings.append("sender_mapping_unresolved")
        occurred = None
        if type(row.get("create_time")) is int:
            try:
                occurred = datetime.fromtimestamp(
                    row["create_time"], timezone.utc
                ).isoformat()
            except (ValueError, OSError, OverflowError):
                pass
        for attachment in attachments:
            attachment.metadata.update(shard_id=shard['shard_id'], local_id=str(row['local_id']),
                                       local_type=row['local_type'])
            item = getattr(self, '_snapshot_attachments', {}).get(
                f'{self.account_id}:{conversation}:{identity}/{attachment.attachment_id}')
            if item:
                attachment.media_type = item['media_type']
                attachment.metadata.update(item.get('metadata', {}))
                selection = getattr(self, 'import_selection', None)
                if selection and selection.include_attachments:
                    data = self.access.read_file(self.access.root / item['path'])
                    if digest(data) != item['sha256']:
                        raise AdapterError('attachment_revision_changed', retryable=True)
                    attachment.data, attachment.status = data, 'available'
                    attachment.metadata.pop('reason', None)
            selection = getattr(self, 'import_selection', None)
            if (attachment.metadata.get('media_kind') == 'emoji' and attachment.data is None
                    and selection and selection.include_attachments):
                from .wechat_media import resolve_emoji
                resolve_emoji(attachment, content, self.access)
        return Snapshot(
            f"{self.account_id}:{conversation}:{identity}",
            raw,
            body,
            occurred_at=occurred,
            attachments=attachments,
            metadata={
                "platform": "wechat",
                "conversation_id": conversation,
                "conversation_name": names.get(conversation),
                "shard_id": shard["shard_id"],
                "db_generation": shard["db_generation"],
                "sort_seq": str(row["sort_seq"]),
                "warnings": text_warnings,
                "identity_strength": "native"
                if server_id and server_id != "0"
                else "provisional",
                "actor": {
                    "source_actor_id": actor_id,
                    "role": "self"
                    if actor_id == self.account_id
                    else "other"
                    if actor_id
                    else "unknown",
                    "display_name": names.get(actor_id) or ("我自己" if actor_id == self.account_id else None),
                },
            },
        )

    def fetch_attachment(self, ref: dict) -> Attachment:
        # The helper may resolve a raw row to an existing file; no foreground
        # download is performed, and only explicit media roots authorize a read.
        return self.access.fetch_attachment(ref)
