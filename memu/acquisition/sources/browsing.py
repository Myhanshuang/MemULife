"""Read-only chat browsing before import, with no Host, CAS, or durable cache.

The management layer authenticates the caller before calling these functions.
Readers are always narrowed again here. Preview cursors are process-local signed
tokens bound to the source configuration, one conversation and its time range.
"""

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
import sqlite3
import time

from ..adapters.base import AdapterError
from ..adapters.local.common import encode, native_id
from ..adapters.qq import QQAdapter
from ..adapters.wechat import WeChatAdapter
from ..adapters.wechat_catalog import conversation_catalog
from .reader import SelectedReader, prepare_reader
from .registry import get_source
from .selection import Selection

CHAT_ADAPTERS = frozenset({"qq.napcat", "wechat.localdb"})
MAX_PREVIEW_TEXT = 4096
MAX_PREVIEW_RECORD = 64 * 1024
MAX_CURSOR = 48 * 1024
_CURSOR_KEY = secrets.token_bytes(32)


@contextmanager
def _bounded_sqlite(opening, path, deadline):
    try:
        with opening(path) as connection:
            # Sorting must not spill previewed original bodies to temp files.
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA cache_size=-2048")
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_PREVIEW_RECORD)
            connection.set_progress_handler(
                lambda: int(time.monotonic() > deadline), 1000
            )
            yield connection
    except sqlite3.Error as error:
        if isinstance(error, sqlite3.DataError) or time.monotonic() > deadline:
            raise AdapterError("import_preview_budget_exceeded") from None
        raise


def _stamp(value):
    if type(value) not in (int, float):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _kind(adapter, value):
    if adapter == "qq.napcat":
        return value["type"]
    return "group" if value.endswith("@chatroom") else "private"


def _configured(config):
    adapter, values = config["adapter_id"], config["config"]
    if adapter == "qq.napcat":
        if values.get('browse_accessible'):
            return get_source(adapter).configured_scopes(values)
        return [
            {
                "id": f"{value['type']}:{value['id']}",
                "name": value["id"],
                "kind": _kind(adapter, value),
                "message_count": None,
                "first_at": None,
                "last_at": None,
            }
            for value in values["conversations"]
        ]
    if adapter == "wechat.localdb":
        return conversation_catalog(WeChatAdapter(values))
    raise AdapterError("import_browse_unsupported")


def _verify_qq(adapter):
    login, _ = adapter.rpc("get_login_info")
    if (
        not isinstance(login, dict)
        or native_id(login.get("user_id")) != adapter.account_id
    ):
        raise AdapterError("account_binding_mismatch")


def _qq_names(config, rows):
    adapter = QQAdapter(config["config"])
    adapter.http.max_bytes = 2 * 1024 * 1024
    try:
        _verify_qq(adapter)
        allowed = {row["id"]: row for row in rows}
        for kind, action, field, names in (
            ("private", "get_friend_list", "user_id", ("remark", "nickname")),
            ("group", "get_group_list", "group_id", ("group_name",)),
        ):
            if not any(row["kind"] == kind for row in rows):
                continue
            contacts, _ = adapter.rpc(action)
            if not isinstance(contacts, list):
                raise AdapterError("incompatible_schema")
            for contact in contacts:
                if not isinstance(contact, dict):
                    raise AdapterError("incompatible_schema")
                row = allowed.get(kind + ":" + str(native_id(contact.get(field))))
                if row:
                    name = next(
                        (
                            contact.get(key)
                            for key in names
                            if isinstance(contact.get(key), str)
                            and contact[key].strip()
                        ),
                        None,
                    )
                    if name:
                        row["name"] = name[:256]
                    members = contact.get("member_count")
                    if kind == "group" and type(members) is int and members >= 0:
                        row["member_count"] = members
    finally:
        adapter.close()


def _wechat_metadata(config, rows):
    adapter = WeChatAdapter(config["config"])
    shards = adapter._manifest()
    if len(shards) > 64:
        raise AdapterError("import_browse_budget_exceeded")
    warnings = set()
    totals = {row["id"]: {"count": 0, "first": None, "last": None} for row in rows}
    deadline = time.monotonic() + 3
    for shard in shards:
        warnings.update(adapter._freshness(shard))
        with _bounded_sqlite(
            adapter.access.sqlite, adapter.access.root / shard["path"], deadline
        ) as db:
            for row in rows:
                if time.monotonic() > deadline:
                    raise AdapterError("import_browse_budget_exceeded")
                conversation = row["id"].removeprefix("conversations:")
                schema = adapter._schema(db, conversation)
                if not schema:
                    continue
                table, fields = schema
                if "create_time" not in fields:
                    raise AdapterError("incompatible_schema")
                # No message body, attachment or sender row is opened here.
                value = db.execute(
                    f'SELECT count(*), min(create_time), max(create_time) FROM "{table}"'
                ).fetchone()
                total = totals[row["id"]]
                total["count"] += value[0]
                for name, stamp, choose in (
                    ("first", value[1], min),
                    ("last", value[2], max),
                ):
                    if type(stamp) in (int, float):
                        total[name] = (
                            stamp if total[name] is None else choose(total[name], stamp)
                        )
    for row in rows:
        value = totals[row["id"]]
        row.update(
            message_count=value["count"],
            first_at=_stamp(value["first"]),
            last_at=_stamp(value["last"]),
        )
    return warnings


def browse(config, *, cursor="", limit=50, query="", kind=""):
    """Conversation metadata only; upstream failure returns configured IDs plus a code.

    Returns items, next_cursor, content_persisted, metadata_available and warnings.
    IDs are stable Selection scope IDs. WeChat discovers the snapshot's readable
    conversations; only a later explicit selection can become an import job.
    """
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or not all(isinstance(value, str) for value in (cursor, query, kind))
        or len(cursor) > 2048
        or len(query) > 256
        or kind not in {"", "private", "group"}
    ):
        raise AdapterError("import_browse_invalid")
    rows = _configured(config)
    if len(rows) > 10000:
        raise AdapterError("import_browse_budget_exceeded")
    rows = sorted(
        (row for row in rows if not kind or row["kind"] == kind),
        key=lambda row: row["id"],
    )
    warnings = set()
    available = True
    if config["adapter_id"] == "qq.napcat" and not config['config'].get('browse_accessible'):
        # Names must be known before applying a name search. No history calls.
        try:
            _qq_names(config, rows)
        except AdapterError as error:
            available = False
            warnings.add(error.code)
    needle = query.strip().casefold()
    rows = [
        row
        for row in rows
        if row["id"] > cursor
        and (not needle or needle in (row["id"] + " " + row["name"]).casefold())
    ]
    more = len(rows) > limit
    rows = rows[:limit]
    if config["adapter_id"] == "wechat.localdb" and rows:
        try:
            warnings.update(_wechat_metadata(config, rows))
        except (AdapterError, OSError, sqlite3.Error) as error:
            available = False
            warnings.add(
                error.code
                if isinstance(error, AdapterError)
                else "import_metadata_unavailable"
            )
    return {
        "items": rows,
        "next_cursor": rows[-1]["id"] if more else None,
        "content_persisted": False,
        "metadata_available": available,
        "warnings": sorted(warnings),
    }


def _binding(config, selection):
    return hashlib.sha256(
        encode({"config": config, "selection": selection.model_dump(mode="json")})
    ).hexdigest()


def _cursor(value, binding):
    if not value:
        return None
    try:
        if not isinstance(value, str) or len(value) > MAX_CURSOR:
            raise ValueError()
        encoded, signature = value.split(".")
        if not hmac.compare_digest(
            hmac.new(_CURSOR_KEY, encoded.encode(), hashlib.sha256).hexdigest(),
            signature,
        ):
            raise ValueError()
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        if (
            payload["binding"] != binding
            or payload["expires_at"] < time.time()
            or not isinstance(payload["state"], dict)
        ):
            raise ValueError()
        return payload["state"]
    except (ValueError, KeyError, TypeError):
        raise AdapterError("import_preview_cursor_stale") from None


def _next_cursor(state, binding):
    value = (
        base64.urlsafe_b64encode(
            encode(
                {"binding": binding, "expires_at": time.time() + 600, "state": state}
            )
        )
        .decode()
        .rstrip("=")
    )
    result = (
        value + "." + hmac.new(_CURSOR_KEY, value.encode(), hashlib.sha256).hexdigest()
    )
    if len(result) > MAX_CURSOR:
        raise AdapterError("import_preview_budget_exceeded")
    return result


def preview(config, selection: Selection, *, cursor="", limit=30):
    """An explicit one-chat, date-bounded text preview, without raw blobs or media.

    Return item id, occurred_at, actor, text, truncated and attachment_count.
    An empty filtered page may have a next_cursor; coverage is always bounded.
    Each call reads at most one upstream page and does not fetch attachments.
    """
    if config["adapter_id"] not in CHAT_ADAPTERS:
        raise AdapterError("import_preview_unsupported")
    if len(selection.scopes) != 1 or type(limit) is not int or not 1 <= limit <= 30:
        raise AdapterError("import_preview_invalid")
    # Ignore attachment preference for a transient text preview. Scope and time
    # bounds remain identical to the eventual import selection.
    selection = selection.model_copy(update={"include_attachments": False})
    values = get_source(config["adapter_id"]).narrow_config(config["config"], selection)
    binding = _binding(config, selection)
    state = _cursor(cursor, binding)
    if config["adapter_id"] == "qq.napcat":
        adapter = QQAdapter(values)
        adapter.http.max_bytes = 2 * 1024 * 1024
    else:
        # Large raw records pause preview instead of spiking API memory; importing
        # keeps the source's normal, isolated-reader budgets.
        values["max_record_bytes"] = min(
            values.get("max_record_bytes", MAX_PREVIEW_RECORD), MAX_PREVIEW_RECORD
        )
        adapter = WeChatAdapter(values)
        opening = adapter.access.sqlite
        deadline = time.monotonic() + 3
        adapter.access.sqlite = lambda path: _bounded_sqlite(opening, path, deadline)
    try:
        if config["adapter_id"] == "qq.napcat":
            _verify_qq(adapter)
        prepare_reader(adapter, selection)
        page = SelectedReader(config, selection, reader=adapter).read(
            "backfill", state, limit
        )
    finally:
        if hasattr(adapter, "close"):
            adapter.close()
    if config["adapter_id"] == "qq.napcat":
        # History preview never resolves live recall events. Do not grow the
        # stateless pagination token with a message-alias ledger.
        page.cursor.get("upstream", {}).pop("aliases", None)
    items = []
    for record in page.records:
        actor = record.metadata.get("actor", {})
        body = record.text or ""
        items.append(
            {
                "id": record.object_id,
                "occurred_at": record.occurred_at,
                "actor": {
                    key: str(actor[key])[:256] if actor.get(key) is not None else None
                    for key in ("source_actor_id", "display_name", "role")
                },
                "text": body[:MAX_PREVIEW_TEXT],
                "truncated": len(body) > MAX_PREVIEW_TEXT,
                "attachment_count": len(record.attachments),
            }
        )
    items.sort(key=lambda item: (item["occurred_at"] or "", item["id"]))
    return {
        "items": items,
        "next_cursor": _next_cursor(page.cursor, binding) if page.has_more else None,
        "content_persisted": False,
        "coverage": "bounded",
        "direction": "older" if config["adapter_id"] == "qq.napcat" else "source",
        "warnings": page.warnings,
        "selection": selection.model_dump(mode="json"),
    }
