"""Private, crash-safe state and immutable spool publication."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from uuid import UUID, uuid4, uuid5

from . import CaptureError


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_dir(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise CaptureError("unsafe_spool_path")
    if not path.exists():
        private_dir(path.parent)
        path.mkdir(mode=0o700)
        sync_dir(path.parent)
    if not path.is_dir():
        raise CaptureError("unsafe_spool_path")
    return path


def private_write(path, data):
    private_dir(path.parent)
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".part")
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise CaptureError("private_state_required")
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise CaptureError("state_too_large")
    return json.loads(raw)


class TokenStore:
    def __init__(self, root, device_id, stream_id):
        self.root = private_dir(root)
        self.device_id = str(UUID(device_id))
        self.stream_id = str(UUID(stream_id))
        self.path = self.root / "portal-state.json"

    @contextmanager
    def lock(self):
        fd = os.open(
            self.root / "capture.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CaptureError("capture_already_running") from None
            yield
        finally:
            os.close(fd)

    def begin(self, session_id, *, reselect=False):
        saved = private_read(self.path) if self.path.exists() else {}
        if saved and (
            saved.get("device_id") != self.device_id
            or saved.get("stream_id") != self.stream_id
        ):
            raise CaptureError("device_binding_mismatch")
        if saved.get("state") == "consuming" and not reselect:
            raise CaptureError("needs_source_selection")
        token = None if reselect else saved.get("restore_token")
        # Consuming is durable before any call can spend the one-use token.
        private_write(
            self.path,
            encoded(
                {
                    "format": "memulife.portal-state.v1",
                    "device_id": self.device_id,
                    "stream_id": self.stream_id,
                    "session_id": session_id,
                    "state": "consuming",
                    "restore_token": None,
                }
            ),
        )
        return token, saved.get("streams", {}) if token else {}

    def started(self, session_id, result, previous):
        streams = result.get("streams")
        if not isinstance(streams, (list, tuple)) or len(streams) != 1:
            raise CaptureError("portal_stream_invalid")
        node, properties = streams[0]
        if (
            isinstance(node, bool)
            or not isinstance(node, int)
            or not 0 <= node < 2**32
            or not isinstance(properties, dict)
        ):
            raise CaptureError("portal_stream_invalid")
        source_id = properties.get("id")
        stream_id = self.stream_id
        serial = properties.get("pipewire-serial")
        if serial is not None and (
            isinstance(serial, bool)
            or not isinstance(serial, int)
            or not 0 <= serial < 2**64
        ):
            raise CaptureError("portal_stream_invalid")
        token = result.get("restore_token")
        if token is not None and (
            not isinstance(token, str) or not token or len(token) > 65536
        ):
            raise CaptureError("portal_token_invalid")
        # Stream metadata + replacement token are one atomic durability boundary.
        private_write(
            self.path,
            encoded(
                {
                    "format": "memulife.portal-state.v1",
                    "device_id": self.device_id,
                    "stream_id": self.stream_id,
                    "session_id": session_id,
                    "state": "ready",
                    "restore_token": token,
                    "streams": {str(source_id): stream_id} if source_id else {},
                    "selection": {"node_id": node, "properties": properties},
                }
            ),
        )
        return {
            "stream_id": stream_id,
            "node_id": node,
            "properties": properties,
            "target_property": "target-object" if serial is not None else "path",
            "target": str(serial if serial is not None else node),
        }


class ClockMapping:
    def __init__(self):
        self.id = str(uuid4())
        before = time.monotonic_ns()
        self.wall_ns = time.time_ns()
        after = time.monotonic_ns()
        self.monotonic_ns = (before + after) // 2
        self.uncertainty_ms = max(1, (after - before) / 2_000_000)

    def wall(self, monotonic_ns):
        value = (self.wall_ns + int(monotonic_ns) - self.monotonic_ns) / 1_000_000_000
        return datetime.fromtimestamp(value, timezone.utc).isoformat()


class Spool:
    def __init__(self, root, device_id, session_id, stream_id, *, emit=print):
        self.root = private_dir(root)
        self.device_id, self.session_id, self.stream_id = map(
            lambda x: str(UUID(x)), (device_id, session_id, stream_id)
        )
        self.clock = ClockMapping()
        self.emit = emit
        self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        for directory in ("segments", "frames", "manifests"):
            private_dir(self.root / directory / self.session_id)

    def identity(self, number):
        return str(uuid5(UUID(self.session_id), "segment:" + str(number)))

    def part(self, number):
        return (
            self.root
            / "segments"
            / self.session_id
            / (self.identity(number) + ".mp4.part")
        )

    def publish(self, value):
        path = (
            self.root / "manifests" / self.session_id / (value["segment_id"] + ".json")
        )
        raw = encoded(value)
        if path.exists():
            if path.read_bytes() != raw:
                raise CaptureError("immutable_manifest_conflict")
        else:
            private_write(path, raw)
        self.emit(raw.decode())
        return value

    def common(self, segment_id, start, end):
        return {
            "format": "memulife.screen-segment.v1",
            "device_id": self.device_id,
            "session_id": self.session_id,
            "stream_id": self.stream_id,
            "segment_id": segment_id,
            "start_monotonic_ns": str(start),
            "end_monotonic_ns": str(max(start, end)),
            "start_wallclock": self.clock.wall(start),
            "end_wallclock": self.clock.wall(max(start, end)),
            "boot_id": self.boot_id,
            "clock_mapping_id": self.clock.id,
            "clock_uncertainty_ms": self.clock.uncertainty_ms,
            "backend": "wayland_portal",
        }

    def gap(self, reason, start=None, end=None, *, notes=None):
        start = time.monotonic_ns() if start is None else start
        end = time.monotonic_ns() if end is None else end
        return self.publish(
            self.common(str(uuid4()), start, end)
            | {
                "kind": "capture_gap",
                "coverage": {
                    "status": "gap",
                    "reason": reason,
                    "notes": notes or [],
                    "dropped_frame_ranges": [],
                },
            }
        )

    def seal(
        self,
        number,
        frames,
        *,
        width,
        height,
        source_dimensions,
        dropped,
        validator,
        recovered=False,
        write_frame_index=True,
    ):
        path = self.part(number)
        if recovered and not path.exists():
            path = path.with_suffix("")
        if not frames:
            raise CaptureError("empty_segment")
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not 0 < info.st_size <= 32 * 1024 * 1024
        ):
            raise CaptureError("segment_size_invalid")
        pts = validator(path, width, height)
        if len(pts) != len(frames):
            raise CaptureError("frame_count_mismatch")
        chunk_id = self.identity(number)
        for frame, pts_us in zip(frames, pts):
            frame["video_chunk_ref"] = chunk_id
            frame["pts_us"] = pts_us
        index = b"".join(encoded(frame) + b"\n" for frame in frames)
        frame_path = self.root / "frames" / self.session_id / (chunk_id + ".jsonl")
        if (
            write_frame_index
            and frame_path.exists()
            and frame_path.read_bytes() != index
        ):
            raise CaptureError("immutable_frame_index_conflict")
        if write_frame_index and not frame_path.exists():
            private_write(frame_path, index)
        index_fields = (
            {
                "frame_index_relative_path": frame_path.relative_to(
                    self.root
                ).as_posix(),
                "frame_index_sha256": hashlib.sha256(index).hexdigest(),
                "frame_index_size_bytes": len(index),
            }
            if write_frame_index
            else {}
        )
        digest = hashlib.sha256()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
            os.fsync(source.fileno())
        final = self.part(number).with_suffix("")
        if path != final:
            if final.exists():
                raise CaptureError("immutable_segment_conflict")
            path.rename(final)
        sync_dir(final.parent)
        start, end = int(frames[0]["monotonic_ns"]), int(frames[-1]["monotonic_ns"])
        return self.publish(
            self.common(chunk_id, start, end)
            | {
                "kind": "sealed_segment",
                "state": "recovered_partial" if recovered else "sealed",
                "relative_path": final.relative_to(self.root).as_posix(),
                "sha256": digest.hexdigest(),
                "size_bytes": info.st_size,
                "media_type": "video/mp4",
                "container": "fragmented_mp4",
                "codec": "h264",
                "width": width,
                "height": height,
                "source_width": source_dimensions[0],
                "source_height": source_dimensions[1],
                "frame_count": len(frames),
                "first_frame_seq": frames[0]["frame_seq"],
                "last_frame_seq": frames[-1]["frame_seq"],
                "coverage": {
                    "status": "partial",
                    "notes": [
                        "sampled_video",
                        "upstream_drop_count_unknown",
                        "frame_scale_describes_encoded_raster",
                    ]
                    + (["recovered_after_interruption"] if recovered else [])
                    + (
                        []
                        if write_frame_index
                        else [
                            "recovered_frame_index_unavailable",
                            "frame_sequence_reconstructed_from_decoded_order",
                        ]
                    ),
                    "dropped_frame_ranges": dropped,
                },
            }
            | index_fields
        )
