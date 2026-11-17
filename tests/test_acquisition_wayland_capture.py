import hashlib
import json
import os
import queue
import signal
import subprocess
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from native.wayland import CaptureError
from native.wayland.__main__ import emit_hint
from native.wayland.portal import PATH, Portal, authorize
from native.wayland.recovery import recover_pending
from native.wayland.recorder import Recorder, safe_gstreamer_error
from native.wayland.state import Spool, TokenStore, encoded, private_read, private_write


def timestamp_recorder(monkeypatch):
    sample = {"host": 5000, "clock": 2000, "base": 0}
    monkeypatch.setattr(time, "monotonic_ns", lambda: sample["host"])
    recorder = Recorder.__new__(Recorder)
    clock = SimpleNamespace(get_time=lambda: sample["clock"])
    recorder.pipeline = SimpleNamespace(
        get_clock=lambda: clock,
        get_base_time=lambda: sample["base"],
    )
    recorder.spool = SimpleNamespace(clock=SimpleNamespace(uncertainty_ms=1))
    recorder.clock_anchor = None
    recorder.last_running_ns = recorder.last_captured_ns = None
    diagnostics = []
    recorder.diagnostic = diagnostics.append
    assert recorder._timestamp(1000) == (5000, 4000)
    return recorder, sample, diagnostics


def test_timestamp_mapping_does_not_turn_read_jitter_into_regression(monkeypatch):
    recorder, sample, diagnostics = timestamp_recorder(monkeypatch)
    sample.update(host=5010, clock=2200)
    # The previous per-buffer subtraction yielded 3910, a false regression.
    assert recorder._timestamp(1100) == (5010, 4100)
    assert not diagnostics


def test_timestamp_regression_survives_segment_drain(monkeypatch):
    recorder, sample, diagnostics = timestamp_recorder(monkeypatch)
    # No queued frames remain after a segment is sealed.
    recorder.frames = []
    sample.update(host=6000, clock=2500)
    with pytest.raises(CaptureError, match="source_timestamp_regressed"):
        recorder._timestamp(900)
    assert diagnostics[0]["kind"] == "source_running_time"
    assert diagnostics[0]["running_ns"] == "900"
    assert recorder.last_running_ns == 1000
    assert recorder.last_captured_ns == 4000


@pytest.mark.parametrize("change", ["base", "clock", "offset"])
def test_timestamp_mapping_stops_when_its_clock_changes(monkeypatch, change):
    recorder, sample, _ = timestamp_recorder(monkeypatch)
    if change == "base":
        sample["base"] = 1
    elif change == "clock":
        recorder.pipeline.get_clock = lambda: SimpleNamespace(get_time=lambda: 2000)
    else:
        sample["clock"] += 3_000_000_000
    with pytest.raises(CaptureError, match="capture_clock_changed"):
        recorder._timestamp(1100)
    assert recorder.last_running_ns == 1000


class FakePortal:
    properties = {"version": 6, "AvailableSourceTypes": 3, "AvailableCursorModes": 3}

    def __init__(
        self, *, token="new-token", fail=None, node=42, serial=90071992547409939
    ):
        self.calls = []
        self.token, self.fail, self.node, self.serial = token, fail, node, serial

    def create(self):
        self.calls.append("CreateSession")

    def select(self, **options):
        self.calls.append(("SelectSources", options))

    def start(self):
        self.calls.append("Start")
        if self.fail:
            raise CaptureError(self.fail)
        properties = {"id": "stable-selection"}
        if self.serial is not None:
            properties["pipewire-serial"] = self.serial
        return {"streams": [(self.node, properties)], "restore_token": self.token}

    def open_remote(self):
        self.calls.append("OpenPipeWireRemote")
        return 123

    def close(self):
        self.calls.append("Close")


@pytest.mark.parametrize("mode", ["early", "delayed", "cancelled", "timeout"])
@pytest.mark.parametrize("stage", ["CreateSession", "SelectSources", "Start"])
def test_portal_response_timing_and_safe_stage_diagnostics(mode, stage):
    diagnostics, calls = [], []
    portal = Portal.__new__(Portal)
    portal.diagnostic = diagnostics.append
    portal._values = lambda options: options
    callbacks = {}
    result = {"restore_token": "fixture-secret-token", "title": "private-window-title"}

    def respond():
        if mode != "timeout":
            callbacks["response"](1 if mode == "cancelled" else 0, result)

    def match(callback, **options):
        callbacks["response"] = callback
        calls.append("subscribed")
        return SimpleNamespace(remove=lambda: calls.append("unsubscribed"))

    def method(*args, **options):
        assert calls == ["subscribed"]
        calls.append(stage)
        if mode == "early":
            respond()
        return f"{PATH}/request/1_23/{args[-1]['handle_token']}"

    portal.bus = SimpleNamespace(
        get_unique_name=lambda: ":1.23",
        add_signal_receiver=match,
        get_object=lambda *_: object(),
    )
    portal.interface = SimpleNamespace(**{stage: method})
    portal.dbus = SimpleNamespace(
        Interface=lambda *_: SimpleNamespace(Close=lambda **_: calls.append("closed"))
    )
    portal.GLib = SimpleNamespace(
        MainLoop=lambda: SimpleNamespace(run=respond, quit=lambda: None),
        timeout_add_seconds=lambda *_: 1,
        MainContext=SimpleNamespace(
            default=lambda: SimpleNamespace(find_source_by_id=lambda _: True)
        ),
        source_remove=lambda _: None,
    )
    if mode in {"timeout", "cancelled"}:
        with pytest.raises(CaptureError) as failure:
            portal.request(stage, options={"restore_token": "fixture-secret-token"})
        assert failure.value.stage == stage
        assert failure.value.code == (
            "portal_request_timeout" if mode == "timeout" else "selection_cancelled"
        )
        assert ("closed" in calls) == (mode == "timeout")
    else:
        assert (
            portal.request(stage, options={"restore_token": "fixture-secret-token"})
            == result
        )
    assert calls[-1] == "unsubscribed"
    assert diagnostics[0] == {"portal_stage": stage, "event": "request_started"}
    assert "fixture-secret-token" not in json.dumps(diagnostics)
    assert "private-window-title" not in json.dumps(diagnostics)


def test_portal_lifecycle_rotates_token_before_remote_open(tmp_path):
    store = TokenStore(tmp_path, str(uuid4()), str(uuid4()))
    first = FakePortal(token="first-token")
    with store.lock():
        stream, fd = authorize(first, store, str(uuid4()), allow_selection=True)
    assert fd == 123
    assert first.calls[0] == "CreateSession"
    assert first.calls[-2:] == ["Start", "OpenPipeWireRemote"]
    assert stream["target_property"] == "target-object"
    assert stream["target"] == "90071992547409939"
    assert private_read(store.path)["restore_token"] == "first-token"
    second = FakePortal(token="rotated-token")

    def opened():
        assert private_read(store.path)["restore_token"] == "rotated-token"
        return 124

    second.open_remote = opened
    with store.lock():
        restored, _ = authorize(second, store, str(uuid4()), allow_selection=True)
    assert restored["stream_id"] == stream["stream_id"]
    assert second.calls[1][1]["token"] == "first-token"


def test_no_implicit_selection_or_retry_consumed_token(tmp_path):
    store = TokenStore(tmp_path, str(uuid4()), str(uuid4()))
    portal = FakePortal(fail="portal_request_timeout")
    with pytest.raises(CaptureError, match="needs_source_selection"):
        authorize(portal, store, str(uuid4()), allow_selection=False)
    assert portal.calls == [] and not store.path.exists()
    with pytest.raises(CaptureError, match="portal_request_timeout"):
        authorize(portal, store, str(uuid4()), allow_selection=True)
    assert portal.calls[-1] == "Close"
    assert private_read(store.path)["state"] == "consuming"
    next_portal = FakePortal()
    with pytest.raises(CaptureError, match="needs_source_selection"):
        authorize(next_portal, store, str(uuid4()), allow_selection=True)
    assert next_portal.calls == []
    authorize(next_portal, store, str(uuid4()), allow_selection=True, reselect=True)
    assert next_portal.calls[1][1]["token"] is None


def test_start_commit_failure_never_opens_remote(tmp_path, monkeypatch):
    store = TokenStore(tmp_path, str(uuid4()), str(uuid4()))
    portal = FakePortal()
    monkeypatch.setattr(
        store,
        "started",
        lambda *_: (_ for _ in ()).throw(OSError("synthetic disk failure")),
    )
    with pytest.raises(OSError):
        authorize(portal, store, str(uuid4()), allow_selection=True)
    assert "OpenPipeWireRemote" not in portal.calls
    assert private_read(store.path)["state"] == "consuming"


def test_legacy_node_identity_and_private_lock(tmp_path):
    store = TokenStore(tmp_path, str(uuid4()), str(uuid4()))
    with store.lock():
        with pytest.raises(CaptureError, match="capture_already_running"):
            with store.lock():
                pass
        stream, _ = authorize(
            FakePortal(serial=None), store, str(uuid4()), allow_selection=True
        )
    assert stream["target_property"] == "path"
    assert stream["target"] == "42"
    assert store.path.stat().st_mode & 0o777 == 0o600
    store.path.chmod(0o644)
    with pytest.raises(CaptureError, match="private_state_required"):
        store.begin(str(uuid4()))


@pytest.mark.parametrize("node,serial", [(True, 1), (2**32, 1), (1, 2**64)])
def test_invalid_stream_never_publishes_session(tmp_path, node, serial):
    store = TokenStore(tmp_path, str(uuid4()), str(uuid4()))
    portal = FakePortal(node=node, serial=serial)
    with pytest.raises(CaptureError, match="portal_stream_invalid"):
        authorize(portal, store, str(uuid4()), allow_selection=True)
    assert "OpenPipeWireRemote" not in portal.calls
    assert private_read(store.path)["state"] == "consuming"


def test_seal_publishes_only_after_media_and_index_exist(tmp_path):
    emitted = []
    spool = Spool(
        tmp_path, str(uuid4()), str(uuid4()), str(uuid4()), emit=emitted.append
    )
    part = spool.part(0)
    part.write_bytes(b"synthetic encoded bytes")
    frame = {"frame_seq": 1, "monotonic_ns": "90071992547409939"}
    value = spool.seal(
        0,
        [frame],
        width=2,
        height=2,
        source_dimensions=(2, 2),
        dropped=[],
        validator=lambda *_: [0],
    )
    assert not part.exists()
    assert (
        tmp_path / value["relative_path"]
    ).read_bytes() == b"synthetic encoded bytes"
    assert value["sha256"] == hashlib.sha256(b"synthetic encoded bytes").hexdigest()
    assert emitted == [json.dumps(value, ensure_ascii=False, separators=(",", ":"))]
    assert (tmp_path / value["frame_index_relative_path"]).exists()
    assert value["start_monotonic_ns"] == "90071992547409939"


def test_invalid_decode_is_never_announced_as_sealed(tmp_path):
    emitted = []
    spool = Spool(
        tmp_path, str(uuid4()), str(uuid4()), str(uuid4()), emit=emitted.append
    )
    spool.part(0).write_bytes(b"truncated")
    with pytest.raises(CaptureError, match="frame_count_mismatch"):
        spool.seal(
            0,
            [{"frame_seq": 1}],
            width=2,
            height=2,
            source_dimensions=(2, 2),
            dropped=[],
            validator=lambda *_: [],
        )
    assert emitted == []
    assert not list((tmp_path / "manifests").rglob("*.json"))
    value = spool.gap("source_stalled", 100, 150)
    assert value["kind"] == "capture_gap" and "relative_path" not in value
    assert value["coverage"]["reason"] == "source_stalled"


def test_cli_requires_manual_authorization_without_side_effects(tmp_path):
    process = subprocess.run(
        [
            "/usr/bin/python3",
            "-m",
            "native.wayland",
            "run",
            "--spool",
            str(tmp_path / "untouched"),
            "--device-id",
            str(uuid4()),
            "--stream-id",
            str(uuid4()),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 1
    assert json.loads(process.stderr)["error_code"] == "needs_source_selection"
    assert not (tmp_path / "untouched").exists()


def test_unreadable_recovery_emits_one_gap_and_preserves_media(tmp_path):
    emitted = []
    device_id, stream_id = str(uuid4()), str(uuid4())
    spool = Spool(tmp_path, device_id, str(uuid4()), stream_id, emit=emitted.append)
    spool.part(0).write_bytes(b"incomplete")
    private_write(
        spool.part(0).with_suffix(".open.json"),
        encoded(
            spool.common(spool.identity(0), 123, 123)
            | {"kind": "open_segment", "number": 0}
        ),
    )
    recover_pending(tmp_path, device_id, stream_id, emit=emitted.append)
    recover_pending(tmp_path, device_id, stream_id, emit=emitted.append)
    assert len(emitted) == 1
    gap = json.loads(emitted[0])
    assert gap["coverage"]["reason"] == "unsealed_segment_unrecoverable"
    assert "gap_end_unknown" in gap["coverage"]["notes"]
    assert spool.part(0).read_bytes() == b"incomplete"


def test_full_or_closed_stdout_cannot_block_durable_publication():
    read_fd, write_fd = os.pipe()
    try:
        for _ in range(1000):
            emit_hint("x" * 2048, fd=write_fd)
        os.close(read_fd)
        read_fd = None
        emit_hint("closed", fd=write_fd)
    finally:
        if read_fd is not None:
            os.close(read_fd)
        os.close(write_fd)


def test_real_gstreamer_synthetic_segments(tmp_path):
    """Test-pattern pixels only: no session bus, portal request, or desktop capture."""
    check = subprocess.run(
        ["/usr/bin/python3", "-m", "native.wayland", "probe", "--dependencies-only"],
        capture_output=True,
        timeout=10,
    )
    if check.returncode:
        pytest.skip("system GStreamer/PyGObject capture dependencies unavailable")
    device_id, stream_id = str(uuid4()), str(uuid4())
    process = subprocess.run(
        [
            "/usr/bin/python3",
            "-m",
            "native.wayland",
            "self-test",
            "--spool",
            str(tmp_path / "spool"),
            "--device-id",
            device_id,
            "--stream-id",
            stream_id,
        ],
        capture_output=True,
        text=True,
        timeout=45,
        env=os.environ | {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent"},
    )
    assert process.returncode == 0, process.stderr
    values = [json.loads(line) for line in process.stdout.splitlines()]
    assert len(values) >= 2 and all(
        value["kind"] == "sealed_segment" for value in values
    )
    counts = 0
    for value in values:
        assert value["size_bytes"] < 32 * 1024 * 1024
        media = tmp_path / "spool" / value["relative_path"]
        assert hashlib.sha256(media.read_bytes()).hexdigest() == value["sha256"]
        index = tmp_path / "spool" / value["frame_index_relative_path"]
        frames = [json.loads(line) for line in index.read_text().splitlines()]
        assert len(frames) == value["frame_count"]
        assert all(frame["video_chunk_ref"] == value["segment_id"] for frame in frames)
        counts += len(frames)
    assert counts >= 20
    from memu.acquisition.adapters.screen import ScreenNativeAdapter

    adapter = ScreenNativeAdapter(
        {
            "root": str(tmp_path / "spool"),
            "device_id": device_id,
            "stream_id": stream_id,
            "backend": "wayland_portal",
        }
    )
    page = adapter.read("live")
    assert len(page.records) == len(values)
    assert all(
        record.metadata["native_event_type"] == "screen.segment"
        for record in page.records
    )
    first = values[0]
    recovery_root = tmp_path / "recoverable-spool"
    recovered = []
    spool = Spool(
        recovery_root, device_id, first["session_id"], stream_id, emit=recovered.append
    )
    spool.part(0).write_bytes(
        (tmp_path / "spool" / first["relative_path"]).read_bytes()
    )
    opening_frame = json.loads(
        (tmp_path / "spool" / first["frame_index_relative_path"])
        .read_text()
        .splitlines()[0]
    )
    private_write(
        spool.part(0).with_suffix(".open.json"),
        encoded(
            spool.common(
                first["segment_id"],
                int(first["start_monotonic_ns"]),
                int(first["start_monotonic_ns"]),
            )
            | {
                "kind": "open_segment",
                "number": 0,
                "first_frame": opening_frame,
                "source_dimensions": [first["source_width"], first["source_height"]],
            }
        ),
    )
    recover_pending(recovery_root, device_id, stream_id, emit=recovered.append)
    assert len(recovered) == 1
    recovered_manifest = json.loads(recovered[0])
    assert recovered_manifest["state"] == "recovered_partial"
    assert recovered_manifest["sha256"] == first["sha256"]
    assert "frame_index_relative_path" not in recovered_manifest
    assert (
        "frame_sequence_reconstructed_from_decoded_order"
        in recovered_manifest["coverage"]["notes"]
    )
    page = ScreenNativeAdapter(
        {
            "root": str(recovery_root),
            "device_id": device_id,
            "stream_id": stream_id,
            "backend": "wayland_portal",
        }
    ).read("live")
    assert len(page.records) == 1


def test_synthetic_duration_limit_is_enforced_inside_helper(tmp_path):
    check = subprocess.run(
        ["/usr/bin/python3", "-m", "native.wayland", "probe", "--dependencies-only"],
        capture_output=True,
        timeout=30,
    )
    if check.returncode:
        pytest.skip("system GStreamer/PyGObject capture dependencies unavailable")
    start = time.monotonic()
    result = subprocess.run(
        [
            "/usr/bin/python3",
            "-m",
            "native.wayland",
            "self-test",
            "--spool",
            str(tmp_path / "limited"),
            "--device-id",
            str(uuid4()),
            "--stream-id",
            str(uuid4()),
            "--buffers",
            "1800",
            "--max-seconds",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        env=os.environ | {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent"},
    )
    assert result.returncode == 0, result.stderr
    values = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(value["kind"] == "sealed_segment" for value in values)
    assert any(
        value["kind"] == "capture_gap"
        and value["coverage"]["reason"] == "duration_limit"
        for value in values
    )
    assert time.monotonic() - start < 20


def test_duration_limit_rejects_invalid_setting_before_selection(tmp_path):
    result = subprocess.run(
        [
            "/usr/bin/python3",
            "-m",
            "native.wayland",
            "run",
            "--allow-selection",
            "--spool",
            str(tmp_path / "untouched"),
            "--device-id",
            str(uuid4()),
            "--stream-id",
            str(uuid4()),
            "--max-seconds",
            "121",
        ],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert not (tmp_path / "untouched").exists()


def test_gstreamer_error_diagnostics_redact_provider_details_and_keep_stage():
    message = SimpleNamespace(
        parse_error=lambda: (
            SimpleNamespace(
                domain="gst-stream-error-quark",
                code=1,
                message="not-negotiated for private-window-title /private/path secret-token",
            ),
            "debug includes private-window-title",
        ),
        src=SimpleNamespace(
            get_factory=lambda: SimpleNamespace(get_name=lambda: "pipewiresrc")
        ),
    )
    diagnostic = safe_gstreamer_error(message, stage="streaming")
    assert diagnostic["reason"] == "capture_format_negotiation_failed"
    assert diagnostic["element"] == "pipewiresrc"
    assert diagnostic["domain"] == "gst-stream-error-quark" and diagnostic["code"] == 1
    assert "private" not in json.dumps(diagnostic)
    assert "secret-token" not in json.dumps(diagnostic)
    emitted = []
    recorder = Recorder.__new__(Recorder)
    recorder.diagnostic = emitted.append
    recorder.stop_reason = None
    messages = iter([message, None])
    recorder.bus = SimpleNamespace(pop_filtered=lambda _: next(messages))
    recorder.Gst = SimpleNamespace(MessageType=SimpleNamespace(ERROR=1))
    with pytest.raises(CaptureError, match="capture_format_negotiation_failed"):
        recorder._startup_error()
    assert emitted[0]["gstreamer_stage"] == "pipeline_start"


def test_gstreamer_filesink_access_error_is_not_pipewire_permission():
    message = SimpleNamespace(
        parse_error=lambda: (
            SimpleNamespace(
                domain="gst-resource-error-quark",
                code=3,
                message="Permission denied /private/path",
            ),
            None,
        ),
        src=SimpleNamespace(
            get_factory=lambda: SimpleNamespace(get_name=lambda: "filesink")
        ),
    )
    assert (
        safe_gstreamer_error(message, stage="streaming")["reason"]
        == "capture_resource_denied"
    )


def test_synchronous_start_failure_persists_gap_before_raising():
    recorder = Recorder.__new__(Recorder)
    recorder.work = queue.Queue()
    recorder._seal_worker = lambda: recorder.work.get()
    recorder.GLib = SimpleNamespace(
        PRIORITY_DEFAULT=0,
        timeout_add_seconds=lambda *_: 1,
        unix_signal_add=lambda *_: 2,
        source_remove=lambda _: None,
        MainContext=SimpleNamespace(
            default=lambda: SimpleNamespace(find_source_by_id=lambda _: False)
        ),
    )
    recorder.Gst = SimpleNamespace(
        State=SimpleNamespace(PLAYING=1, NULL=0),
        StateChangeReturn=SimpleNamespace(FAILURE=-1),
    )
    recorder.pipeline = SimpleNamespace(set_state=lambda state: -1 if state == 1 else 0)
    recorder.bus = SimpleNamespace(remove_signal_watch=lambda: None)
    recorder._watchdog = lambda: True
    recorder._startup_error = lambda: (_ for _ in ()).throw(
        CaptureError("capture_format_negotiation_failed")
    )
    recorder.stop_reason = None
    recorder.source_frames = 0
    recorder.frame_seq = 0
    diagnostics = []
    recorder.diagnostic = diagnostics.append
    recorder.closed = False
    recorder.last_frame = 123
    recorder.seal_errors = []
    recorder.pending_paths = set()
    gaps = []
    recorder.spool = SimpleNamespace(
        gap=lambda *args, **options: gaps.append((args, options))
    )
    with pytest.raises(CaptureError, match="capture_format_negotiation_failed"):
        recorder.run()
    assert recorder.closed
    assert gaps[0][0] == ("capture_format_negotiation_failed", 123)
    assert diagnostics == [
        {
            "event": "capture_summary",
            "source_frames": 0,
            "frame_seq": 0,
            "reason": "capture_format_negotiation_failed",
        }
    ]


def test_real_gstreamer_contains_encoder_reconfigure_but_preserves_source_failure():
    script = """
import json
from types import SimpleNamespace
from native.wayland.recorder import Recorder, dependencies
Gst, GLib = dependencies()
recorder = Recorder.__new__(Recorder)
recorder.Gst, recorder.GLib = Gst, GLib
recorder.source_dimensions = None
recorder.reconfigure_contained = False
recorder.setting_output_caps = False
recorder.stop_reason = None
recorder.fps, recorder.longest_edge = 5, 1280
diagnostics, stops = [], []
recorder.diagnostic = diagnostics.append
recorder.stop = lambda reason: stops.append(reason)
recorder.loop = SimpleNamespace(quit=lambda: None)
recorder.caps = Gst.ElementFactory.make('capsfilter')
pad = Gst.Pad.new('synthetic_source', Gst.PadDirection.SRC)
sink = recorder.caps.get_static_pad('sink')
assert pad.link(sink) == Gst.PadLinkReturn.OK
pad.set_active(True)
sink.set_active(True)
sink.add_probe(Gst.PadProbeType.EVENT_UPSTREAM, recorder._upstream_event)
# Unrelated renegotiation still reaches and marks the source pad.
recorder.caps.set_property('caps', Gst.Caps.from_string('video/x-raw,format=I420'))
assert pad.check_reconfigure()
def source_caps(width):
    event = Gst.Event.new_caps(Gst.Caps.from_string(f'video/x-raw,format=BGRA,width={width},height=898,framerate=0/1'))
    return SimpleNamespace(type=Gst.PadProbeType.EVENT_DOWNSTREAM, get_event=lambda: event)
recorder._source_event(pad, source_caps(1702))
# Critically, DROP at the downstream sink prevents NEED_RECONFIGURE itself;
# dropping at the source pad would already be too late.
assert not pad.check_reconfigure()
assert any(item['event'] == 'downstream_reconfigure_contained' for item in diagnostics)
recorder._source_event(pad, source_caps(1702))
assert not pad.check_reconfigure()
recorder.caps.set_property('caps', Gst.Caps.from_string('video/x-raw,format=I420,width=640'))
assert pad.check_reconfigure()
recorder._source_event(pad, source_caps(1800))
while GLib.MainContext.default().iteration(False):
    pass
assert stops == ['display_config_changed']
error = GLib.Error.new_literal(Gst.ResourceError.quark(), 'all buffers have been removed', int(Gst.ResourceError.NOT_FOUND))
message = Gst.Message.new_error(Gst.ElementFactory.make('pipewiresrc'), error, 'PipeWire link to remote node was destroyed')
recorder._message(None, message)
assert recorder.stop_reason == 'source_disconnected'
pad.set_active(False)
sink.set_active(False)
print(json.dumps({'reconfigure_contained': True, 'resize_stops': True, 'disconnect_stops': True}))
"""
    check = subprocess.run(
        ["/usr/bin/python3", "-m", "native.wayland", "probe", "--dependencies-only"],
        capture_output=True,
        timeout=30,
    )
    if check.returncode:
        pytest.skip("system GStreamer/PyGObject capture dependencies unavailable")
    result = subprocess.run(
        ["/usr/bin/python3", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=os.environ | {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent"},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["disconnect_stops"]


@pytest.mark.parametrize(
    ("max_seconds", "expected_reason"),
    [(1, "duration_limit"), (10, "requested_stop")],
)
def test_synthetic_stop_signals_during_sealing_and_portal_close(
    tmp_path, max_seconds, expected_reason
):
    """Fake portal, real test-pattern encoder, repeated TERM at forced boundaries."""
    check = subprocess.run(
        ["/usr/bin/python3", "-m", "native.wayland", "probe", "--dependencies-only"],
        capture_output=True,
        timeout=30,
    )
    if check.returncode:
        pytest.skip("system GStreamer/PyGObject capture dependencies unavailable")
    script = """
import json, sys, time
from native.wayland import __main__ as cli
from native.wayland.recorder import Recorder
from native.wayland.state import Spool
class FixturePortal:
    closed = False
    def watch_interruptions(self, callback): return True
    def close(self):
        print(json.dumps({'fixture': 'portal_closing'}), flush=True)
        time.sleep(0.5)
        print(json.dumps({'fixture': 'portal_closed'}), flush=True)
def synthetic(*args, **options):
    options['synthetic_buffers'] = 1800
    return Recorder(*args, **options)
publish = Spool.publish
def slow_publish(self, value):
    result = publish(self, value)
    if value['kind'] == 'sealed_segment':
        time.sleep(0.5)
    return result
Spool.publish = slow_publish
cli.Portal = FixturePortal
cli.probe = lambda: {}
cli.authorize = lambda portal, store, session, **options: ({'stream_id': store.stream_id}, None)
cli.Recorder = synthetic
sys.argv[0] = 'memulife-wayland'
raise SystemExit(cli.main())
"""
    command = [
        "/usr/bin/python3",
        "-c",
        script,
        "run",
        "--allow-selection",
        "--spool",
        str(tmp_path / "spool"),
        "--device-id",
        str(uuid4()),
        "--stream-id",
        str(uuid4()),
        "--max-seconds",
        str(max_seconds),
        "--segment-seconds",
        "1",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ | {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent"},
    )
    deadline = threading.Timer(40, process.kill)
    deadline.start()
    values = []
    try:
        for line in process.stdout:
            value = json.loads(line)
            values.append(value)
            if (
                value.get("kind") == "sealed_segment"
                or value.get("fixture") == "portal_closing"
            ):
                process.send_signal(signal.SIGTERM)
        _, errors = process.communicate(timeout=20)
    finally:
        deadline.cancel()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    assert process.returncode == 0, errors
    summaries = [
        value
        for line in errors.splitlines()
        if (value := json.loads(line)).get("event") == "capture_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["source_frames"] >= summaries[0]["frame_seq"] > 0
    assert summaries[0]["reason"] == expected_reason
    assert any(value.get("kind") == "sealed_segment" for value in values)
    assert any(value.get("fixture") == "portal_closed" for value in values)
    assert any(
        value.get("coverage", {}).get("reason") == expected_reason for value in values
    )
