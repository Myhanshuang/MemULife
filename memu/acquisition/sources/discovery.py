"""Bounded in-memory browser catalog, independent of import and persistence."""

from datetime import datetime
import json
import re
import threading
import time

from ..adapters.base import AdapterError

TTL = 600
MAX_CATALOG_BYTES = 8 * 1024 * 1024
CATALOG_PROVIDERS = {"chatgpt", "claude", "gemini", "perplexity", "deepseek", "grok"}
CATALOG_FIELDS = {
    "id",
    "locator",
    "revision",
    "title",
    "provider",
    "created_at",
    "updated_at",
    "message_count",
}


def catalog_timestamp(value):
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > 40
        or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
            r"(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
            value,
        )
    ):
        raise AdapterError("import_catalog_invalid")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise AdapterError("import_catalog_invalid") from None
    return value


class Discovery:
    def __init__(self):
        self.values = {}
        self.lock = threading.Lock()

    def prune(self):
        now = time.monotonic()
        self.values = {k: v for k, v in self.values.items() if v[0] > now}

    def offer(self, tenant, source, items, *, provider=None):
        if not isinstance(items, list) or len(items) > 200:
            raise AdapterError("import_catalog_invalid")
        if provider is not None and (
            not isinstance(provider, str) or provider not in CATALOG_PROVIDERS
        ):
            raise AdapterError("import_catalog_invalid")
        clean = []
        for item in items:
            if not isinstance(item, dict) or set(item) - CATALOG_FIELDS:
                raise AdapterError("import_catalog_invalid")
            if any(
                any("\ud800" <= char <= "\udfff" for char in value)
                for value in item.values()
                if isinstance(value, str)
            ):
                raise AdapterError("import_catalog_invalid")
            if any(
                not isinstance(item.get(k), str) or not 1 <= len(item[k]) <= 2048
                for k in ("id", "locator")
            ):
                raise AdapterError("import_catalog_invalid")
            if item.get("revision") is not None and (
                not isinstance(item["revision"], str) or len(item["revision"]) > 256
            ):
                raise AdapterError("import_catalog_invalid")
            title = item.get("title")
            if title is not None and (
                not isinstance(title, str)
                or len(title) > 512
                or any(ord(char) < 32 or ord(char) == 127 for char in title)
            ):
                raise AdapterError("import_catalog_invalid")
            claimed_provider = item.get("provider")
            if claimed_provider is not None and (
                not isinstance(claimed_provider, str)
                or claimed_provider not in CATALOG_PROVIDERS
                or (provider is not None and claimed_provider != provider)
            ):
                raise AdapterError("import_catalog_invalid")
            count = item.get("message_count")
            if count is not None and (
                type(count) is not int or not 0 <= count <= 2**53 - 1
            ):
                raise AdapterError("import_catalog_invalid")
            clean.append(
                {
                    "id": item["id"],
                    "name": title if title and title.strip() else item["id"],
                    "title": title,
                    "locator": item["locator"],
                    "kind": "conversation",
                    "revision": item.get("revision"),
                    "provider": provider or claimed_provider,
                    "created_at": catalog_timestamp(item.get("created_at")),
                    "updated_at": catalog_timestamp(item.get("updated_at")),
                    "message_count": count,
                }
            )
        with self.lock:
            self.prune()
            key = (tenant, source)
            if key not in self.values and len(self.values) >= 64:
                raise AdapterError("import_catalog_capacity")
            merged = {item["id"]: item for item in self.values.get(key, (0, []))[1]}
            merged.update({item["id"]: item for item in clean})
            if (
                len(merged) > 2000
                or sum(len(v[1]) for k, v in self.values.items() if k != key)
                + len(merged)
                > 4000
            ):
                raise AdapterError("import_catalog_capacity")
            rows = list(merged.values())
            size = len(
                json.dumps(
                    rows,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            )
            if (
                size + sum(v[2] for k, v in self.values.items() if k != key)
                > MAX_CATALOG_BYTES
            ):
                raise AdapterError("import_catalog_capacity")
            self.values[key] = (time.monotonic() + TTL, rows, size)
        return {"accepted": len(clean), "expires_in": TTL, "content_persisted": False}

    def get(self, tenant, source):
        with self.lock:
            self.prune()
            return list(self.values.get((tenant, source), (0, []))[1])


discovery = Discovery()
