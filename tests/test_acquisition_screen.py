"""Sealed spool fixtures test custody transfer without starting screen capture."""


from copy import deepcopy


from datetime import datetime, timedelta, timezone


import hashlib


import json


from pathlib import Path


from uuid import UUID, uuid4


import pytest


from memu.acquisition.adapters.base import AdapterError


from memu.acquisition.adapters.screen import ScreenNativeAdapter
from memu.acquisition.contracts import validator


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode()


def write(root, relative, data):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def spool(tmp_path):
    root = tmp_path / "spool"
    root.mkdir()
    config = {
        "root": str(root),
        "device_id": str(uuid4()),
        "stream_id": str(uuid4()),
        "backend": "wayland_portal",
    }
    return root, config


def seal(
    spool, *, segment_id=None, frame_index=True, kind="sealed_segment", session_id=None
):
    root, config = spool
    session, segment = session_id or str(uuid4()), segment_id or str(uuid4())
    start = datetime(2026, 10, 22, tzinfo=timezone.utc)
    item = {
        "format": "memulife.screen-segment.v1",
        "backend": config["backend"],
        "kind": kind,
        "device_id": config["device_id"],
        "session_id": session,
        "stream_id": config["stream_id"],
        "segment_id": segment,
        "start_monotonic_ns": "90071992547409930",
        "end_monotonic_ns": "90071993547409930",
        "start_wallclock": start.isoformat(),
        "end_wallclock": (start + timedelta(seconds=1)).isoformat(),
        "coverage": {
            "status": "observed_only",
            "notes": [],
            "dropped_frame_ranges": [],
        },
    }
    if kind == "capture_gap":
        item["coverage"].update({"status": "gap", "reason": "permission_revoked"})
    else:
        # Codec bytes are fixtures, not a claim of device/decoder qualification.
        video = b"fixture-ftyp-moov-moof-mdat-" + segment.encode()
        item.update(
            {
                "relative_path": f"segments/{session}/{segment}.mp4",
                "sha256": sha(video),
                "size_bytes": len(video),
                "media_type": "video/mp4",
                "container": "fragmented_mp4",
                "codec": "h264",
                "width": 1280,
                "height": 720,
                "source_width": 1280,
                "source_height": 720,
                "frame_count": 2,
                "first_frame_seq": 20,
                "last_frame_seq": 21,
            }
        )
        write(root, item["relative_path"], video)
        if frame_index:
            frames = []
            for index in range(2):
                frames.append(
                    {
                        "frame_id": f"{session}/{item['stream_id']}/{index + 20}",
                        "capture_session_id": session,
                        "stream_id": item["stream_id"],
                        "frame_seq": 20 + index,
                        "monotonic_ns": str(
                            int(item["start_monotonic_ns"]) + index * 1_000_000_000
                        ),
                        "captured_at": (start + timedelta(seconds=index)).isoformat(),
                        "width": 1280,
                        "height": 720,
                        "rotation": 0,
                        "scale_factor": 1.0,
                        "color_space": "srgb",
                        "pixel_format": "bgra8",
                        "active_window_ref": None,
                        "accessibility_snapshot_ref": None,
                        "video_chunk_ref": segment,
                        "pts_us": index * 1_000_000,
                        "image_blob_ref": None,
                        "capture_quality": "native",
                    }
                )
            raw = b"".join(encode(frame) + b"\n" for frame in frames)
            item.update(
                {
                    "frame_index_relative_path": f"frames/{session}/{segment}.jsonl",
                    "frame_index_sha256": sha(raw),
                    "frame_index_size_bytes": len(raw),
                }
            )
            write(root, item["frame_index_relative_path"], raw)
        else:
            item["coverage"].update(
                {"status": "partial", "notes": ["frame_index_unavailable"]}
            )
    path = f"manifests/{session}/{segment}.json"
    write(root, path, encode(item))
    return item, path


def test_sealed_segment_retains_raw_native_time_and_contract_payload(spool):
    root, config = spool
    item, path = seal(spool)
    adapter = ScreenNativeAdapter(config)
    page = adapter.read("discover")
    assert len(page.records) == 1 and not page.has_more and not page.scope_complete
    record = page.records[0]
    assert record.text is None and record.raw == (root / path).read_bytes()
    assert record.revision == "sealed-v1"
    assert record.metadata["processing_state"] == "waiting_screen_processing"
    assert (
        record.metadata["screen_payload"]["start_monotonic_ns"] == "90071992547409930"
    )
    assert record.metadata["screen_payload"]["manifest_ref"]["sha256"] == sha(
        record.raw
    )
    schema = validator("screen-payloads.schema.json").schema
    from jsonschema import Draft202012Validator, FormatChecker

    Draft202012Validator(
        {"$ref": "#/$defs/VideoChunk", "$defs": schema["$defs"]},
        format_checker=FormatChecker(),
    ).validate(record.metadata["screen_payload"])
    assert [a.attachment_id for a in record.attachments] == ["video", "frame_index"]
    assert sha(record.attachments[0].data) == item["sha256"]
    assert not adapter.read("live", page.cursor).records


def test_candidate_cursor_replay_and_late_seal_before_sort_position(spool):
    root, config = spool
    session = str(uuid4())
    seal(spool, session_id=session, segment_id=str(UUID(int=200)))
    adapter = ScreenNativeAdapter(config)
    first = adapter.read("live", limit=1)
    retry = ScreenNativeAdapter(config).read("live", limit=1)
    assert first == retry  # No reader-side cursor advancement before Host ACK.
    part = write(root, f"segments/{session}/earlier.mp4.part", b"still-writing")
    pending = adapter.read("live", first.cursor)
    assert not pending.records and "unsealed_segments_pending" in pending.warnings
    part.unlink()
    item, _ = seal(spool, session_id=session, segment_id=str(UUID(int=1)))
    complete = adapter.read("live", pending.cursor)
    assert len(complete.records) == 1
    assert item["segment_id"] in complete.records[0].object_id


def test_gap_is_persisted_and_remains_visible_after_spool_retention(spool):
    root, config = spool
    gap, path = seal(spool, kind="capture_gap")
    adapter = ScreenNativeAdapter(config)
    first = adapter.read("live")
    assert first.records[0].attachments == [] and first.records[0].text is None
    assert first.records[0].metadata["native_event_type"] == "source.coverage"
    assert "capture_gap:permission_revoked" in first.warnings
    (root / path).unlink()
    later = adapter.read("live", first.cursor)
    assert later.records == [] and "capture_gap:permission_revoked" in later.warnings
    assert not later.scope_complete
    assert gap["coverage"]["reason"] == "permission_revoked"


def test_windows_profile_can_deliver_video_without_fabricated_frame_index(spool):
    root, config = spool
    config["backend"] = "windows_wgc"
    seal(spool, frame_index=False)
    page = ScreenNativeAdapter(config).read("live")
    assert len(page.records[0].attachments) == 1
    assert page.records[0].metadata["frame_index_available"] is False
    assert "frame_index_unavailable" in page.warnings
    repeated = ScreenNativeAdapter(config).read("live", page.cursor)
    assert not repeated.records and "frame_index_unavailable" in repeated.warnings
    with pytest.raises(AdapterError, match="screen_manifest_scope_mismatch"):
        ScreenNativeAdapter(config | {"backend": "wayland_portal"}).read("live")


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("media_hash", "screen_blob_mismatch"),
        ("media_size", "screen_blob_mismatch"),
        ("manifest_changed", "sealed_manifest_changed"),
        ("traversal", "screen_segment_invalid"),
        ("foreign_device", "screen_manifest_scope_mismatch"),
        ("integer_nanoseconds", "screen_timestamp_invalid"),
        ("overflow_nanoseconds", "screen_timestamp_invalid"),
    ],
)
def test_corrupt_or_wrong_scope_seal_never_advances_cursor(spool, mutation, code):
    root, config = spool
    item, path = seal(spool)
    adapter = ScreenNativeAdapter(config)
    first = adapter.read("live")
    cursor = first.cursor if mutation == "manifest_changed" else None
    if mutation == "media_hash":
        target = root / item["relative_path"]
        target.write_bytes(target.read_bytes().replace(b"fixture", b"changed"))
    elif mutation == "media_size":
        (root / item["relative_path"]).write_bytes(b"short")
    elif mutation == "manifest_changed":
        item["coverage"]["notes"] = ["changed-after-seal"]
    elif mutation == "traversal":
        item["relative_path"] = "../outside.mp4"
    elif mutation == "foreign_device":
        item["device_id"] = str(uuid4())
    elif mutation == "integer_nanoseconds":
        item["start_monotonic_ns"] = int(item["start_monotonic_ns"])
    else:
        item["end_monotonic_ns"] = str(2**64)
    if mutation not in {"media_hash", "media_size"}:
        write(root, path, encode(item))
    unchanged = deepcopy(cursor)
    with pytest.raises(AdapterError, match=code):
        adapter.read("live", cursor)
    assert cursor == unchanged


def test_sealed_media_symlink_and_parent_link_rejected(spool, tmp_path):
    root, config = spool
    item, _ = seal(spool)
    media = root / item["relative_path"]
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(media.read_bytes())
    media.unlink()
    media.symlink_to(outside)
    with pytest.raises(AdapterError, match="screen_path_outside_scope"):
        ScreenNativeAdapter(config).read("live")
    media.unlink()
    media.parent.rmdir()
    media.parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(AdapterError, match="screen_path_outside_scope"):
        ScreenNativeAdapter(config).read("live")


@pytest.mark.parametrize(
    "fault", ["incomplete", "wrong_frame_scope", "duplicate_frame_id", "wrong_count"]
)
def test_index_contract_and_correspondence_are_checked(spool, fault):
    root, config = spool
    item, path = seal(spool)
    index_path = root / item["frame_index_relative_path"]
    frames = [json.loads(line) for line in index_path.read_bytes().splitlines()]
    if fault == "wrong_frame_scope":
        frames[1]["video_chunk_ref"] = "another-video"
    elif fault == "duplicate_frame_id":
        frames[1]["frame_id"] = frames[0]["frame_id"]
    elif fault == "wrong_count":
        frames = frames[:1]
    raw = b"".join(encode(frame) + b"\n" for frame in frames)
    if fault == "incomplete":
        raw = raw[:-1]
    index_path.write_bytes(raw)
    item.update({"frame_index_sha256": sha(raw), "frame_index_size_bytes": len(raw)})
    write(root, path, encode(item))
    with pytest.raises(AdapterError, match="screen_frame_"):
        ScreenNativeAdapter(config).read("live")


def test_probe_never_launches_helper_or_creates_capture_files(tmp_path):
    root = tmp_path / "absent-spool"
    helper = tmp_path / "should-never-run"
    helper.write_text("raise SystemExit('capture must not run')")
    config = {
        "root": str(root),
        "device_id": str(uuid4()),
        "stream_id": str(uuid4()),
        "backend": "wayland_portal",
        "helper_path": str(helper),
    }
    adapter = ScreenNativeAdapter(config)
    probe = adapter.probe()
    assert probe["status"] == "spool_unavailable" and probe["helper_present"]
    assert probe["capture_started"] is False and probe["capture_running"] is None
    assert not root.exists()
    root.mkdir()
    helper.unlink()
    assert adapter.health()["status"] == "helper_unavailable"
    assert list(root.iterdir()) == []


def test_reconcile_paginates_rechecks_existing_bytes_and_new_files(spool):
    root, config = spool
    session = str(uuid4())
    first_item, _ = seal(spool, session_id=session, segment_id=str(UUID(int=1)))
    second_item, _ = seal(spool, session_id=session, segment_id=str(UUID(int=2)))
    adapter = ScreenNativeAdapter(config)
    original = adapter.read("live")
    first = adapter.read("reconcile", original.cursor, limit=1)
    assert first.has_more and not first.records
    second = adapter.read("reconcile", first.cursor, limit=1)
    assert not second.has_more and not second.records
    assert second.cursor["verify_after"] == ""
    target = root / first_item["relative_path"]
    target.write_bytes(target.read_bytes() + b"corruption")
    with pytest.raises(AdapterError, match="screen_blob_mismatch"):
        adapter.read("reconcile", second.cursor, limit=1)
    assert (root / second_item["relative_path"]).exists()


def test_fetch_attachment_requires_valid_manifest_and_page_budget(spool):
    _, config = spool
    item, path = seal(spool)
    adapter = ScreenNativeAdapter(config)
    attachment = adapter.fetch_attachment(
        {"manifest_path": path, "attachment_id": "video"}
    )
    assert sha(attachment.data) == item["sha256"]
    with pytest.raises(AdapterError, match="screen_attachment_reference_invalid"):
        adapter.fetch_attachment({"path": item["relative_path"]})
    with pytest.raises(AdapterError, match="screen_segment_invalid"):
        ScreenNativeAdapter(config | {"max_segment_bytes": 1}).read("live")
