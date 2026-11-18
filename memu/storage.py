"""File-authoritative memory repository with stable, line-addressed records.

All public mutations are serialized by an in-process lock and a repository
``flock``. A durable redo journal makes a multi-file commit recoverable without
running a model again. Readers take only this short filesystem lock; model work
must happen outside it. Mutable ``state`` changes belong inside ``lock()`` and
must be persisted with ``save_state()``.
"""

from __future__ import annotations

import base64
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import threading
from typing import Any, Iterator
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from .errors import RevisionConflict


class InvalidReference(ValueError):
    """A locator is missing, deleted, outside a file, or not a block boundary."""


_REF = re.compile(r"^(?P<alias>[cusra][1-9][0-9]*)(?::(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?|@(?P<begin>\d+(?:\.\d+)?)-(?P<finish>\d+(?:\.\d+)?))?$")
_RECORD = re.compile(r"^(\S+) @([^ ]+) \^([^ ]*) \| ", re.DOTALL)
_HEADER = "!memu="
_KINDS = {"source": "c", "unit": "u", "detail": "s", "media": "a", "view": "r"}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _as_dict(value: Any) -> dict:
    return value.model_dump(by_alias=True) if hasattr(value, "model_dump") else dict(value)


def _time_token(value: dict) -> str:
    precision, item = value["precision"], value["value"]
    if precision == "unknown":
        return "unknown"
    if precision == "range":
        return "range:" + item["from"] + "/" + item["to"]
    return precision + ":" + item


def _parse_time(token: str) -> dict:
    if token == "unknown":
        return {"precision": "unknown", "value": None}
    precision, value = token.split(":", 1)
    if precision == "range":
        start, end = value.split("/", 1)
        value = {"from": start, "to": end}
    return {"precision": precision, "value": value}


def _date_parts(value: str, tz: str) -> list[str]:
    if "T" in value or " " in value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("instant timestamps must include a timezone")
        value = parsed.astimezone(ZoneInfo(tz)).date().isoformat()
    return value.split("-")


def archive_directory(value: dict, tz: str) -> str:
    """Archive by explicitly known event time, never by arrival time."""
    value = _as_dict(value)
    precision, item = value["precision"], value["value"]
    if precision == "unknown":
        return "_undated"
    if precision == "range":
        left, right = _date_parts(item["from"], tz), _date_parts(item["to"], tz)
        common = []
        for first, second in zip(left, right):
            if first != second:
                break
            common.append(first)
        return "/".join(common) if common else "_undated"
    parts = _date_parts(item, tz)
    if precision in {"month", "year"}:
        parts.append("_undated")
    return "/".join(parts)


def _escape_body(body: str) -> str:
    return body.replace("\\", "\\\\").replace("|", "\\|").replace(";", "\\;")


def _unescape_body(body: str) -> str:
    return re.sub(r"\\([\\|;])", r"\1", body)


def _body_separator(value: str) -> int:
    for match in re.finditer(r" \| ", value):
        index = match.start() + 1
        preceding = index - 1
        slashes = 0
        while preceding >= 0 and value[preceding] == "\\":
            slashes += 1
            preceding -= 1
        if slashes % 2 == 0:
            return match.start()
    raise ValueError("record missing index separator")


def _render_entry(entry: dict, source: str) -> str:
    entry = _as_dict(entry)
    time = _as_dict(entry["time"])
    topics = ",".join(quote(topic, safe="") for topic in entry.get("topics", [])) or "-"
    refs = [">" + fact for fact in entry.get("facts", [])]
    refs += [quote(rel["rel"], safe="") + ">" + rel["target"] for rel in entry.get("relations", [])]
    # Keep human-readable relation words; quote only separators and whitespace.
    refs = [unquote(ref) if ref.split(">", 1)[0] and not re.search(r"\s", unquote(ref.split(">", 1)[0])) else ref for ref in refs]
    return f"{_time_token(time)} @{quote(source, safe='')} ^{topics} | {_escape_body(entry['body'])} | {' '.join(refs)} ;;\n"


def _parse_entry(text: str) -> dict:
    from .revision_storage import ENTRY_PREFIX
    if text.startswith(ENTRY_PREFIX):
        return json.loads(text[len(ENTRY_PREFIX):].rstrip()[:-2].rstrip())
    match = _RECORD.match(text)
    if not match or not text.rstrip().endswith(";;"):
        raise ValueError("malformed memory record")
    rest = text[match.end():].rstrip()[:-2].rstrip()
    divider = _body_separator(rest)
    body, index_text = rest[:divider], rest[divider + 3:]
    facts, relations = [], []
    for token in index_text.split():
        word, ref = token.split(">", 1)
        if word:
            relations.append({"rel": unquote(word), "target": ref})
        else:
            facts.append(ref)
    return {"time": _parse_time(match[1]), "source": unquote(match[2]),
            "topics": [] if match[3] == "-" else [unquote(value) for value in match[3].split(",")],
            "body": _unescape_body(body), "facts": facts, "relations": relations}


class Repository:
    def __init__(self, root: str | Path, timezone: str = "Asia/Singapore"):
        self.root = Path(root).expanduser().resolve()
        self.timezone = timezone
        ZoneInfo(timezone)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".meta").mkdir(exist_ok=True)
        with _LOCKS_GUARD:
            self._mutex = _LOCKS.setdefault(str(self.root), threading.RLock())
        self._local = threading.local()
        self.catalog: dict[str, dict] = {}
        self.state: dict = {}
        with self.lock():
            if not (self.root / ".meta/catalog.tsv").exists():
                self._transaction({})

    def _path(self, relative: str) -> Path:
        posix = PurePosixPath(relative)
        if posix.is_absolute() or ".." in posix.parts or not posix.parts:
            raise ValueError("repository paths must be safe relative paths")
        result = self.root.joinpath(*posix.parts)
        if not result.resolve().is_relative_to(self.root):
            raise ValueError("repository path escapes through a symlink")
        return result

    def _bytes(self, relative: str) -> bytes:
        staged = getattr(self._local, "staged", {})
        return staged[relative] if relative in staged else self._path(relative).read_bytes()

    def _exists(self, relative: str) -> bool:
        return relative in getattr(self._local, "staged", {}) or self._path(relative).exists()

    @contextmanager
    def transaction(self) -> Iterator["Repository"]:
        """Atomically publish records, aliases, and their maintenance checkpoint.

        Mutations and reads inside the context see staged versions. No files are
        published until the outermost context exits successfully. Exceptions
        discard this context's changes; the durable journal handles interruption
        once publication begins.
        """
        with self.lock():
            depth = getattr(self._local, "transaction_depth", 0)
            before_catalog, before_state = deepcopy(self.catalog), deepcopy(self.state)
            before_staged = dict(getattr(self._local, "staged", {}))
            self._local.staged = dict(before_staged)
            self._local.transaction_depth = depth + 1
            try:
                yield self
            except BaseException:
                self.catalog, self.state = before_catalog, before_state
                self._local.staged = before_staged
                self._local.transaction_depth = depth
                raise
            else:
                self._local.transaction_depth = depth
                if depth == 0:
                    payload = self._local.staged
                    self._local.staged = {}
                    self._transaction(payload)

    @contextmanager
    def lock(self) -> Iterator["Repository"]:
        with self._mutex:
            depth = getattr(self._local, "depth", 0)
            if depth == 0:
                descriptor = os.open(self.root / ".meta/repository.lock", os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._local.descriptor = descriptor
                try:
                    self._recover()
                    self._load()
                except BaseException:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
                    raise
            self._local.depth = depth + 1
            try:
                yield self
            finally:
                self._local.depth = depth
                if depth == 0:
                    descriptor = self._local.descriptor
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)

    def _load(self) -> None:
        catalog = self.root / ".meta/catalog.tsv"
        records, paths = {}, set()
        if catalog.exists():
            for line in catalog.read_text(encoding="utf-8").splitlines():
                if not line or line.startswith("#"):
                    continue
                alias, kind, path, revision, metadata = line.split("\t", 4)
                if alias in records or path in paths:
                    raise ValueError("catalog aliases and paths must be unique")
                paths.add(path)
                self._path(path)
                records[alias] = {"alias": alias, "kind": kind, "path": path,
                                  "revision": revision, "metadata": json.loads(metadata)}
        self.catalog = records
        state_path = self.root / ".meta/maintenance.json"
        self.state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
            "idempotency": {}, "pending": {}, "tasks": {}, "deleted": [], "generation": 0,
        }

    def _catalog_bytes(self) -> bytes:
        rows = ["# alias\tkind\tpath\trevision\tmetadata"]
        for record in self.catalog.values():
            rows.append("\t".join([record["alias"], record["kind"], record["path"], record["revision"], _json(record["metadata"])]))
        return ("\n".join(rows) + "\n").encode()

    def _replace_file(self, relative: str, data: bytes) -> None:
        target = self._path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".memu-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _recover(self) -> None:
        journal_path = self.root / ".meta/transaction.json"
        if not journal_path.exists():
            return
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        for item in journal["writes"]:
            data = base64.b64decode(item["data"], validate=True)
            if _sha(data) != item["sha256"]:
                raise RuntimeError("repository transaction journal checksum mismatch")
            target = self._path(item["path"])
            if not target.exists() or _sha(target.read_bytes()) != item["sha256"]:
                self._replace_file(item["path"], data)
        journal_path.unlink()
        directory = os.open(journal_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _transaction(self, writes: dict[str, str | bytes], expected_revisions: dict[str, str] | None = None) -> None:
        for alias, expected in (expected_revisions or {}).items():
            if self.revision(alias) != expected:
                raise RevisionConflict(f"input revision changed: {alias}")
        payload = {path: data.encode() if isinstance(data, str) else data for path, data in writes.items()}
        for path, data in payload.items():
            self._path(path)
            for record in self.catalog.values():
                if record["path"] == path:
                    record["revision"] = _sha(data)
        if getattr(self._local, "transaction_depth", 0):
            self._local.staged.update(payload)
            return
        payload[".meta/catalog.tsv"] = self._catalog_bytes()
        payload[".meta/maintenance.json"] = (_json(self.state) + "\n").encode()
        journal = {"version": 1, "writes": [
            {"path": path, "sha256": _sha(data), "data": base64.b64encode(data).decode()}
            for path, data in payload.items()
        ]}
        self._replace_file(".meta/transaction.json", (_json(journal) + "\n").encode())
        self._recover()

    def commit(self, writes: dict[str, str | bytes], expected_revisions: dict[str, str] | None = None) -> None:
        """Commit prepared files and current maintenance state, or raise conflict.

        Normal clients should use append_entries/write_view rather than editing
        records through this low-level recovery primitive.
        """
        with self.lock():
            self._transaction(writes, expected_revisions)

    def save_state(self) -> None:
        # Calling from an existing lock preserves pending in-memory edits.
        if getattr(self._local, "depth", 0):
            self._transaction({})
        else:
            desired = deepcopy(self.state)
            with self.lock():
                self.state = desired
                self._transaction({})

    def _allocate(self, kind: str, path: str, metadata: dict | None = None) -> str:
        self._path(path)
        for record in self.catalog.values():
            if record["path"] == path:
                if record["kind"] != kind:
                    raise ValueError("existing file has a different kind")
                return record["alias"]
        prefix = _KINDS[kind]
        number = max((int(alias[1:]) for alias in self.catalog if alias.startswith(prefix)), default=0) + 1
        alias = f"{prefix}{number}"
        self.catalog[alias] = {"alias": alias, "kind": kind, "path": path,
                               "revision": "", "metadata": metadata or {}}
        return alias

    def _new_number(self, kind):
        prefix = _KINDS[kind]
        return max((int(alias[1:]) for alias in self.catalog if alias.startswith(prefix)), default=0) + 1

    def revision(self, alias: str) -> str:
        with self.lock():
            alias = alias.split(":", 1)[0].split("@", 1)[0]
            record = self.catalog.get(alias)
            if not record:
                raise InvalidReference(f"unknown alias: {alias}")
            return _sha(self._bytes(record["path"])) if self._exists(record["path"]) else ""

    def save_source(self, text: str, source: str, time: dict, metadata: dict | None = None) -> str:
        if not text or not text.strip():
            raise ValueError("source text cannot be empty")
        if not source or "\n" in source:
            raise ValueError("source identity is required")
        time = _as_dict(time)
        with self.lock():
            number = self._new_number('source')
            path = f"{archive_directory(time, self.timezone)}/source/c{number}.md"
            info = dict(metadata or {}) | {"source": source, "time": time}
            alias = self._allocate("source", path, info)
            self._transaction({path: text})
            count = len(text.splitlines())
            return f"{alias}:1-{count}"

    def append_source(self, ref: str, text: str) -> str:
        if not text or not text.strip():
            raise ValueError("source text cannot be empty")
        with self.lock():
            alias = ref.split(":", 1)[0]
            record = self.catalog.get(alias)
            if not record or record["kind"] != "source":
                raise InvalidReference("append_source requires a source alias")
            original = self._bytes(record["path"]).decode("utf-8")
            start = len(original.splitlines()) + 1
            combined = original + ("" if not original or original.endswith("\n") else "\n") + text
            self._transaction({record["path"]: combined})
            return f"{alias}:{start}-{len(combined.splitlines())}"

    def save_media(self, data: bytes, source: str, time: dict, mime_type: str,
                   filename: str | None = None, duration: float | None = None,
                   metadata: dict | None = None) -> str:
        if not isinstance(data, bytes) or not data:
            raise ValueError("media requires nonempty original bytes")
        modality = mime_type.split("/", 1)[0]
        if modality not in {"image", "audio", "video"}:
            raise ValueError("unsupported media MIME type")
        if modality in {"audio", "video"} and (duration is None or not math.isfinite(duration) or duration <= 0):
            raise ValueError("audio/video requires a positive original duration")
        time = _as_dict(time)
        with self.lock():
            number = self._new_number('media')
            extension = mimetypes.guess_extension(mime_type) or ".bin"
            path = f"{archive_directory(time, self.timezone)}/source/a{number}{extension}"
            info = dict(metadata or {}) | {"source": source, "time": time, "mime_type": mime_type,
                    "modality": modality, "duration": duration, "original_filename": Path(filename).name if filename else None}
            alias = self._allocate("media", path, info)
            self._transaction({path: data})
            return alias

    def append_entries(self, kind: str, entries: list[dict], source: str, *, single_line=False) -> list[str]:
        if kind not in {"unit", "detail"}:
            raise ValueError("only unit/detail files support append_entries")
        if not entries:
            return []
        with self.lock():
            writes: dict[str, str] = {}
            line_counts = {}
            refs = []
            for raw in entries:
                entry = _as_dict(raw)
                if not entry.get("facts"):
                    raise InvalidReference("every saved entry requires direct factual references")
                for ref in entry.get("facts", []):
                    self.validate_ref(ref)
                for relation in entry.get("relations", []):
                    target = self.read(relation["target"])
                    if target["kind"] != "unit":
                        raise InvalidReference("relations must target Units")
                directory = archive_directory(_as_dict(entry["time"]), self.timezone)
                path = f"{directory}/{'units' if kind == 'unit' else 'detail'}.md"
                alias = self._allocate(kind, path, {"timezone": self.timezone})
                if path not in writes:
                    writes[path] = self._bytes(path).decode("utf-8") if self._exists(path) else _HEADER + _json({"kind": kind, "timezone": self.timezone}) + "\n"
                    line_counts[path] = len(writes[path].splitlines())
                old = writes[path]
                if old and not old.endswith("\n"):
                    old += "\n"
                start = line_counts[path] + 1
                if single_line:
                    from .revision_storage import entry_line
                    rendered = entry_line(dict(entry, source=source))
                else:
                    rendered = _render_entry(entry, source)
                end = start + len(rendered.splitlines()) - 1
                line_counts[path] = end
                refs.append(f"{alias}:{start}" if kind == "unit" else f"{alias}:{start}-{end}")
                writes[path] = old + rendered
            self._transaction(writes)
            return refs

    def _parse_ref(self, ref: str) -> tuple[dict, re.Match]:
        if ref in {"MEMORY", "MEMORY.md"}:
            ref = self.view_ref('memory', 'global') or ''
        match = _REF.fullmatch(ref)
        if not match or match["alias"] not in self.catalog:
            raise InvalidReference(f"unknown or malformed locator: {ref}")
        record = self.catalog[match["alias"]]
        return record, match

    def _entries(self, text: str, alias: str, kind: str) -> list[dict]:
        from .revision_storage import ENTRY_PREFIX
        lines = text.splitlines()
        records = []
        index = 0
        while index < len(lines):
            if not (_RECORD.match(lines[index]) or lines[index].startswith(ENTRY_PREFIX)):
                index += 1
                continue
            start = index + 1
            buffer = [lines[index]]
            while not lines[index].rstrip().endswith(";;"):
                index += 1
                if index >= len(lines):
                    raise ValueError(f"unterminated memory record in {alias}:{start}")
                buffer.append(lines[index])
            end = index + 1
            value = _parse_entry("\n".join(buffer))
            value.update({"ref": f"{alias}:{start}" if kind == "unit" else f"{alias}:{start}-{end}",
                          "start_line": start, "end_line": end, "text": "\n".join(buffer)})
            records.append(value)
            index += 1
        return records

    def _text_document(self, record):
        """Bounded parsed snapshots, invalidated by staged bytes or physical file identity."""
        if not hasattr(self, '_text_cache'):
            self._text_cache, self._text_cache_size = OrderedDict(), 0
        alias, relative = record['alias'], record['path']
        staged = getattr(self._local, 'staged', {})
        if relative in staged:
            signature = ('staged', staged[relative])
        else:
            stat = self._path(relative).stat()
            signature = (relative, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        cached = self._text_cache.get(alias)
        if cached and cached['signature'] == signature:
            self._text_cache.move_to_end(alias)
            return cached
        if cached:
            self._text_cache_size -= self._text_cache.pop(alias)['size']
        data = self._bytes(relative)
        text = data.decode('utf-8')
        entries = self._entries(text, alias, record['kind']) if record['kind'] != 'source' else []
        document = {'signature': signature, 'size': len(data), 'text': text, 'lines': text.splitlines(),
                    'revision': _sha(data), 'entries': entries,
                    'by_start': {entry['start_line']: entry for entry in entries}}
        # Bound both the number of files and their original encoded byte size.
        if len(data) <= 8 * 1024 * 1024:
            while self._text_cache and (len(self._text_cache) >= 32 or
                                       self._text_cache_size + len(data) > 8 * 1024 * 1024):
                self._text_cache_size -= self._text_cache.popitem(last=False)[1]['size']
            self._text_cache[alias] = document
            self._text_cache_size += len(data)
        return document

    def read(self, ref: str, *, _document=None) -> dict:
        with self.lock():
            record, match = self._parse_ref(ref)
            alias, kind = record["alias"], record["kind"]
            path = self._path(record["path"])
            if not self._exists(record["path"]):
                raise InvalidReference("referenced file is missing")
            media_size = (len(self._local.staged[record['path']]) if record['path'] in getattr(self._local, 'staged', {})
                          else path.stat().st_size) if kind == 'media' else None
            document = None if kind == 'media' else (_document or self._text_document(record))
            base = {"ref": ref, "alias": alias, "kind": kind, "path": record["path"],
                    "revision": record['revision'] if kind == 'media' else document['revision'], "facts": [], "relations": [], "topics": [],
                    "source": record["metadata"].get("source", ""),
                    "time": record["metadata"].get("time", {"precision": "unknown", "value": None})}
            if kind == "media":
                if match["start"] or not media_size:
                    raise InvalidReference("invalid or deleted media locator")
                meta = record["metadata"]
                if match["begin"]:
                    begin, finish = float(match["begin"]), float(match["finish"])
                    if meta.get("modality") == "image" or not 0 <= begin < finish <= meta.get("duration", 0):
                        raise InvalidReference("media range exceeds the original resource")
                    for deleted in self.state.get("media_blanked_ranges", []):
                        if deleted["alias"] == alias and begin < deleted["end"] and finish > deleted["start"]:
                            raise InvalidReference("media locator intersects deleted material")
                    base.update({"start_seconds": begin, "end_seconds": finish})
                base.update(meta)
                base["redacted_ranges"] = [item for item in self.state.get("media_blanked_ranges", []) if item["alias"] == alias]
                base.update({"body": "", "text": "", "absolute_path": str(path), "size_bytes": media_size})
                return base
            if match["begin"]:
                raise InvalidReference("time ranges only address media")
            text, lines = document['text'], document['lines']
            if kind == "view":
                if match["start"]:
                    raise InvalidReference("views may only be referenced by file entry")
                header = json.loads(lines[0][len(_HEADER):]) if lines and lines[0].startswith(_HEADER) else record["metadata"]
                entries = deepcopy(document['entries'])
                from .revision_storage import EDITABLE_VIEWS, view_entries
                if header.get('view') in EDITABLE_VIEWS:
                    entries = view_entries(self, alias, entries, header)
                base.update(header)
                base.update({"ref": alias, "entries": entries, "body": "\n".join(entry["body"] for entry in entries), "text": text,
                             "facts": list(dict.fromkeys(fact for entry in entries for fact in entry["facts"]
                                                        if fact not in entry.get('invalid_refs', [])))})
                if header.get('view') in EDITABLE_VIEWS:
                    base['invalid_refs'] = list(dict.fromkeys(f for e in entries for f in e['invalid_refs']))
                    base['edit_revision'] = _sha(_json([(e['entry_id'], e['edit_revision']) for e in entries]).encode())
                    base['protected_count'] = sum(e['protected'] for e in entries)
                    base['protected'] = bool(entries) and all(e['protected'] for e in entries)
                return base
            if not match["start"]:
                raise InvalidReference("text locators require line positions")
            start, end = int(match["start"]), int(match["end"] or match["start"])
            if not 1 <= start <= end <= len(lines):
                raise InvalidReference("text range is outside the file")
            if kind == "unit":
                if match["end"]:
                    raise InvalidReference("Unit locators name the record start, not a range")
                entry = document['by_start'].get(start)
                if entry is None:
                    raise InvalidReference("locator does not address a live Unit start")
                base.update(deepcopy(entry))
                # Relation links to removed events are ignored without rewriting history.
                base["relations"] = [relation for relation in base["relations"]
                                     if self._is_live_unit(relation["target"], documents={alias: document})]
                from .revision_storage import edit_revision
                base['protected'] = base['ref'] in self.state.get('unit_pins', {})
                head = self.state.get('edit_heads', {}).get(base['ref'], {})
                base['edit_revision'] = edit_revision(base, head.get('transaction_id'))
                return base
            selected = lines[start - 1:end]
            if not any(line.strip() for line in selected) or any(line.startswith(_HEADER) for line in selected):
                raise InvalidReference("locator addresses deleted lines or a file header")
            # Blanked original lines cannot be smuggled into a larger surviving range.
            if self._intersects_tombstone(alias, start, end):
                raise InvalidReference("locator intersects deleted material")
            body = "\n".join(selected)
            base.update({"ref": f"{alias}:{start}-{end}", "start_line": start, "end_line": end, "body": body, "text": body})
            if kind == "detail":
                exact = document['by_start'].get(start)
                entries = deepcopy([exact] if exact and exact['end_line'] == end else
                                   [entry for entry in document['entries']
                                    if entry['start_line'] >= start and entry['end_line'] <= end])
                base["entries"] = entries
                base["facts"] = list(dict.fromkeys(fact for entry in entries for fact in entry["facts"]))
                if len(entries) == 1 and entries[0]["start_line"] == start and entries[0]["end_line"] == end:
                    base.update(entries[0])
            return base

    def _intersects_tombstone(self, alias: str, start: int, end: int) -> bool:
        for target in self.state.get("blanked_ranges", []):
            if target["alias"] == alias and start <= target["end"] and end >= target["start"]:
                return True
        return False

    def _is_live_unit(self, ref: str, *, documents=None) -> bool:
        from .revision_storage import ENTRY_PREFIX
        # Avoid recursively parsing relation cycles.
        try:
            record, match = self._parse_ref(ref)
            if record["kind"] != "unit" or not match["start"] or match["end"]:
                return False
            document = (documents or {}).get(record['alias']) or self._text_document(record)
            lines = document['lines']
            start = int(match["start"])
            return start <= len(lines) and bool(_RECORD.match(lines[start - 1]) or lines[start - 1].startswith(ENTRY_PREFIX))
        except (ValueError, OSError):
            return False

    def validate_ref(self, ref: str) -> bool:
        self.read(ref)
        return True

    def iter_blocks(self, kinds: list[str] | set[str] | None = None, *, aliases=None) -> list[dict]:
        with self.lock():
            results = []
            records = (self.catalog[alias] for alias in aliases) if aliases is not None else self.catalog.values()
            for record in list(records):
                alias, kind = record["alias"], record["kind"]
                subtype = record["metadata"].get("view") if kind == "view" else None
                if kind == 'view' and record['metadata'].get('active') is False:
                    continue
                if kinds is not None and kind not in kinds and subtype not in kinds:
                    continue
                if not self._exists(record["path"]):
                    continue
                if kind == 'media':
                    staged = getattr(self._local, 'staged', {})
                    size = (len(staged[record['path']]) if record['path'] in staged
                            else self._path(record['path']).stat().st_size)
                    if not size:
                        continue
                else:
                    document = self._text_document(record)
                    if not document['size']:
                        continue
                if kind in {"media", "view"}:
                    results.append(self.read(alias, _document=document if kind == 'view' else None))
                    continue
                if kind == "source":
                    # Return contiguous live spans after source deletion.
                    lines = document['lines']
                    run = None
                    for number in range(1, len(lines) + 2):
                        live = number <= len(lines) and not self._intersects_tombstone(alias, number, number)
                        if live and run is None:
                            run = number
                        if not live and run is not None:
                            if any(line.strip() for line in lines[run - 1:number - 1]):
                                results.append(self.read(f"{alias}:{run}-{number - 1}", _document=document))
                            run = None
                else:
                    for entry in document['entries']:
                        results.append(self.read(entry["ref"], _document=document))
            return results

    def _view_path(self, view: str, scope: str | dict | None) -> str:
        if view == "memory":
            return "MEMORY.md"
        if isinstance(scope, dict):
            scope = scope.get("key") or scope.get("topic") or scope.get("date") or scope.get("month")
        if not isinstance(scope, str) or not scope:
            raise ValueError("view scope must identify a day, month, or topic")
        if view == "day":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", scope):
                raise ValueError("day scope must be YYYY-MM-DD")
            datetime.strptime(scope, "%Y-%m-%d")
            return scope.replace("-", "/") + "/summary.md"
        if view == "month":
            if not re.fullmatch(r"\d{4}-\d{2}", scope):
                raise ValueError("month scope must be YYYY-MM")
            datetime.strptime(scope, "%Y-%m")
            return scope.replace("-", "/") + "/summary.md"
        if view == "runtime":
            return "runtime/" + quote(scope, safe="") + ".md"
        raise ValueError("unknown view type")

    def view_ref(self, view: str, scope: str | dict | None, create: bool = False) -> str | None:
        path = self._view_path(view, scope)
        with self.lock():
            found = next((item["alias"] for item in self.catalog.values() if item["path"] == path), None)
            if found or not create:
                return found
            return self.write_view(view, scope, [])

    def write_view(self, view: str, scope: str | dict | None, entries: list[dict], metadata: dict | None = None) -> str:
        path = self._view_path(view, scope)
        with self.lock():
            entries = [_as_dict(entry) for entry in entries]
            existing = self.view_ref(view, scope)
            from .revision_storage import EDITABLE_VIEWS, preserve_view, pins_for
            if view in EDITABLE_VIEWS:
                entries = preserve_view(self, existing, entries)
            pins = pins_for(self, existing) if view in EDITABLE_VIEWS and existing else {}
            for entry in entries:
                if entry.get('entry_id') in pins:
                    continue  # Preserve invalidated citations verbatim; reads flag them.
                for fact in entry.get("facts", []):
                    target = self.read(fact)
                    if target["kind"] != "unit" and not (view == "memory" and target["kind"] == "view" and target.get("view") == "runtime"):
                        raise InvalidReference("view facts must name Units (or Runtime entries for MEMORY)")
            if hasattr(self, 'alias_for_path'):
                existing = self.alias_for_path(path)
                previous = self.catalog[existing]['metadata'] if existing else {}
            else:
                previous = next((item["metadata"] for item in self.catalog.values() if item["path"] == path), {})
            header = dict(previous) | dict(metadata or {}) | {"kind": "view", "view": view, "scope": scope,
                "built_at": datetime.now(timezone.utc).isoformat(), "prompt_version": (metadata or {}).get("prompt_version", "v1.3")}
            if view in EDITABLE_VIEWS:
                header['entry_ids'] = [entry['entry_id'] for entry in entries]
                if pins:
                    header['active'] = True
            if view == "runtime":
                key = scope if isinstance(scope, str) else scope.get("key", scope.get("topic"))
                header.setdefault("key", key)
                header.setdefault("title", key)
                header.setdefault("aliases", [])
            alias = self._allocate("view", path, header)
            self.catalog[alias]["metadata"] = header
            rendered = _HEADER + _json(header) + "\n"
            for item in header.get("directory", []):
                target = item["ref"] if isinstance(item, dict) else item
                self.validate_ref(target)
                label = item.get("title", target) if isinstance(item, dict) else target
                rendered += f"- {label} >{target}\n"
            for entry in entries:
                if view in EDITABLE_VIEWS:
                    from .revision_storage import entry_line
                    rendered += entry_line(dict(entry, source='view', topics=[], relations=[]))
                else:
                    rendered += _render_entry(entry, "view")
            self._transaction({path: rendered})
            return alias

    def blank(self, ref: str) -> None:
        self.blank_many([ref])

    def blank_many(self, refs: list[str]) -> None:
        with self.lock():
            writes: dict[str, str | bytes] = {}
            for ref in refs:
                value = self.read(ref)
                if value["kind"] == "view":
                    raise ValueError("views are rebuilt, not deleted through line blanking")
                record = self.catalog[value["alias"]]
                if value["kind"] == "media":
                    if "start_seconds" in value:
                        raise ValueError("media deletion requires the whole resource; segments share original bytes")
                    writes[record["path"]] = b""
                else:
                    previous = writes.get(record["path"])
                    if previous is None:
                        previous = self._bytes(record["path"]).decode("utf-8")
                    lines = str(previous).splitlines(keepends=True)
                    start, end = value["start_line"], value["end_line"]
                    for index in range(start - 1, end):
                        lines[index] = "\n"
                    writes[record["path"]] = "".join(lines)
                    self.state.setdefault("blanked_ranges", []).append({"alias": value["alias"], "start": start, "end": end})
                deleted = self.state.setdefault("deleted_refs", [])
                if isinstance(deleted, list) and value["ref"] not in deleted:
                    deleted.append(value["ref"])
            self._transaction(writes)
