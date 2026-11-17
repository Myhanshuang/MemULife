"""Bounded, expiring preview rendezvous; no database, files, or content logging."""

import hashlib
import json
import re
import threading
import time
from uuid import uuid4

from ..adapters.base import AdapterError
from .messages import clean_message, wire

TTL = 600
MAX_BYTES = 8 * 1024 * 1024
MAX_RESPONSE = 900 * 1024


def binding_hash(config):
    return hashlib.sha256(
        json.dumps(
            {k: v for k, v in config.items() if k != "_preview_key"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


class MessagePreviews:
    def __init__(self):
        self.values = {}
        self.lock = threading.RLock()

    def prune(self):
        for key, value in list(self.values.items()):
            if value["expires"] <= time.monotonic():
                value["event"].set()
                del self.values[key]

    def read(self, config, conversation, locator, cursor="", limit=30):
        if not 1 <= limit <= 30:
            raise AdapterError("invalid_limit")
        owner = (
            config["tenant_id"],
            config["source_instance_id"],
            config["_preview_key"],
            conversation,
            binding_hash(config),
        )
        with self.lock:
            self.prune()
            if not cursor:
                existing = next(
                    (
                        v
                        for v in self.values.values()
                        if v["owner"] == owner and v["status"] == "pending"
                    ),
                    None,
                )
                if existing:
                    identity = existing["id"]
                else:
                    if len(self.values) >= 32:
                        raise AdapterError("import_preview_capacity")
                    identity = str(uuid4())
                    self.values[identity] = {
                        "id": identity,
                        "owner": owner,
                        "conversation": {
                            "id": conversation,
                            "locator": locator,
                            "revision": None,
                        },
                        "expires": time.monotonic() + TTL,
                        "status": "pending",
                        "event": threading.Event(),
                        "claimed": None,
                        "messages": [],
                        "bytes": 0,
                    }
                offset = 0
            else:
                parts = cursor.split(":")
                identity = parts[0]
                if len(parts) > 2 or (
                    len(parts) == 2 and not re.fullmatch(r"\d{1,3}", parts[1])
                ):
                    raise AdapterError("import_preview_cursor_invalid")
                offset = int(parts[1]) if len(parts) == 2 else 0
            value = self.values.get(identity)
            if not value or value["owner"] != owner:
                raise AdapterError("import_preview_cursor_invalid")
            event = value["event"] if cursor and value["status"] == "pending" else None
        if event:
            event.wait(
                20
            )  # One user-requested wait; no polling or automatic resubmission.
        with self.lock:
            self.prune()
            value = self.values.get(identity)
            if not value or value["owner"] != owner:
                raise AdapterError("import_preview_cursor_invalid")
            result = {
                "request_id": identity,
                "status": value["status"],
                "content_persisted": False,
                "items": [],
                "next_cursor": None,
            }
            if value["status"] == "pending":
                return result | {"next_cursor": identity}
            if value["status"] == "failed":
                return result | {"error_code": value["error_code"]}
            if offset > len(value["messages"]):
                raise AdapterError("import_preview_cursor_invalid")
            rows = value["messages"][offset : offset + limit]
            return result | {
                "items": rows,
                "total": len(value["messages"]),
                "next_cursor": f"{identity}:{offset + len(rows)}"
                if offset + len(rows) < len(value["messages"])
                else None,
            }

    def pending(self, tenant, source):
        with self.lock:
            self.prune()
            return [
                {k: v[k] for k in ("id", "owner", "conversation", "claimed")}
                for v in self.values.values()
                if v["owner"][:2] == (tenant, source) and v["status"] == "pending"
            ]

    def claim(self, identity):
        with self.lock:
            self.prune()
            value = self.values.get(identity)
            if (
                not value
                or value["status"] != "pending"
                or (
                    value["claimed"] is not None
                    and time.monotonic() - value["claimed"] < 150
                )
            ):
                return None
            value["claimed"] = time.monotonic()
            return {"request_id": identity, "conversation": value["conversation"]}

    def owner(self, identity):
        with self.lock:
            self.prune()
            value = self.values.get(identity)
            if not value:
                raise AdapterError("import_preview_expired")
            return value["owner"]

    def discard(self, identity):
        with self.lock:
            value = self.values.pop(identity, None)
            if value:
                value["event"].set()

    def complete(self, identity, messages, error=None):
        if error is not None:
            if (
                not isinstance(error, str)
                or not re.fullmatch(r"[a-z_]{1,80}", error)
                or messages
            ):
                raise AdapterError("import_preview_invalid")
            clean = []
        else:
            if not isinstance(messages, list) or len(messages) > 500:
                raise AdapterError("import_preview_invalid")
            clean = [clean_message(m, digest=True) for m in messages]
            if len({m["id"] for m in clean}) != len(clean):
                raise AdapterError("import_preview_invalid")
        size = len(wire(clean))
        if size > MAX_RESPONSE:
            raise AdapterError("import_preview_capacity")
        with self.lock:
            self.prune()
            value = self.values.get(identity)
            if not value or value["status"] != "pending" or value["claimed"] is None:
                raise AdapterError("import_preview_expired")
            if sum(v["bytes"] for v in self.values.values()) + size > MAX_BYTES:
                raise AdapterError("import_preview_capacity")
            value.update(
                messages=clean,
                bytes=size,
                status="failed" if error else "ready",
                error_code=error,
            )
            value["event"].set()
        return {"accepted": len(clean), "content_persisted": False}


previews = MessagePreviews()
