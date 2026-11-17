"""Bounded, read-only access to explicitly authorized local source roots."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from ..base import AdapterError, Attachment


def encode(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def native_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if type(value) is int:
        return str(value)
    return None


def timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat() if parsed.tzinfo else None


def text_parts(value: Any) -> str:
    """Extract only explicit text/content fields; opaque reasoning stays opaque."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text for part in value if (text := text_parts(part)))
    if not isinstance(value, dict):
        return ""
    if value.get("type") in {"redacted_thinking", "encrypted_reasoning", "reasoning"}:
        return ""
    for key in ("text", "content", "output", "result", "message"):
        if key in value:
            return text_parts(value[key])
    return ""


class LocalAccess:
    def __init__(self, config: dict):
        raw = config.get("root")
        if not isinstance(raw, str) or not raw:
            raise AdapterError("root_required")
        self.root = Path(raw).expanduser().resolve()
        if not self.root.is_dir():
            raise AdapterError("source_offline", retryable=True)
        self.max_bytes = int(config.get("max_record_bytes", 8 * 1024 * 1024))
        self.max_files = int(config.get("max_files", 10000))
        if (
            not 1024 <= self.max_bytes <= 32 * 1024 * 1024
            or not 1 <= self.max_files <= 100000
        ):
            raise AdapterError("invalid_source_budget")
        self.attachment_roots = [
            Path(p).resolve() for p in config.get("attachment_roots", [])
        ]
        if any(not p.is_dir() for p in self.attachment_roots):
            raise AdapterError("attachment_root_unavailable")

    def checked(self, path: Path, *, attachment: bool = False) -> Path:
        roots = self.attachment_roots if attachment else [self.root]
        absolute = path.absolute()
        # Reject symlinks anywhere below the selected root, even inside the scope.
        root = next((root for root in roots if absolute.is_relative_to(root)), None)
        if root is None:
            raise AdapterError("path_outside_scope")
        relative = absolute.relative_to(root)
        current = root
        for component in relative.parts:
            current /= component
            if component in {"..", "."} or current.is_symlink():
                raise AdapterError("path_outside_scope")
        if not absolute.resolve().is_relative_to(root):
            raise AdapterError("path_outside_scope")
        return absolute

    def open(self, path: Path):
        checked = self.checked(path)
        try:
            fd = os.open(checked, os.O_RDONLY | os.O_NOFOLLOW)
            opened = Path(f"/proc/self/fd/{fd}")
            if opened.exists() and not opened.resolve().is_relative_to(self.root):
                os.close(fd)
                raise AdapterError("path_outside_scope")
            return os.fdopen(fd, "rb")
        except OSError:
            raise AdapterError("source_offline", retryable=True) from None

    def read_file(self, path: Path) -> bytes:
        with self.open(path) as source:
            before = os.fstat(source.fileno())
            raw = source.read(self.max_bytes + 1)
            after = os.fstat(source.fileno())
        if len(raw) > self.max_bytes:
            raise AdapterError("record_too_large")
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise AdapterError("source_changed_during_read", retryable=True)
        return raw

    def files(self, patterns: tuple[str, ...]) -> list[Path]:
        found = set()
        for pattern in patterns:
            for path in self.root.glob(pattern):
                if path.is_file():
                    self.checked(path)
                    found.add(path)
                    if len(found) > self.max_files:
                        raise AdapterError("inventory_budget_exceeded")
        return sorted(found)

    @contextmanager
    def sqlite(self, path: Path):
        checked = self.checked(path)
        try:
            connection = sqlite3.connect(
                checked.as_uri() + "?mode=ro", uri=True, timeout=2
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
        except sqlite3.Error:
            raise AdapterError("source_offline", retryable=True) from None
        try:
            yield connection
        finally:
            connection.close()

    def fetch_attachment(self, ref: dict) -> Attachment:
        value = ref.get("path")
        if not isinstance(value, str):
            raise AdapterError("attachment_path_required")
        path = self.checked(Path(value), attachment=True)
        # The explicit attachment root is the only additional authorization.
        scoped = LocalAccess(
            {
                "root": str(
                    next(p for p in self.attachment_roots if path.is_relative_to(p))
                ),
                "max_record_bytes": self.max_bytes,
            }
        )
        return Attachment(
            str(ref.get("attachment_id", path.name)),
            scoped.read_file(path),
            str(ref.get("media_type", "application/octet-stream")),
            path.as_uri(),
            "available",
        )


LOCAL_CONFIG_SCHEMA = {
    "type": "object",
    "required": ["root"],
    "properties": {
        "root": {"type": "string", "minLength": 1},
        "attachment_roots": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "max_record_bytes": {"type": "integer", "minimum": 1024, "maximum": 33554432},
        "max_files": {"type": "integer", "minimum": 1, "maximum": 100000},
    },
    "additionalProperties": False,
}
