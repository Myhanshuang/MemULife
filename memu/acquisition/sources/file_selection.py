"""Filesystem metadata and frozen selections, independent of durable import jobs."""

import os
import mimetypes
from pathlib import Path, PurePosixPath

from ..adapters.base import AdapterError

MAX_FILES = 200
MAX_ENTRIES = 10000


def resolve(root, relative):
    parts = PurePosixPath(relative)
    if not relative or parts.is_absolute() or ".." in parts.parts or "\\" in relative:
        raise AdapterError("import_scope_denied")
    path = root
    for part in parts.parts:
        if part in {".git", ".memulife"}:
            raise AdapterError("import_scope_denied")
        path = path / part
        if path.is_symlink():
            raise AdapterError("import_scope_denied")
    if not path.resolve().is_relative_to(root):
        raise AdapterError("import_scope_denied")
    return path


def version(path):
    stat = path.stat(follow_symlinks=False)
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"


def candidates(config, parent=".", after="", limit=100, query="", kind=""):
    root = Path(config["root"]).resolve(strict=True)
    directory = resolve(root, parent)
    if not directory.is_dir():
        raise AdapterError("import_directory_required")
    rows = []
    query = query.strip().casefold()
    # Bounded metadata enumeration; never open a document or attachment.
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_ENTRIES:
                raise AdapterError("import_catalog_too_large")
            if entry.is_symlink() or entry.name in {".git", ".memulife"}:
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            if kind and kind != ("directory" if is_dir else "file"):
                continue
            if query and query not in entry.name.casefold():
                continue
            if not is_dir and not entry.is_file(follow_symlinks=False):
                continue
            path = Path(entry.path)
            identity = path.relative_to(root).as_posix()
            if identity <= after:
                continue
            rows.append(
                {
                    "id": identity,
                    "name": entry.name,
                    "kind": "directory" if is_dir else "file",
                    "version": None if is_dir else version(path),
                    "size_bytes": None if is_dir else entry.stat().st_size,
                    "format": None if is_dir else (path.suffix.lstrip('.').upper() or '文件'),
                    "media_type": None if is_dir else (mimetypes.guess_type(path.name)[0] or 'application/octet-stream'),
                }
            )
    rows.sort(key=lambda row: row["id"])
    return {
        "items": rows[:limit],
        "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        "content_persisted": False,
    }


def freeze(config, selection):
    root = Path(config["root"]).resolve(strict=True)
    chosen = {}
    pending = list(selection.scopes)
    visits = 0
    while pending:
        relative = pending.pop()
        visits += 1
        if visits > MAX_ENTRIES:
            raise AdapterError("import_catalog_too_large")
        path = resolve(root, relative)
        if path.is_dir():
            # A directory means the current finite file list, never future arrivals.
            page = candidates(config, relative, limit=MAX_ENTRIES)
            pending.extend(item["id"] for item in page["items"])
        elif path.is_file():
            chosen[path.relative_to(root).as_posix()] = version(path)
            if len(chosen) > MAX_FILES:
                raise AdapterError("import_selection_too_large")
        else:
            raise AdapterError("import_candidate_unavailable")
    if not chosen:
        raise AdapterError("import_selection_empty")
    return selection.model_copy(
        update={"scopes": sorted(chosen), "revisions": dict(sorted(chosen.items()))}
    )


def validate_frozen(config, selection):
    if set(selection.revisions) != set(selection.scopes):
        raise AdapterError("import_plan_required")
    current = freeze(config, selection)
    if current.revisions != selection.revisions:
        raise AdapterError("import_candidate_changed")
