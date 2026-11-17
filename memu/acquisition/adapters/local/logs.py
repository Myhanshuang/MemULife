"""Incremental readers for authorized Codex, Claude Code, and Gemini CLI logs."""

from __future__ import annotations

import gzip
import io
import json
import os
from copy import deepcopy
from pathlib import Path

from ..base import AdapterError, Page, Snapshot
from .common import (
    LOCAL_CONFIG_SCHEMA,
    LocalAccess,
    digest,
    encode,
    native_id,
    text_parts,
    timestamp,
)
from .gemini import fold


class LocalLogAdapter:
    adapter_id = "local.logs"
    config_schema = LOCAL_CONFIG_SCHEMA
    patterns: tuple[str, ...] = ()

    def __init__(self, config: dict):
        self.config = config
        self.access = LocalAccess(config)
        self._status = "ready"
        self._decoded_cache = None

    def probe(self) -> dict:
        files = self.access.files(self.patterns)
        return {
            "adapter_id": self.adapter_id,
            "status": "ready",
            "files": len(files),
            "capabilities": {
                "poll": True,
                "backfill": True,
                "reconcile": True,
                "subscribe": False,
                "read_only": True,
            },
            "coverage": "authorized_local_files",
            "device_verified": False,
            "conditions": [
                "saved complete records only",
                "compressed logs have a 64 MiB decoded budget",
                "external referenced history is reported, not followed",
            ],
        }

    def health(self) -> dict:
        return {"status": self._status, "read_only": True, "foreground_actions": False}

    def fetch_attachment(self, ref: dict):
        return self.access.fetch_attachment(ref)

    def read(self, mode: str, cursor: dict | None = None, limit: int = 50) -> Page:
        if mode not in {"discover", "live", "poll", "backfill", "reconcile"}:
            raise AdapterError("capability_unavailable")
        if not 1 <= limit <= 1000:
            raise AdapterError("invalid_limit")
        state = deepcopy(cursor or {"files": {}})
        if state.get("root") not in {None, str(self.access.root)}:
            raise AdapterError("source_binding_mismatch")
        state["root"] = str(self.access.root)
        files = self.access.files(self.patterns)
        records: list[Snapshot] = []
        warnings: list[str] = []
        has_more = False
        for path in files:
            if len(records) >= limit:
                has_more = True
                break
            key = str(path.relative_to(self.access.root))
            previous = state.setdefault("files", {}).get(key, {})
            if path.suffix == ".json":
                result, next_state = self._snapshot_file(path, previous)
                if result:
                    records.append(result)
                state["files"][key] = next_state
                continue
            result, next_state, more, gaps = self._tail(
                path, previous, limit - len(records)
            )
            records.extend(result)
            state["files"][key] = next_state
            warnings.extend(gaps)
            has_more |= more
        self._status = "partial_coverage" if warnings else "ready"
        # A completed census proves only the supplied on-disk scope, not all app history.
        return Page(
            records,
            state,
            has_more,
            not has_more and not warnings,
            sorted(set(warnings)),
        )

    def _tail(self, path: Path, previous: dict, limit: int):
        state = deepcopy(previous)
        records = []
        warnings = []
        with self.access.open(path) as file:
            stat = os.fstat(file.fileno())
            identity = f"{stat.st_dev}:{stat.st_ino}"
            file_stamp = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
            if previous.get('finished_stat') == file_stamp:
                return [], previous, False, []
            state.pop('finished_stat', None)
            if self.exclude_session(file, path):
                return [], {'finished_stat': file_stamp, 'excluded': 'internal_review'}, False, ['internal_session_excluded']
            file.seek(0)
            stream = file
            cached = self._decoded_cache
            if path.suffix in {'.gz', '.zst'} and cached and cached[:2] == (str(path), file_stamp):
                stream = io.BytesIO(cached[2])
            elif path.suffix in {".gz", ".zst"}:
                try:
                    if path.suffix == ".gz":
                        with gzip.GzipFile(fileobj=file) as reader:
                            decoded = reader.read(64 * 1024 * 1024 + 1)
                    else:
                        import zstandard

                        compressed = file.read(64 * 1024 * 1024 + 1)
                        if len(compressed) > 64 * 1024 * 1024:
                            raise AdapterError("compressed_budget_exceeded")
                        size = zstandard.frame_content_size(compressed)
                        if (
                            size
                            not in {
                                zstandard.CONTENTSIZE_UNKNOWN,
                                zstandard.CONTENTSIZE_ERROR,
                            }
                            and size > 64 * 1024 * 1024
                        ):
                            raise AdapterError("decompressed_budget_exceeded")
                        try:
                            decoded = zstandard.ZstdDecompressor().decompress(
                                compressed,
                                max_output_size=64 * 1024 * 1024,
                                allow_extra_data=False,
                            )
                        except zstandard.ZstdError:
                            return [], previous, False, ["compressed_file_incomplete"]
                    if len(decoded) > 64 * 1024 * 1024:
                        raise AdapterError("decompressed_budget_exceeded")
                    stream = io.BytesIO(decoded)
                    self._decoded_cache = (str(path), file_stamp, decoded)
                except (EOFError, gzip.BadGzipFile):
                    return [], previous, False, ["compressed_file_incomplete"]
            offset = int(state.get("offset", 0))
            try:
                # The rolling suffix detects in-place replacement/truncation even when
                # a new file has already grown past the old committed byte position.
                start = max(0, offset - 4096)
                stream.seek(start)
                prefix = stream.read(offset - start)
                changed = state.get("identity") not in {None, identity} or (
                    offset
                    and (
                        len(prefix) != offset - start
                        or digest(prefix) != state.get("prefix")
                    )
                )
                if changed:
                    state = {"generation": state.get("generation", 0) + 1}
                    offset = 0
                    warnings.append("file_generation_changed")
                stream.seek(offset)
                self.prime(stream, state)
                state["identity"] = identity
                state.setdefault("generation", 0)
                state.setdefault("ordinal", 0)
                while len(records) < limit:
                    raw = stream.readline(self.access.max_bytes + 1)
                    if not raw:
                        break
                    if len(raw) > self.access.max_bytes:
                        raise AdapterError("record_too_large")
                    if not raw.endswith(b"\n"):
                        warnings.append("incomplete_tail")
                        break
                    try:
                        value = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        raise AdapterError("incompatible_schema") from None
                    if not isinstance(value, dict):
                        raise AdapterError("incompatible_schema")
                    state["ordinal"] += 1
                    result, gaps = self.project(value, raw, state, path)
                    records.append(result)
                    warnings.extend(gaps)
                    offset += len(raw)
                stream.seek(offset)
                has_more = bool(stream.read(1)) and "incomplete_tail" not in warnings
                start = max(0, offset - 4096)
                stream.seek(start)
                state["offset"] = offset
                state["prefix"] = digest(stream.read(offset - start))
                if not has_more and 'incomplete_tail' not in warnings:
                    state['finished_stat'] = file_stamp
            except (EOFError, gzip.BadGzipFile):
                # No candidate progress is published for an incompletely sealed gzip.
                return [], previous, False, ["compressed_file_incomplete"]
        if len(encode(state)) > 16 * 1024 * 1024:
            raise AdapterError("fold_state_budget_exceeded")
        return records, state, has_more, warnings

    def exclude_session(self, file, path):
        return False

    def _snapshot_file(self, path: Path, previous: dict):
        raise AdapterError("capability_unavailable")

    def prime(self, stream, state):
        """Subclasses may inspect metadata without advancing the committed cursor."""

    def project(self, value: dict, raw: bytes, state: dict, path: Path):
        raise NotImplementedError

    def record(
        self,
        value: dict,
        raw: bytes,
        state: dict,
        path: Path,
        *,
        object_id: str,
        body: str | None,
        actor: str = "unknown",
        extra: dict | None = None,
    ):
        ordinal = native_id(value.get("ordinal")) or str(state["ordinal"])
        revision = f"{state.get('generation', 0)}:{ordinal}:{digest(raw)}"
        metadata = {
            "immutable_record": True,
            "record_identity": revision,
            "ordinal": ordinal,
            "file": str(path.relative_to(self.access.root)),
            "generation": state.get("generation", 0),
            "session_id": state.get("session"),
            "actor": {"source_actor_id": None, "role": actor, "display_name": None},
        }
        metadata.update(extra or {})
        return Snapshot(
            object_id,
            raw,
            body,
            revision=revision,
            occurred_at=timestamp(value.get("timestamp")),
            metadata=metadata,
        )


class CodexAdapter(LocalLogAdapter):
    adapter_id = "local.codex"
    patterns = (
        "sessions/**/*.jsonl",
        "archived_sessions/**/*.jsonl",
        "sessions/**/*.jsonl.gz",
        "archived_sessions/**/*.jsonl.gz",
        "sessions/**/*.jsonl.zst",
        "archived_sessions/**/*.jsonl.zst",
    )

    def __init__(self, config):
        # Codex can place several tool-returned images in one JSONL record.
        # Explicit source limits still win; this changes no HTTP/model budget.
        super().__init__({**config, 'max_record_bytes': config.get('max_record_bytes', 16 * 1024 * 1024)})

    def exclude_session(self, file, path):
        # Only decode the small session header, including for large compressed logs.
        from ...sources.local_ai_origin import session_kind
        from zstandard import ZstdError

        if path.suffix == '.zst':
            import zstandard
            stream = io.BufferedReader(zstandard.ZstdDecompressor().stream_reader(file, closefd=False))
        elif path.suffix == '.gz':
            stream = gzip.GzipFile(fileobj=file)
        else:
            stream = file
        try:
            header = stream.readline(65536)
            value = json.loads(header)
            return (value.get('type') == 'session_meta' and
                    session_kind(value.get('payload', {}).get('source')) == 'internal_review')
        except (ValueError, EOFError, OSError, ZstdError):
            return False  # The normal reader reports damaged or incomplete inputs.
        finally:
            if stream is not file:
                stream.close()

    def project(self, value, raw, state, path):
        item = value.get("item", value)
        if not isinstance(item, dict):
            raise AdapterError("incompatible_schema")
        kind = item.get("type")
        payload = item.get("payload", item)
        if not isinstance(payload, dict):
            payload = {"unknown": payload}
        if kind == "session_meta":
            meta = payload.get("meta", payload)
            session = native_id(meta.get("id")) or native_id(meta.get("session_id"))
            if session:
                state["session"] = session
            state["forked_from_id"] = meta.get("forked_from_id")
            state['session_source'] = meta.get('source')
            state['originator'] = meta.get('originator')
        if not state.get("session"):
            raise AdapterError("session_identity_missing")
        warnings = []
        if kind in {"reference", "retained_context", "rollout_reference"}:
            warnings.append("referenced_history_unresolved")
        if kind == "turn_context":
            state["model"] = payload.get("model")
            state["turn_id"] = payload.get("turn_id")
        native = native_id(payload.get("id")) or native_id(payload.get("call_id"))
        role = payload.get("role", "unknown")
        if role not in {"user", "assistant", "system"}:
            role = "unknown"
        # Mirrored notification rows remain durable raw objects but do not become
        # another textual memory; native response_item carries the visible answer.
        mirrored = kind == "event_msg" and payload.get("type") in {
            "agent_message",
            "user_message",
        }
        body = None if mirrored else text_parts(payload)
        object_id = (
            f"{state['session']}/{kind}/{native or 'record:' + str(state['ordinal'])}"
        )
        return self.record(
            value,
            raw,
            state,
            path,
            object_id=object_id,
            body=body,
            actor=role,
            extra={
                "native_type": kind,
                "model": state.get("model"),
                "turn_id": state.get("turn_id"),
                "mirrored_notification": mirrored,
                "forked_from_id": state.get("forked_from_id"),
                'session_source': state.get('session_source'),
                'originator': state.get('originator'),
            },
        ), warnings


class ClaudeCodeAdapter(LocalLogAdapter):
    adapter_id = "local.claude-code"
    patterns = (
        "projects/**/*.jsonl",
        "projects/**/*.jsonl.gz",
        "projects/**/*.jsonl.zst",
    )

    def prime(self, stream, state):
        if state.get("session"):
            return
        offset = stream.tell()
        try:
            for _ in range(64):
                raw = stream.readline(self.access.max_bytes + 1)
                if not raw.endswith(b"\n") or len(raw) > self.access.max_bytes:
                    break
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    break
                if isinstance(value, dict) and native_id(value.get("sessionId")):
                    state["session"] = native_id(value["sessionId"])
                    break
        finally:
            stream.seek(offset)

    def project(self, value, raw, state, path):
        session = native_id(value.get("sessionId")) or state.get("session")
        if not session:
            raise AdapterError("session_identity_missing")
        state["session"] = session
        record_id = native_id(value.get("uuid"))
        native = record_id or f"record:{state['ordinal']}"
        role = value.get("type", "unknown")
        if role not in {"user", "assistant", "system"}:
            role = "unknown"
        subagent = native_id(value.get("agentId"))
        stream = f"{session}/agent:{subagent}" if subagent else session
        return self.record(
            value,
            raw,
            state,
            path,
            object_id=f"{stream}/{native}",
            body=text_parts(value.get("message", value)),
            actor=role,
            extra={
                "parent_uuid": value.get("parentUuid"),
                "sidechain": value.get("isSidechain"),
                "identity_strength": "native" if record_id else "provisional",
            },
        ), []


class GeminiCLIAdapter(LocalLogAdapter):
    adapter_id = "local.gemini-cli"
    patterns = (
        "tmp/*/chats/session-*.jsonl",
        "tmp/*/chats/session-*.json",
        "tmp/*/chats/session-*.jsonl.gz",
        "tmp/*/chats/session-*.jsonl.zst",
    )

    def project(self, value, raw, state, path):
        accumulator, warnings = fold(state.get("fold", {}), value)
        state["fold"] = accumulator
        session = native_id(accumulator["metadata"].get("sessionId")) or state.get(
            "session"
        )
        if not session:
            raise AdapterError("session_identity_missing")
        state["session"] = session
        # The immutable source record and the resulting complete active view travel
        # together; rewind removes context, never historical evidence or raw rows.
        projected = {
            "record": value,
            "active_messages": list(accumulator["messages"].values()),
            "metadata": accumulator["metadata"],
        }
        body = "\n".join(
            text_parts(message.get("content", ""))
            for message in accumulator["messages"].values()
        )
        snapshot = self.record(
            value,
            raw,
            state,
            path,
            object_id=f"{session}/active-context",
            body=body,
            extra={
                "active_ids": list(accumulator["messages"]),
                "fold_schema": 1,
                "projection": projected,
                "semantic_messages": [accumulator['messages'][update['id']]
                    for update in (([value['$patch']] if 'id' in value.get('$patch', {}) else []) +
                                   value.get('$patch', {}).get('updates', []))
                    if update.get('id') in accumulator['messages']],
            },
        )
        return snapshot, warnings

    def _snapshot_file(self, path, previous):
        raw = self.access.read_file(path)
        checksum = digest(raw)
        if previous.get("digest") == checksum:
            return None, previous
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AdapterError("incompatible_schema") from None
        if (
            not isinstance(value, dict)
            or not native_id(value.get("sessionId"))
            or not isinstance(value.get("messages"), list)
        ):
            raise AdapterError("incompatible_schema")
        text = "\n".join(
            text_parts(message.get("content", "")) for message in value["messages"]
        )
        return Snapshot(
            f"{value['sessionId']}/active-context",
            raw,
            text,
            metadata={
                "session_id": value["sessionId"],
                "legacy_snapshot": True,
                "active_ids": [message.get("id") for message in value["messages"]],
            },
        ), {"digest": checksum}
