"""Recover decodable orphan segments; preserve unreadable media with an explicit gap."""

from datetime import datetime
import json
from pathlib import Path
from uuid import UUID, uuid5

from . import CaptureError
from .recorder import validate_video
from .state import Spool, private_read, sync_dir


def recover_pending(root, device_id, stream_id, *, emit=print):
    root = Path(root)
    descriptors = sorted((root / "segments").glob("*/*.mp4.open.json"))
    if len(descriptors) > 1000:
        raise CaptureError("recovery_budget_exceeded")
    for path in descriptors:
        saved = private_read(path)
        session = str(UUID(saved["session_id"]))
        segment = str(UUID(saved["segment_id"]))
        if (
            saved.get("device_id") != device_id
            or saved.get("stream_id") != stream_id
            or saved.get("backend") != "wayland_portal"
        ):
            raise CaptureError("recovery_scope_mismatch")
        if path != root / "segments" / session / (segment + ".mp4.open.json"):
            raise CaptureError("recovery_scope_mismatch")
        spool = Spool(root, device_id, session, stream_id, emit=emit)
        if spool.identity(saved["number"]) != segment:
            raise CaptureError("recovery_identity_mismatch")
        manifest = root / "manifests" / session / (segment + ".json")
        if manifest.exists():
            path.unlink()
            sync_dir(path.parent)
            continue
        gap_id = str(uuid5(UUID(segment), "unrecoverable"))
        if (root / "manifests" / session / (gap_id + ".json")).exists():
            continue
        first = saved.get("first_frame")
        try:
            if not isinstance(first, dict):
                raise CaptureError("recovery_frame_metadata_missing")
            spool.boot_id = saved["boot_id"]
            spool.clock.id = saved["clock_mapping_id"]
            spool.clock.monotonic_ns = int(first["monotonic_ns"])
            spool.clock.wall_ns = int(
                datetime.fromisoformat(first["captured_at"]).timestamp() * 1_000_000_000
            )
            media = spool.part(saved["number"])
            if not media.exists():
                media = media.with_suffix("")
            if media.is_symlink() or not 0 < media.stat().st_size <= 32 * 1024 * 1024:
                raise CaptureError("recovery_media_invalid")
            pts = validate_video(media, first["width"], first["height"])
            index = root / "frames" / session / (segment + ".jsonl")
            has_native_index = index.exists()
            if has_native_index:
                if index.is_symlink() or index.stat().st_size > 8 * 1024 * 1024:
                    raise CaptureError("recovery_index_invalid")
                frames = [json.loads(line) for line in index.read_bytes().splitlines()]
            else:
                frames = []
                for offset, pts_us in enumerate(pts):
                    mono = int(first["monotonic_ns"]) + (pts_us - pts[0]) * 1000
                    seq = first["frame_seq"] + offset
                    frames.append(
                        first
                        | {
                            "frame_id": f"{device_id}:{spool.boot_id}:{session}:{stream_id}:{seq}",
                            "frame_seq": seq,
                            "monotonic_ns": str(mono),
                            "captured_at": spool.clock.wall(mono),
                            "capture_quality": "recovered",
                            "pts_us": pts_us,
                        }
                    )
            if any(
                frame.get("capture_session_id") != session
                or frame.get("stream_id") != stream_id
                or frame.get("video_chunk_ref") != segment
                for frame in frames
            ):
                raise CaptureError("recovery_index_scope_mismatch")
            spool.seal(
                saved["number"],
                frames,
                width=first["width"],
                height=first["height"],
                source_dimensions=saved["source_dimensions"],
                dropped=[],
                validator=validate_video,
                recovered=True,
                write_frame_index=has_native_index,
            )
            path.unlink()
            sync_dir(path.parent)
        except (CaptureError, OSError, ValueError, KeyError, TypeError):
            # Bounds beyond the durable opening anchor are unknown after a crash.
            # Do not reinterpret a new boot's monotonic clock as the old session.
            value = {
                key: saved[key]
                for key in (
                    "format",
                    "device_id",
                    "session_id",
                    "stream_id",
                    "start_monotonic_ns",
                    "start_wallclock",
                    "boot_id",
                    "clock_mapping_id",
                    "clock_uncertainty_ms",
                    "backend",
                )
            }
            value.update(
                segment_id=gap_id,
                kind="capture_gap",
                end_monotonic_ns=saved["start_monotonic_ns"],
                end_wallclock=saved["start_wallclock"],
                coverage={
                    "status": "gap",
                    "reason": "unsealed_segment_unrecoverable",
                    "notes": ["gap_end_unknown", "unsealed_media_preserved"],
                    "dropped_frame_ranges": [],
                },
            )
            spool.publish(value)
