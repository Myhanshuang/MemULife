"""Bounded names for configured roots; never fetch blocks, children or media."""

from contextlib import contextmanager
from datetime import datetime
import time
from urllib.parse import quote

import httpx

from ..adapters.base import AdapterError

MAX_ROOTS = 20
MAX_METADATA_BYTES = 512 * 1024
DEADLINE_SECONDS = 12
LABELS = {
    "page_ids": "页面",
    "database_ids": "数据库",
    "data_source_ids": "数据源",
    "document_ids": "文档",
    "folder_tokens": "文件夹",
    "wiki_spaces": "知识库",
    "album_ids": "相册",
    "all_assets": "全部资产",
}
STOP_ERRORS = {
    "auth_required",
    "account_binding_mismatch",
    "account_scope_mismatch",
    "throttled",
    "source_offline",
}


def safe_time(value):
    if not isinstance(value, str):
        return None
    try:
        return (
            value
            if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo
            else None
        )
    except ValueError:
        return None


def title_fields(value, updated_at=None, revision=None):
    title = value.strip() if isinstance(value, str) else ""
    truncated = len(title) > 300
    return {
        "title": (title[:299] + "…" if truncated else title) or None,
        "title_truncated": truncated,
        "updated_at": safe_time(updated_at),
        "revision": revision,
        "metadata_status": "available" if title else "partial",
    }


def plain_title(values):
    parts = []
    for value in values:
        if not isinstance(value, dict):
            raise AdapterError("incompatible_schema")
        part = value.get("plain_text")
        if part is None and isinstance(value.get("text"), dict):
            part = value["text"].get("content", "")
        if not isinstance(part, str):
            raise AdapterError("incompatible_schema")
        parts.append(part)
    return "".join(parts)


def restrict(reader):
    reader.http.max_bytes = min(reader.http.max_bytes, MAX_METADATA_BYTES)
    reader.http.client.timeout = httpx.Timeout(5, connect=2)


@contextmanager
def notion_lookup(config):
    from ..adapters.notion import NotionAdapter, native_id

    reader = NotionAdapter(config)
    try:
        restrict(reader)
        reader.probe()

        def lookup(row):
            field, identity = row["id"].split(":", 1)
            entity, collection = {
                "page_ids": ("page", "pages"),
                "database_ids": ("database", "databases"),
                "data_source_ids": ("data_source", "data_sources"),
            }[field]
            identity = native_id(identity)
            value = reader.request("GET", f"/{collection}/{identity}")
            if value.get("object") != entity or native_id(value.get("id")) != identity:
                raise AdapterError("source_binding_mismatch")
            values = value.get("title", [])
            if entity == "page":
                properties = value.get("properties", {})
                if not isinstance(properties, dict):
                    raise AdapterError("incompatible_schema")
                values = next(
                    (
                        p.get("title", [])
                        for p in properties.values()
                        if isinstance(p, dict) and p.get("type") == "title"
                    ),
                    [],
                )
            if not isinstance(values, list):
                raise AdapterError("incompatible_schema")
            return title_fields(plain_title(values), value.get("last_edited_time"))

        yield lookup
    finally:
        reader.close()


@contextmanager
def feishu_lookup(config):
    from ..adapters.feishu import FeishuAdapter

    reader = FeishuAdapter(config)
    try:
        restrict(reader)
        reader._verify_identity()

        def lookup(row):
            field, identity = row["id"].split(":", 1)
            if field != "document_ids":
                return {
                    "metadata_status": "unsupported",
                    "error_code": "metadata_unavailable",
                }
            value, _ = reader._get("/docx/v1/documents/" + quote(identity, safe=""))
            document = value.get("document")
            if (
                not isinstance(document, dict)
                or document.get("document_id") != identity
            ):
                raise AdapterError("source_binding_mismatch")
            revision = document.get("revision_id")
            return title_fields(
                document.get("title"),
                revision=str(revision) if type(revision) is int else None,
            )

        yield lookup
    finally:
        reader.close()


def candidates(
    source, binding, *, cursor="", limit=100, query="", kind="", lookup=None
):
    """Search each bounded root window, continuing even when no names match it.

    Cursor is the last inspected configured root, never a remotely discovered ID.
    Validation and plan creation keep using local scopes and do not contact APIs.
    """
    if not 1 <= limit <= 200:
        raise AdapterError("invalid_limit")
    rows = sorted(source.configured_scopes(binding["config"]), key=lambda r: r["id"])
    rows = [
        row
        for row in rows
        if row["id"] > cursor and (not kind or row.get("kind") == kind)
    ]
    window = [
        dict(
            row,
            type_label=LABELS.get(row.get("kind")),
            title=None,
            updated_at=None,
            metadata_status="unsupported",
        )
        for row in rows[: min(limit, MAX_ROOTS)]
    ]
    deadline = time.monotonic() + DEADLINE_SECONDS
    warnings = set()
    inspected = len(window)
    if window and lookup:
        try:
            with lookup(binding["config"]) as resolve:
                for index, row in enumerate(window):
                    if time.monotonic() >= deadline:
                        inspected = index
                        warnings.add("metadata_budget_exceeded")
                        break
                    try:
                        row.update(resolve(row))
                        if row.get("title"):
                            row["name"] = row["title"]
                        if row.get("error_code"):
                            warnings.add(row["error_code"])
                    except AdapterError as error:
                        row.update(metadata_status="unavailable", error_code=error.code)
                        warnings.add(error.code)
                        if error.code in STOP_ERRORS:
                            for remaining in window[index + 1 :]:
                                remaining.update(
                                    metadata_status="unavailable", error_code=error.code
                                )
                            break
        except AdapterError as error:
            warnings.add(error.code)
            for row in window:
                row.update(metadata_status="unavailable", error_code=error.code)
    elif window:
        warnings.add("metadata_unavailable")
        for row in window:
            row["error_code"] = "metadata_unavailable"
    # An expired identity check still returns IDs and a usable continuation.
    if inspected == 0 and window:
        inspected = 1
        window[0].update(
            metadata_status="unavailable", error_code="metadata_budget_exceeded"
        )
    window = window[:inspected]
    next_cursor = window[-1]["id"] if window and len(rows) > len(window) else None
    query = query.strip().casefold()
    return {
        "items": [
            row
            for row in window
            if not query
            or any(
                query in str(row.get(key, "")).casefold()
                for key in ("id", "name", "title")
            )
        ],
        "next_cursor": next_cursor,
        "scanned_count": len(window),
        "warnings": sorted(warnings),
        "content_persisted": False,
    }
