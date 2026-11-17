"""Read immutable native screen spools; capture and model execution are external.

Only a Host edge_durable receipt establishes custody. This reader does not
delete files, launch a helper, or persist a candidate cursor on its own.
"""

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from ..contracts import validator
from .base import AdapterError, Attachment, Page, Snapshot


FORMAT = "memulife.screen-segment.v1"
CURSOR_FORMAT = "memulife.screen-cursor.v1"
MAX_MANIFEST = 256 * 1024
MAX_CENSUS = 32 * 1024 * 1024
MAX_PAGE = 64 * 1024 * 1024
HASH = re.compile(r"[a-f0-9]{64}")
REASON = re.compile(r"[a-z][a-z0-9_]{0,95}")
FORMATS = FormatChecker()


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _ns(value: object) -> int:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[0-9]{1,20}", value)
        or int(value) >= 2**64
    ):
        raise AdapterError("screen_timestamp_invalid")
    return int(value)


def _json(raw: bytes) -> dict:
    def reject_constant(_):
        raise ValueError

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        result = json.loads(
            raw, object_pairs_hook=pairs, parse_constant=reject_constant
        )
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise AdapterError("screen_manifest_invalid") from None


def _relative(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or "\\" in value
        or "\0" in value
    ):
        raise AdapterError("screen_path_outside_scope")
    parts = value.split("/")
    if PurePosixPath(value).is_absolute() or any(
        p in {"", ".", ".."} or ":" in p for p in parts
    ):
        raise AdapterError("screen_path_outside_scope")
    return tuple(parts)


@lru_cache
def _payload_validator(name: str) -> Draft202012Validator:
    definitions = validator("screen-payloads.schema.json").schema["$defs"]
    return Draft202012Validator(
        {"$ref": f"#/$defs/{name}", "$defs": definitions}, format_checker=FORMATS
    )


class ScreenNativeAdapter:
    adapter_id = "screen.native"
    config_schema = {
        "type": "object",
        "properties": {
            "root": {"type": "string", "minLength": 1},
            "device_id": {"type": "string", "format": "uuid"},
            "stream_id": {"type": "string", "format": "uuid"},
            "backend": {"enum": ["wayland_portal", "windows_wgc"]},
            "helper_path": {"type": "string", "minLength": 1},
            "max_segment_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 64 * 1024 * 1024,
            },
            "max_index_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 8 * 1024 * 1024,
            },
            "max_files": {"type": "integer", "minimum": 1, "maximum": 100000},
        },
        "required": ["root", "device_id", "stream_id", "backend"],
        "additionalProperties": False,
    }

    def __init__(self, config: dict):
        if not Draft202012Validator(
            self.config_schema, format_checker=FORMATS
        ).is_valid(config) or any(
            not _uuid(config.get(key)) for key in ("device_id", "stream_id")
        ):
            raise AdapterError("config_invalid")
        self.root = Path(config["root"]).expanduser().resolve()
        self.device_id = config["device_id"]
        self.stream_id = config["stream_id"]
        self.backend = config["backend"]
        self.helper = (
            Path(config["helper_path"]).expanduser()
            if config.get("helper_path")
            else None
        )
        self.max_segment = config.get("max_segment_bytes", 32 * 1024 * 1024)
        self.max_index = config.get("max_index_bytes", 8 * 1024 * 1024)
        self.max_files = config.get("max_files", 20000)
        self.binding = _digest(
            json.dumps(
                [str(self.root), self.device_id, self.stream_id, self.backend]
            ).encode()
        )
        self._status = "not_probed"

    def _read_file(self, relative: str, maximum: int) -> bytes:
        """Anchor every component to an open directory on POSIX, reject links."""
        parts = _relative(relative)
        fd = None
        directory = None
        try:
            if os.open in os.supports_dir_fd:
                directory = os.open(
                    self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
                for part in parts[:-1]:
                    next_dir = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory,
                    )
                    os.close(directory)
                    directory = next_dir
                fd = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory,
                )
            else:
                # The Windows spool must be an ACL-private directory. Reject
                # reparse points and compare file identity before/after reading.
                current = self.root
                for part in parts:
                    current /= part
                    info = current.lstat()
                    if (
                        stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & 0x400
                    ):
                        raise AdapterError("screen_path_outside_scope")
                fd = os.open(current, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                self._check_windows_handle(fd)
            with os.fdopen(fd, "rb") as source:
                fd = None
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise AdapterError("screen_path_outside_scope")
                if before.st_size > maximum:
                    raise AdapterError("screen_file_budget_exceeded")
                raw = source.read(maximum + 1)
                after = os.fstat(source.fileno())

                def identity(s):
                    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

                if identity(before) != identity(after) or len(raw) != before.st_size:
                    raise AdapterError("screen_file_changed", True)
            return raw
        except OSError as error:
            import errno

            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise AdapterError("screen_path_outside_scope") from None
            raise AdapterError("screen_file_unavailable", True) from None
        finally:
            if fd is not None:
                os.close(fd)
            if directory is not None:
                os.close(directory)

    def _check_windows_handle(self, fd: int) -> None:
        """Check the opened handle, not a racy pathname, before reading bytes."""
        if os.name != "nt":
            raise AdapterError("screen_platform_unavailable")
        import ctypes
        from ctypes import wintypes
        import msvcrt

        get_path = ctypes.WinDLL(
            "kernel32", use_last_error=True
        ).GetFinalPathNameByHandleW
        get_path.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        get_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_path(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
        if not 0 < length < len(buffer):
            raise AdapterError("screen_path_outside_scope")
        name = buffer.value
        if name.startswith("\\\\?\\UNC\\"):
            name = "\\\\" + name[8:]
        elif name.startswith("\\\\?\\"):
            name = name[4:]
        if not Path(name).is_relative_to(self.root):
            raise AdapterError("screen_path_outside_scope")

    def _inventory(self) -> tuple[list[str], int]:
        if not self.root.is_dir():
            raise AdapterError("source_offline", True)
        manifests, pending = [], 0
        for top in ("manifests", "segments", "frames"):
            directory = self.root / top
            if not directory.exists():
                continue
            if directory.is_symlink():
                raise AdapterError("screen_path_outside_scope")
            for session in directory.iterdir():
                if session.is_symlink():
                    raise AdapterError("screen_path_outside_scope")
                if not session.is_dir():
                    continue
                for path in session.iterdir():
                    if path.is_symlink():
                        raise AdapterError("screen_path_outside_scope")
                    if path.name.endswith(".part"):
                        pending += 1
                    elif top == "manifests" and path.suffix == ".json":
                        manifests.append(path.relative_to(self.root).as_posix())
                    if len(manifests) + pending > self.max_files:
                        raise AdapterError("screen_inventory_budget_exceeded")
        return sorted(manifests), pending

    def probe(self) -> dict:
        helper_present = self.helper.is_file() if self.helper is not None else None
        try:
            manifests, pending = self._inventory()
            self._status = "sealed_data_available" if manifests else "waiting_capture"
            if not manifests and helper_present is not True:
                self._status = "helper_unavailable"
            return {
                "adapter_id": self.adapter_id,
                "status": self._status,
                "sealed_manifest_count": len(manifests),
                "unsealed_file_count": pending,
                "helper_present": helper_present,
                "capture_running": None,
                "read_only": True,
                "capture_started": False,
                "device_verified": False,
                "conditions": [
                    "explicit spool scope",
                    "native helper never launched by reader",
                    "edge_durable receipt required before any spool cleanup",
                    "screen processing not connected",
                ],
            }
        except AdapterError as error:
            if error.code != "source_offline":
                raise
            self._status = "spool_unavailable"
            return {
                "adapter_id": self.adapter_id,
                "status": self._status,
                "helper_present": helper_present,
                "capture_running": None,
                "read_only": True,
                "capture_started": False,
                "device_verified": False,
            }

    def health(self) -> dict:
        return self.probe()

    def _manifest(self, path: str, raw: bytes) -> dict:
        item = _json(raw)
        ids = ("device_id", "session_id", "stream_id", "segment_id")
        if (
            item.get("format") != FORMAT
            or item.get("backend") != self.backend
            or item.get("kind") not in {"sealed_segment", "capture_gap"}
            or any(not _uuid(item.get(key)) for key in ids)
            or item["device_id"] != self.device_id
            or item["stream_id"] != self.stream_id
            or path != f"manifests/{item['session_id']}/{item['segment_id']}.json"
        ):
            raise AdapterError("screen_manifest_scope_mismatch")
        start, end = (
            _ns(item.get("start_monotonic_ns")),
            _ns(item.get("end_monotonic_ns")),
        )
        if start > end:
            raise AdapterError("screen_timestamp_invalid")
        for key in ("start_wallclock", "end_wallclock"):
            value = item.get(key)
            if (
                not isinstance(value, str)
                or not FORMATS.conforms(value, "date-time")
                or not (value.endswith("Z") or value.endswith("+00:00"))
            ):
                raise AdapterError("screen_timestamp_invalid")
        coverage = item.get("coverage")
        if (
            not isinstance(coverage, dict)
            or coverage.get("status") not in {"observed_only", "partial", "gap"}
            or not isinstance(coverage.get("notes"), list)
            or len(coverage["notes"]) > 64
            or any(
                not isinstance(note, str) or len(note) > 1024
                for note in coverage["notes"]
            )
            or not isinstance(coverage.get("dropped_frame_ranges"), list)
        ):
            raise AdapterError("screen_coverage_invalid")
        previous_drop = -1
        for dropped in coverage["dropped_frame_ranges"]:
            if (
                not isinstance(dropped, dict)
                or set(dropped) != {"first_seq", "last_seq", "reason"}
                or type(dropped["first_seq"]) is not int
                or type(dropped["last_seq"]) is not int
                or not previous_drop
                < dropped["first_seq"]
                <= dropped["last_seq"]
                < 2**64
                or not isinstance(dropped["reason"], str)
                or not REASON.fullmatch(dropped["reason"])
            ):
                raise AdapterError("screen_coverage_invalid")
            previous_drop = dropped["last_seq"]
        if item["kind"] == "capture_gap":
            if (
                coverage["status"] != "gap"
                or not isinstance(coverage.get("reason"), str)
                or not REASON.fullmatch(coverage["reason"])
                or any(
                    k in item
                    for k in (
                        "relative_path",
                        "sha256",
                        "size_bytes",
                        "frame_index_relative_path",
                    )
                )
            ):
                raise AdapterError("screen_gap_invalid")
            return item
        if coverage["status"] == "gap":
            raise AdapterError("screen_coverage_invalid")
        fields = (
            "width",
            "height",
            "source_width",
            "source_height",
            "frame_count",
            "first_frame_seq",
            "last_frame_seq",
            "size_bytes",
        )
        if any(type(item.get(k)) is not int or item[k] < 0 for k in fields):
            raise AdapterError("screen_segment_invalid")
        if (
            not all(
                0 < item[k] <= 32768
                for k in ("width", "height", "source_width", "source_height")
            )
            or not 0 < item["frame_count"] <= 1000000
            or not 0 <= item["first_frame_seq"] <= item["last_frame_seq"] < 2**64
            or item["frame_count"]
            > item["last_frame_seq"] - item["first_frame_seq"] + 1
            or not 0 < item["size_bytes"] <= self.max_segment
            or item.get("relative_path")
            != f"segments/{item['session_id']}/{item['segment_id']}.mp4"
            or not isinstance(item.get("sha256"), str)
            or not HASH.fullmatch(item["sha256"])
            or item.get("media_type") != "video/mp4"
            or item.get("container") != "fragmented_mp4"
            or item.get("codec") not in {"h264", "h265", "av1"}
            or item.get("state", "sealed") not in {"sealed", "recovered_partial"}
        ):
            raise AdapterError("screen_segment_invalid")
        index_keys = {
            "frame_index_relative_path",
            "frame_index_sha256",
            "frame_index_size_bytes",
        }
        if index_keys & item.keys():
            if (
                not index_keys <= item.keys()
                or item["frame_index_relative_path"]
                != f"frames/{item['session_id']}/{item['segment_id']}.jsonl"
                or not isinstance(item["frame_index_sha256"], str)
                or not HASH.fullmatch(item["frame_index_sha256"])
                or type(item["frame_index_size_bytes"]) is not int
                or not 0 < item["frame_index_size_bytes"] <= self.max_index
            ):
                raise AdapterError("screen_frame_index_invalid")
        return item

    def _media(self, item: dict, prefix: str, maximum: int) -> bytes:
        raw = self._read_file(item[prefix + "relative_path"], maximum)
        if (
            len(raw) != item[prefix + "size_bytes"]
            or _digest(raw) != item[prefix + "sha256"]
        ):
            raise AdapterError("screen_blob_mismatch")
        return raw

    def _frame_index(self, raw: bytes, item: dict) -> None:
        if not raw.endswith(b"\n"):
            raise AdapterError("screen_frame_index_incomplete")
        previous_seq, previous_ns, previous_pts = -1, -1, -1
        frame_ids = set()
        count = 0
        for line in raw.splitlines():
            frame = _json(line)
            if not _payload_validator("CaptureFrame").is_valid(frame):
                raise AdapterError("screen_frame_index_invalid")
            stamp = _ns(frame["monotonic_ns"])
            if (
                frame["capture_session_id"] != item["session_id"]
                or frame["stream_id"] != item["stream_id"]
                or frame["video_chunk_ref"] != item["segment_id"]
                or not item["first_frame_seq"]
                <= frame["frame_seq"]
                <= item["last_frame_seq"]
                or frame["frame_seq"] <= previous_seq
                or stamp < previous_ns
                or frame["pts_us"] < previous_pts
                or not int(item["start_monotonic_ns"])
                <= stamp
                <= int(item["end_monotonic_ns"])
                or frame["width"] != item["width"]
                or frame["height"] != item["height"]
                or frame["image_blob_ref"] is not None
                or frame["frame_id"] in frame_ids
                or count == 0
                and frame["frame_seq"] != item["first_frame_seq"]
            ):
                raise AdapterError("screen_frame_index_scope_mismatch")
            previous_seq, previous_ns, previous_pts = (
                frame["frame_seq"],
                stamp,
                frame["pts_us"],
            )
            frame_ids.add(frame["frame_id"])
            count += 1
        if count != item["frame_count"] or previous_seq != item["last_frame_seq"]:
            raise AdapterError("screen_frame_count_mismatch")

    def _snapshot(self, item: dict, raw: bytes) -> Snapshot:
        identity = f"{item['session_id']}/{item['stream_id']}/{item['segment_id']}"
        metadata = {
            "immutable_record": True,
            "capture_session_id": item["session_id"],
            "stream_id": item["stream_id"],
            "screen_kind": item["kind"],
            "screen_coverage": item["coverage"],
            "processing_state": "waiting_screen_processing",
        }
        # IDs name an immutable sealed object. A fixed native revision lets Host
        # reject altered media/metadata even when a reader cursor is lost.
        record = Snapshot(
            identity,
            raw,
            revision="sealed-v1",
            occurred_at=item["start_wallclock"],
            metadata=metadata,
        )
        if item["kind"] == "capture_gap":
            metadata["native_event_type"] = "source.coverage"
            return record
        video = self._media(item, "", self.max_segment)
        record.attachments.append(Attachment("video", video, item["media_type"]))
        if "frame_index_relative_path" in item:
            index = self._media(item, "frame_index_", self.max_index)
            self._frame_index(index, item)
            record.attachments.append(
                Attachment("frame_index", index, "application/x-ndjson")
            )
        else:
            metadata["frame_index_available"] = False
        blob = {
            "sha256": item["sha256"],
            "size_bytes": len(video),
            "media_type": item["media_type"],
        }
        payload = {
            "chunk_id": item["segment_id"],
            "device_id": self.device_id,
            "capture_session_id": item["session_id"],
            "stream_id": item["stream_id"],
            "start_monotonic_ns": item["start_monotonic_ns"],
            "end_monotonic_ns": item["end_monotonic_ns"],
            "first_frame_seq": item["first_frame_seq"],
            "last_frame_seq": item["last_frame_seq"],
            "state": item.get("state", "sealed"),
            "container": item["container"],
            "codec": item["codec"],
            "blob_ref": blob,
            "manifest_ref": {
                "sha256": _digest(raw),
                "size_bytes": len(raw),
                "media_type": "application/json",
            },
            "recorded_frame_count": item["frame_count"],
            "dropped_frame_ranges": item["coverage"]["dropped_frame_ranges"],
            "source_dimensions": [item["source_width"], item["source_height"]],
            "encoded_dimensions": [item["width"], item["height"]],
        }
        if not _payload_validator("VideoChunk").is_valid(payload):
            raise AdapterError("screen_segment_invalid")
        metadata.update(
            {"native_event_type": "screen.segment", "screen_payload": payload}
        )
        return record

    def read(self, mode: str, cursor: dict | None = None, limit: int = 50) -> Page:
        if mode not in {
            "discover",
            "discovery",
            "live",
            "poll",
            "backfill",
            "reconcile",
        }:
            raise AdapterError("capability_unavailable")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AdapterError("invalid_limit")
        state = (
            deepcopy(cursor)
            if cursor is not None
            else {
                "format": CURSOR_FORMAT,
                "binding": self.binding,
                "seen": {},
                "gap_reasons": [],
            }
        )
        if (
            not isinstance(state, dict)
            or state.get("format") != CURSOR_FORMAT
            or state.get("binding") != self.binding
            or not isinstance(state.get("seen"), dict)
            or len(state["seen"]) > self.max_files
            or not isinstance(state.get("gap_reasons"), list)
        ):
            raise AdapterError("screen_cursor_invalid")
        if (
            any(
                not isinstance(name, str)
                or not isinstance(digest, str)
                or not HASH.fullmatch(digest)
                for name, digest in state["seen"].items()
            )
            or len(state["gap_reasons"]) > 128
            or any(
                not isinstance(reason, str) or not REASON.fullmatch(reason)
                for reason in state["gap_reasons"]
            )
            or not isinstance(state.get("verify_after", ""), str)
        ):
            raise AdapterError("screen_cursor_invalid")
        names, pending = self._inventory()
        warnings = {"unsealed_segments_pending"} if pending else set()
        manifests = {}
        census_size = 0
        for name in names:
            raw = self._read_file(name, MAX_MANIFEST)
            census_size += len(raw)
            if census_size > MAX_CENSUS:
                raise AdapterError("screen_inventory_budget_exceeded")
            item = self._manifest(name, raw)
            digest = _digest(raw)
            if name in state["seen"] and state["seen"][name] != digest:
                raise AdapterError("sealed_manifest_changed")
            manifests[name] = (item, raw, digest)
            if (
                item["kind"] == "sealed_segment"
                and "frame_index_relative_path" not in item
            ):
                warnings.add("frame_index_unavailable")
            if item["coverage"]["status"] != "observed_only":
                warnings.add("screen_coverage_partial")
        # Removed spool files do not become source tombstones. Host may already
        # own their bytes. Pruning absent identities bounds a maintained spool.
        state["seen"] = {
            name: value for name, value in state["seen"].items() if name in manifests
        }
        verify_after = state.get("verify_after", "") if mode == "reconcile" else ""
        candidates = [
            name
            for name in names
            if name not in state["seen"] or mode == "reconcile" and name > verify_after
        ]
        selection = getattr(self, "import_selection", None)
        if selection:
            from ..sources.selection import in_time

            candidates = [
                name
                for name in candidates
                if f"{manifests[name][0]['session_id']}/{manifests[name][0]['stream_id']}/{manifests[name][0]['segment_id']}"
                in selection.scopes
                and in_time(manifests[name][0]["start_wallclock"], selection)
            ]
        records, used, processed = [], 0, 0
        for name in candidates:
            item, raw, digest = manifests[name]
            size = (
                len(raw)
                + item.get("size_bytes", 0)
                + item.get("frame_index_size_bytes", 0)
            )
            if size > MAX_PAGE:
                raise AdapterError("screen_page_budget_exceeded")
            if processed >= limit or used + size > MAX_PAGE:
                break
            snapshot = self._snapshot(item, raw)
            if name not in state["seen"]:
                records.append(snapshot)
            state["seen"][name] = digest
            used += size
            processed += 1
            if item["kind"] == "capture_gap":
                state["gap_reasons"] = sorted(
                    set(state["gap_reasons"]) | {item["coverage"]["reason"]}
                )
            if mode == "reconcile":
                state["verify_after"] = name
        has_more = processed < len(candidates)
        if mode == "reconcile" and not has_more:
            state["verify_after"] = ""
        warnings.update("capture_gap:" + reason for reason in state["gap_reasons"])
        self._status = "partial_coverage" if warnings else "sealed_data_available"
        return Page(records, state, has_more, False, sorted(warnings))

    def fetch_attachment(self, ref: dict) -> Attachment:
        if not isinstance(ref, dict) or set(ref) != {"manifest_path", "attachment_id"}:
            raise AdapterError("screen_attachment_reference_invalid")
        raw = self._read_file(ref["manifest_path"], MAX_MANIFEST)
        record = self._snapshot(self._manifest(ref["manifest_path"], raw), raw)
        for attachment in record.attachments:
            if attachment.attachment_id == ref["attachment_id"]:
                return attachment
        raise AdapterError("screen_attachment_unavailable")
