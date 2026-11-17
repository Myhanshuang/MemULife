"""Bounded PipeWire → closed-GOP H.264 segments; only sealed files are published."""

from collections import deque
from decimal import Decimal
import json
import queue
import shutil
import subprocess
import sys
import threading
import time

from . import CaptureError
from .state import encoded, private_write


def safe_gstreamer_error(message, *, stage):
    error, debug = message.parse_error()
    domain = str(error.domain)
    if domain not in {
        "gst-core-error-quark",
        "gst-resource-error-quark",
        "gst-stream-error-quark",
        "gst-library-error-quark",
    }:
        domain = "other"
    factory = message.src.get_factory() if hasattr(message.src, "get_factory") else None
    element = factory.get_name() if factory else "pipeline"
    if element not in {
        "pipewiresrc",
        "videotestsrc",
        "queue",
        "videoconvert",
        "videoscale",
        "videorate",
        "capsfilter",
        "x264enc",
        "h264parse",
        "splitmuxsink",
        "mp4mux",
        "filesink",
        "pipeline",
    }:
        element = "other"
    # Provider text may contain source titles and paths. Inspect it locally for
    # known classifications, but never return or log raw message/debug strings.
    details = (str(error.message) + " " + (debug or "")).lower()
    reason = "capture_pipeline_failed"
    for needles, stable in (
        (
            (
                "not-negotiated",
                "no supported formats",
                "no more output formats",
                "not negotiated",
            ),
            "capture_format_negotiation_failed",
        ),
        (("permission denied", "access denied"), "pipewire_access_denied"),
        (
            ("connection refused", "connection reset", "could not connect"),
            "pipewire_connection_failed",
        ),
        (("bad file descriptor", "invalid file descriptor"), "pipewire_remote_invalid"),
        (
            ("all buffers have been removed", "link to remote node was destroyed"),
            "source_disconnected",
        ),
    ):
        if any(needle in details for needle in needles):
            reason = stable
            break
    if element != "pipewiresrc":
        reason = {
            "pipewire_access_denied": "capture_resource_denied",
            "pipewire_connection_failed": "capture_resource_unavailable",
            "pipewire_remote_invalid": "capture_resource_invalid",
        }.get(reason, reason)
    return {
        "event": "gstreamer_error",
        "gstreamer_stage": stage,
        "domain": domain,
        "code": int(error.code),
        "element": element,
        "reason": reason,
    }


def dependencies():
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst, GLib

        Gst.init(None)
    except (ImportError, ValueError):
        raise CaptureError("gstreamer_python_unavailable") from None
    if Gst.version()[:2] < (1, 26):
        raise CaptureError("gstreamer_version_unsupported")
    needed = (
        "pipewiresrc",
        "videoconvert",
        "videoscale",
        "videorate",
        "capsfilter",
        "queue",
        "x264enc",
        "h264parse",
        "splitmuxsink",
        "mp4mux",
    )
    if any(Gst.ElementFactory.find(name) is None for name in needed):
        raise CaptureError("gstreamer_elements_unavailable")
    if not shutil.which("ffprobe"):
        raise CaptureError("ffprobe_unavailable")
    return Gst, GLib


def validate_video(path, width, height):
    """ffprobe show_frames actually decodes; validate count, dimensions and first IDR."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=best_effort_timestamp_time,key_frame,width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode or result.stderr or len(result.stdout) > 4 * 1024 * 1024:
            raise CaptureError("segment_decode_failed")
        frames = json.loads(result.stdout)["frames"]
        if not frames or len(frames) > 10000 or frames[0]["key_frame"] != 1:
            raise CaptureError("segment_not_independently_decodable")
        pts = []
        for frame in frames:
            if (frame["width"], frame["height"]) != (width, height):
                raise CaptureError("segment_dimensions_changed")
            value = int(Decimal(frame["best_effort_timestamp_time"]) * 1_000_000)
            if value < 0 or (pts and value <= pts[-1]):
                raise CaptureError("segment_timestamps_invalid")
            pts.append(value)
        return pts
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        raise CaptureError("segment_decode_failed") from None


class Recorder:
    def __init__(
        self,
        spool,
        stream,
        fd,
        *,
        fps=10,
        segment_seconds=30,
        longest_edge=1920,
        bitrate_kbps=2000,
        synthetic_buffers=None,
        max_seconds=None,
        diagnostic=None,
    ):
        if (
            not 1 <= fps <= 30
            or not 1 <= segment_seconds <= 60
            or not 64 <= longest_edge <= 3840
            or not 128 <= bitrate_kbps <= 8000
            or (
                max_seconds is not None
                and (type(max_seconds) is not int or not 1 <= max_seconds <= 120)
            )
        ):
            raise CaptureError("capture_settings_invalid")
        self.Gst, self.GLib = dependencies()
        self.diagnostic = diagnostic or (
            lambda value: print(json.dumps(value), file=sys.stderr, flush=True)
        )
        self.max_seconds = max_seconds
        self.spool, self.stream, self.fd = spool, stream, fd
        self.fps, self.segment_seconds, self.longest_edge = (
            fps,
            segment_seconds,
            longest_edge,
        )
        self.loop = self.GLib.MainLoop()
        self.lock = threading.Lock()
        self.frames, self.fragments = deque(), {}
        self.frame_seq = 0
        self.last_running_ns = None
        self.last_captured_ns = None
        self.clock_anchor = None
        self.source_frames = 0
        self.source_dimensions = None
        self.reconfigure_contained = False
        self.setting_output_caps = False
        self.dimensions = None
        self.segment = self.Gst.Segment.new()
        self.segment.init(self.Gst.Format.TIME)
        self.last_frame = time.monotonic_ns()
        self.stop_reason = None
        self.pipeline = self.Gst.Pipeline.new("memulife-capture")
        self.work = queue.Queue(maxsize=2)
        self.seal_errors = []
        self.pending_paths = set()
        self.closed = False
        names = [
            "videotestsrc" if synthetic_buffers else "pipewiresrc",
            "queue",
            "videoconvert",
            "videoscale",
            "videorate",
            "capsfilter",
            "x264enc",
            "h264parse",
            "splitmuxsink",
        ]
        elements = [self.Gst.ElementFactory.make(name) for name in names]
        if any(element is None for element in elements):
            raise CaptureError("gstreamer_elements_unavailable")
        self.source, buffering, _, _, rate, self.caps, encoder, parser, self.sink = (
            elements
        )
        if synthetic_buffers:
            self.source.set_property("num-buffers", synthetic_buffers)
            self.source.set_property("is-live", True)
        else:
            if not self.source.find_property(stream["target_property"]):
                raise CaptureError("pipewire_targeting_unavailable")
            self.source.set_property("fd", fd)
            self.source.set_property(stream["target_property"], stream["target"])
            self.source.set_property("resend-last", False)
            if self.source.find_property("on-disconnect"):
                self.source.set_property("on-disconnect", 2)
            if self.source.find_property("dont-reconnect"):
                self.source.set_property("dont-reconnect", True)
        buffering.set_property("max-size-buffers", 4)
        buffering.set_property("max-size-bytes", 0)
        buffering.set_property("max-size-time", 0)
        # Backpressure is bounded. Never silently discard frames in our queue.
        buffering.connect(
            "overrun", lambda *_: self.GLib.idle_add(self.stop, "capture_backpressure")
        )
        buffering.get_static_pad("sink").add_probe(
            self.Gst.PadProbeType.EVENT_UPSTREAM, self._upstream_event
        )
        rate.set_property("drop-only", True)
        # Fix the encoding policy before querying the portal source. Its native
        # geometry is learned from the first CAPS and scaled downstream only.
        self.caps.set_property(
            "caps",
            self.Gst.Caps.from_string(
                f"video/x-raw,format=I420,colorimetry=bt709,framerate={fps}/1"
            ),
        )
        encoder.set_property("bitrate", bitrate_kbps)
        encoder.set_property("key-int-max", fps)
        encoder.set_property("bframes", 0)
        encoder.set_property("tune", 4)  # zerolatency
        encoder.set_property("speed-preset", 1)  # ultrafast; bounded CPU fallback
        encoder.set_property("option-string", "open-gop=0:scenecut=0")
        parser.set_property("config-interval", -1)
        self.sink.set_property("max-size-time", segment_seconds * self.Gst.SECOND)
        self.sink.set_property("max-size-bytes", 24 * 1024 * 1024)
        self.sink.set_property("async-finalize", False)
        muxer = self.Gst.ElementFactory.make("mp4mux")
        muxer.set_property("fragment-duration", 1000)
        self.sink.set_property("muxer", muxer)
        self.sink.connect("format-location", self._location)
        for element in elements:
            self.pipeline.add(element)
        for first, second in zip(elements, elements[1:]):
            if not first.link(second):
                raise CaptureError("capture_pipeline_incompatible")
        self.source.get_static_pad("src").add_probe(
            self.Gst.PadProbeType.EVENT_DOWNSTREAM | self.Gst.PadProbeType.BUFFER,
            self._source_event,
        )
        encoder.get_static_pad("sink").add_probe(
            self.Gst.PadProbeType.BUFFER | self.Gst.PadProbeType.EVENT_DOWNSTREAM,
            self._frame,
        )
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self._message)

    def _upstream_event(self, pad, info):
        if (
            self.setting_output_caps
            and info.get_event().type == self.Gst.EventType.RECONFIGURE
        ):
            # Intercept before the source pad: GstPad marks NEED_RECONFIGURE
            # before invoking its own probe. Only our synchronous geometry
            # setter is contained; every other renegotiation/error is retained.
            if not self.reconfigure_contained:
                self.diagnostic({"event": "downstream_reconfigure_contained"})
                self.reconfigure_contained = True
            return self.Gst.PadProbeReturn.DROP
        return self.Gst.PadProbeReturn.OK

    def _source_event(self, pad, info):
        if info.type & self.Gst.PadProbeType.BUFFER:
            # videorate may mark deliberately sampled output DISCONT. Observe
            # this flag before sampling so it represents source discontinuity.
            if self.source_frames > self.fps and info.get_buffer().has_flags(
                self.Gst.BufferFlags.DISCONT
            ):
                self.GLib.idle_add(self.stop, "source_discontinuity")
                return self.Gst.PadProbeReturn.DROP
            self.source_frames += 1
            return self.Gst.PadProbeReturn.OK
        event = info.get_event()
        if event.type != self.Gst.EventType.CAPS:
            return self.Gst.PadProbeReturn.OK
        structure = event.parse_caps().get_structure(0)
        color = str(structure.get_value("colorimetry") or "unknown").lower()
        if any(transfer in color for transfer in ("pq", "hlg", "2084")):
            self.GLib.idle_add(self.stop, "hdr_source_unsupported")
            return self.Gst.PadProbeReturn.DROP
        dimensions = (structure.get_value("width"), structure.get_value("height"))
        if any(type(x) is not int or not 1 <= x <= 16384 for x in dimensions):
            self.GLib.idle_add(self.stop, "source_dimensions_invalid")
            return self.Gst.PadProbeReturn.DROP
        if self.source_dimensions:
            if dimensions != self.source_dimensions:
                self.GLib.idle_add(self.stop, "display_config_changed")
                return self.Gst.PadProbeReturn.DROP
            return self.Gst.PadProbeReturn.OK
        self.source_dimensions = dimensions
        self.diagnostic(
            {
                "event": "source_caps_received",
                "width": dimensions[0],
                "height": dimensions[1],
            }
        )
        ratio = min(1, self.longest_edge / max(dimensions))
        self.dimensions = tuple(max(2, int(x * ratio) // 2 * 2) for x in dimensions)
        width, height = self.dimensions
        self.setting_output_caps = True
        try:
            self.caps.set_property(
                "caps",
                self.Gst.Caps.from_string(
                    f"video/x-raw,format=I420,colorimetry=bt709,width={width},height={height},framerate={self.fps}/1"
                ),
            )
        finally:
            self.setting_output_caps = False
        return self.Gst.PadProbeReturn.OK

    def _timestamp(self, running):
        clock = self.pipeline.get_clock()
        if clock is None:
            raise CaptureError("capture_clock_unavailable")
        base = self.pipeline.get_base_time()
        before = time.monotonic_ns()
        clock_now = clock.get_time()
        now = time.monotonic_ns()
        uncertainty = (now - before) // 2
        offset = (before + now) // 2 - clock_now
        if self.clock_anchor is None:
            self.clock_anchor = (clock, base, offset, uncertainty)
            self.spool.clock.uncertainty_ms += uncertainty / 1_000_000
        anchor_clock, anchor_base, anchor_offset, anchor_uncertainty = self.clock_anchor
        if clock != anchor_clock or base != anchor_base:
            raise CaptureError("capture_clock_changed")
        if abs(offset - anchor_offset) > (
            2_000_000_000 + uncertainty + anchor_uncertainty
        ):
            raise CaptureError("capture_clock_changed")
        # Use one affine mapping throughout the session. Recomputing now minus
        # latency for every buffer turns scheduling jitter into timestamp drift.
        captured = anchor_offset + anchor_base + running
        if self.last_running_ns is not None and (
            running < self.last_running_ns or captured < self.last_captured_ns
        ):
            self.diagnostic(
                {
                    "event": "timestamp_regression",
                    "kind": "source_running_time"
                    if running < self.last_running_ns
                    else "clock_conversion",
                    "running_ns": str(running),
                    "previous_running_ns": str(self.last_running_ns),
                    "captured_monotonic_ns": str(captured),
                    "previous_monotonic_ns": str(self.last_captured_ns),
                    "host_monotonic_ns": str(now),
                    "pipeline_clock_ns": str(clock_now),
                    "pipeline_base_ns": str(base),
                    "anchor_offset_ns": str(anchor_offset),
                }
            )
            raise CaptureError("source_timestamp_regressed")
        # Publication drains self.frames. Keep timestamp continuity separately
        # so a regression at a segment boundary is still detected.
        self.last_running_ns, self.last_captured_ns = running, captured
        return now, captured

    def _frame(self, pad, info):
        if info.type & self.Gst.PadProbeType.EVENT_DOWNSTREAM:
            event = info.get_event()
            if event.type == self.Gst.EventType.SEGMENT:
                self.segment = event.parse_segment().copy()
            return self.Gst.PadProbeReturn.OK
        if self.stop_reason:
            return self.Gst.PadProbeReturn.DROP
        buffer = info.get_buffer()
        running = self.segment.to_running_time(self.Gst.Format.TIME, buffer.pts)
        if running == self.Gst.CLOCK_TIME_NONE:
            self.GLib.idle_add(self.stop, "source_timestamp_missing")
            return self.Gst.PadProbeReturn.DROP
        width, height = self.dimensions
        with self.lock:
            try:
                now, captured = self._timestamp(int(running))
            except CaptureError as error:
                self.GLib.idle_add(self.stop, error.code)
                return self.Gst.PadProbeReturn.DROP
            self.frame_seq += 1
            self.last_frame = now
            self.frames.append(
                {
                    "frame_id": f"{self.spool.device_id}:{self.spool.boot_id}:{self.spool.session_id}:{self.spool.stream_id}:{self.frame_seq}",
                    "capture_session_id": self.spool.session_id,
                    "stream_id": self.spool.stream_id,
                    "frame_seq": self.frame_seq,
                    "monotonic_ns": str(captured),
                    "captured_at": self.spool.clock.wall(captured),
                    "width": width,
                    "height": height,
                    "rotation": 0,
                    "scale_factor": width / self.source_dimensions[0],
                    "color_space": "bt709",
                    "pixel_format": "I420",
                    "active_window_ref": None,
                    "accessibility_snapshot_ref": None,
                    "video_chunk_ref": "pending",
                    "pts_us": 0,
                    "image_blob_ref": None,
                    "capture_quality": "scaled"
                    if self.dimensions != self.source_dimensions
                    else "native",
                    "_running": int(running),
                }
            )
            if len(self.frames) > 10000:
                self.GLib.idle_add(self.stop, "frame_index_budget_exceeded")
        return self.Gst.PadProbeReturn.OK

    def _location(self, sink, number):
        path = self.spool.part(number)
        self.fragments[str(path)] = {"number": number}
        self.pending_paths.add(path)
        private_write(
            path.with_suffix(".open.json"),
            encoded(
                self.spool.common(
                    self.spool.identity(number),
                    time.monotonic_ns(),
                    time.monotonic_ns(),
                )
                | {"kind": "open_segment", "number": number}
            ),
        )
        return str(path)

    def _message(self, bus, message):
        try:
            if message.type == self.Gst.MessageType.ELEMENT:
                structure = message.get_structure()
                if not structure or not structure.get_name().startswith(
                    "splitmuxsink-fragment-"
                ):
                    return
                path = str(structure.get_value("location"))
                fragment = self.fragments[path]
                running = int(structure.get_value("running-time"))
                if structure.get_name().endswith("opened"):
                    fragment["start"] = running
                    with self.lock:
                        first = next(
                            (
                                frame.copy()
                                for frame in self.frames
                                if frame["_running"] >= running
                            ),
                            None,
                        )
                    if first:
                        first.pop("_running")
                        first["video_chunk_ref"] = self.spool.identity(
                            fragment["number"]
                        )
                        descriptor = self.spool.common(
                            self.spool.identity(fragment["number"]),
                            int(first["monotonic_ns"]),
                            int(first["monotonic_ns"]),
                        )
                        descriptor.update(
                            kind="open_segment",
                            number=fragment["number"],
                            first_frame=first,
                            source_dimensions=self.source_dimensions,
                        )
                        private_write(
                            self.spool.part(fragment["number"]).with_suffix(
                                ".open.json"
                            ),
                            encoded(descriptor),
                        )
                else:
                    # running-time is the next fragment's first PTS on a split,
                    # but the final buffer's PTS on EOS. 1.26+ duration provides
                    # the unambiguous half-open interval for both cases.
                    boundary = fragment["start"] + int(
                        structure.get_value("fragment-duration")
                    )
                    with self.lock:
                        frames = []
                        while self.frames and self.frames[0]["_running"] < boundary:
                            frame = self.frames.popleft()
                            if frame.pop("_running") >= fragment["start"]:
                                frames.append(frame)
                    self.work.put_nowait(
                        (
                            fragment["number"],
                            frames,
                            self.dimensions,
                            self.source_dimensions,
                        )
                    )
                    del self.fragments[path]
            elif message.type == self.Gst.MessageType.ERROR:
                self._record_error(message, stage="streaming")
                self.loop.quit()
            elif message.type == self.Gst.MessageType.EOS:
                self.loop.quit()
            elif message.type == self.Gst.MessageType.QOS:
                self.stop("pipeline_qos_gap")
        except queue.Full:
            self.stop("segment_finalization_backpressure")
        except CaptureError as error:
            self.stop(error.code)
        except (KeyError, ValueError, TypeError):
            self.stop("segment_boundary_invalid")

    def _record_error(self, message, *, stage):
        diagnostic = safe_gstreamer_error(message, stage=stage)
        self.diagnostic(diagnostic)
        self.stop_reason = self.stop_reason or diagnostic["reason"]

    def _startup_error(self):
        # A synchronous state-change failure happens before the GLib signal
        # watch can dispatch ERROR. Preserve those diagnostics as well.
        seen = False
        while (
            message := self.bus.pop_filtered(self.Gst.MessageType.ERROR)
        ) is not None:
            self._record_error(message, stage="pipeline_start")
            seen = True
        if not seen:
            self.diagnostic(
                {
                    "event": "gstreamer_error",
                    "gstreamer_stage": "pipeline_start",
                    "reason": "state_change_failed_without_error",
                }
            )
        raise CaptureError(self.stop_reason or "capture_pipeline_failed")

    def _seal_worker(self):
        while True:
            item = self.work.get()
            try:
                if item is None:
                    return
                number, frames, dimensions, source_dimensions = item
                self.spool.seal(
                    number,
                    frames,
                    width=dimensions[0],
                    height=dimensions[1],
                    source_dimensions=source_dimensions,
                    dropped=[],
                    validator=validate_video,
                )
                part = self.spool.part(number)
                part.with_suffix(".open.json").unlink(missing_ok=True)
                self.pending_paths.discard(part)
            except Exception as error:
                self.seal_errors.append(
                    error.code
                    if isinstance(error, CaptureError)
                    else "segment_finalize_failed"
                )
                self.GLib.idle_add(self.stop, "segment_finalize_failed")
            finally:
                self.work.task_done()

    def stop(self, reason="requested_stop"):
        if self.stop_reason:
            return False
        self.stop_reason = reason
        self.pipeline.send_event(self.Gst.Event.new_eos())
        self.GLib.timeout_add_seconds(5, lambda: (self.loop.quit(), False)[1])
        return False

    def _watchdog(self):
        if self.closed:
            return False
        if time.monotonic_ns() - self.last_frame > 30_000_000_000:
            self.stop("source_stalled")
        elif (
            abs(
                time.time_ns()
                - (
                    self.spool.clock.wall_ns
                    + time.monotonic_ns()
                    - self.spool.clock.monotonic_ns
                )
            )
            > 2_000_000_000
        ):
            self.stop("clock_mapping_changed")
        elif shutil.disk_usage(self.spool.root).free < 128 * 1024 * 1024:
            self.stop("spool_storage_blocked")
        elif any(
            path.exists() and path.stat().st_size > 32 * 1024 * 1024
            for path in tuple(self.pending_paths)
        ):
            self.stop("segment_size_limit")
        return True

    def run(self):
        worker = threading.Thread(target=self._seal_worker, daemon=True)
        worker.start()
        watcher = self.GLib.timeout_add_seconds(1, self._watchdog)
        duration_timer = None
        try:
            if (
                self.pipeline.set_state(self.Gst.State.PLAYING)
                == self.Gst.StateChangeReturn.FAILURE
            ):
                self._startup_error()
            if self.max_seconds is not None:
                duration_timer = self.GLib.timeout_add(
                    self.max_seconds * 1000, self.stop, "duration_limit"
                )
            self.loop.run()
        except CaptureError as error:
            # Startup can fail before the loop dispatches any messages. Finish
            # cleanup, then persist the same coverage gap used by async errors.
            self.stop_reason = self.stop_reason or error.code
        finally:
            self.closed = True
            self.pipeline.set_state(self.Gst.State.NULL)
            self.bus.remove_signal_watch()
            for source in [watcher, duration_timer]:
                if (
                    source is not None
                    and self.GLib.MainContext.default().find_source_by_id(source)
                ):
                    self.GLib.source_remove(source)
            self.work.put(None)
            worker.join(timeout=90)
        reason = self.seal_errors[0] if self.seal_errors else self.stop_reason
        if worker.is_alive():
            reason = "segment_finalize_timeout"
        self.diagnostic(
            {
                "event": "capture_summary",
                "source_frames": self.source_frames,
                "frame_seq": self.frame_seq,
                "reason": reason or "source_eos",
            }
        )
        if reason == "segment_finalize_timeout":
            raise CaptureError(reason)
        if reason or self.pending_paths:
            self.spool.gap(
                reason or "unsealed_segment",
                self.last_frame,
                notes=["unsealed_parts_preserved"] if self.pending_paths else [],
            )
        if reason not in (None, "requested_stop", "duration_limit"):
            raise CaptureError(reason)
