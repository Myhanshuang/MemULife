"""Manual start only. `probe` never requests a portal session or records pixels."""

import argparse
import json
import os
from pathlib import Path
import signal
import sys
from uuid import UUID, uuid4

from . import CaptureError
from .portal import Portal, authorize
from .recorder import Recorder, dependencies
from .recovery import recover_pending
from .state import Spool, TokenStore, private_dir


def emit_hint(line, *, fd=1):
    """Best-effort atomic pipe hint; durable manifests are the source of truth."""
    try:
        data = (line + "\n").encode()
        if len(data) > os.fpathconf(fd, "PC_PIPE_BUF"):
            return
        os.set_blocking(fd, False)
        os.write(fd, data)
    except (OSError, ValueError):
        pass


def duration_limit(value):
    try:
        seconds = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 120") from None
    if not 1 <= seconds <= 120:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 120")
    return seconds


def probe(*, dependencies_only=False):
    Gst, _ = dependencies()
    result = {
        "backend": "wayland-portal",
        "status": "dependencies_ready",
        "gstreamer": Gst.version_string(),
        "encoder": "x264-software",
        "device_verified": False,
        "permission_requested": False,
    }
    if dependencies_only:
        return result
    if (
        sys.platform != "linux"
        or os.environ.get("XDG_SESSION_TYPE") != "wayland"
        or not os.environ.get("WAYLAND_DISPLAY")
    ):
        raise CaptureError("wayland_session_required")
    portal = Portal()
    try:
        if int(portal.properties.get("version", 0)) < 2:
            raise CaptureError("portal_version_unsupported")
        return result | {
            "status": "needs_source_selection",
            "portal_version": int(portal.properties["version"]),
            "source_types": int(portal.properties.get("AvailableSourceTypes", 0)),
            "cursor_modes": int(portal.properties.get("AvailableCursorModes", 0)),
        }
    finally:
        portal.close()


def main():
    parser = argparse.ArgumentParser(description="User-started Wayland capture helper")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("probe")
    check.add_argument("--dependencies-only", action="store_true")
    for operation in ("run", "self-test", "recover"):
        command = commands.add_parser(operation)
        command.add_argument("--spool", type=Path, required=True)
        command.add_argument("--device-id", required=True)
        command.add_argument("--stream-id", required=True)
        command.add_argument("--fps", type=int, default=10)
        command.add_argument(
            "--segment-seconds", type=int, default=30 if operation == "run" else 2
        )
        command.add_argument("--longest-edge", type=int, default=1920)
        command.add_argument("--bitrate-kbps", type=int, default=2000)
        if operation != "recover":
            command.add_argument(
                "--max-seconds",
                type=duration_limit,
                help="Stop capture internally after 1..120 seconds; no limit by default",
            )
        if operation == "run":
            command.add_argument(
                "--allow-selection",
                action="store_true",
                help="Allow one system source-selection interaction",
            )
            command.add_argument(
                "--reselect",
                action="store_true",
                help="Discard consumed/invalid selection and explicitly select again",
            )
            command.add_argument(
                "--source", choices=("monitor", "window"), default="monitor"
            )
            command.add_argument("--hide-cursor", action="store_true")
        elif operation == "self-test":
            command.add_argument(
                "--buffers",
                type=int,
                default=75,
                help="Synthetic test-pattern buffers; no portal or screen access",
            )
    args = parser.parse_args()
    try:
        if args.command == "probe":
            print(json.dumps(probe(dependencies_only=args.dependencies_only)))
            return 0
        if args.command == "run" and not args.allow_selection:
            raise CaptureError("needs_source_selection")
        UUID(args.device_id)
        UUID(args.stream_id)
        if args.command == "self-test" and not 1 <= args.buffers <= 1800:
            raise CaptureError("test_buffer_limit")
        os.umask(0o077)
        root = private_dir(args.spool)
        if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise CaptureError("private_spool_required")
        store = TokenStore(root / "private", args.device_id, args.stream_id)
        with store.lock():
            recover_pending(
                root,
                args.device_id,
                args.stream_id,
                emit=emit_hint,
            )
            if args.command == "recover":
                return 0
            session = str(uuid4())
            portal, fd = None, None
            try:
                if args.command == "run":
                    probe()
                    portal = Portal()
                    stream, fd = authorize(
                        portal,
                        store,
                        session,
                        allow_selection=True,
                        reselect=args.reselect,
                        source_type=1 if args.source == "monitor" else 2,
                        cursor_mode=1 if args.hide_cursor else 2,
                    )
                else:
                    stream = {"stream_id": args.stream_id}
                spool = Spool(
                    root,
                    args.device_id,
                    session,
                    stream["stream_id"],
                    emit=emit_hint,
                )
                recorder = Recorder(
                    spool,
                    stream,
                    fd,
                    fps=args.fps,
                    segment_seconds=args.segment_seconds,
                    longest_edge=args.longest_edge,
                    bitrate_kbps=args.bitrate_kbps,
                    synthetic_buffers=args.buffers
                    if args.command == "self-test"
                    else None,
                    max_seconds=args.max_seconds,
                )
                if portal:
                    portal.closed_callback = recorder.stop
                    if not portal.watch_interruptions(recorder.stop):
                        spool.gap(
                            "session_state_unavailable",
                            notes=["lock_and_suspend_signals_unavailable"],
                        )
                    if portal.closed:
                        raise CaptureError("portal_session_closed")

                def request_stop(signum, frame):
                    # The CLI owns process signals through sealing and portal
                    # Close, including after the GLib loop has stopped. Keep
                    # this handler until process exit: a late/repeated TERM
                    # must not kill a successfully sealed capture's cleanup.
                    if not recorder.closed:
                        recorder.GLib.idle_add(recorder.stop)

                for sig in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(sig, request_stop)
                recorder.run()
            finally:
                if fd is not None:
                    os.close(fd)
                if portal:
                    portal.close()
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error_code": error.code
                    if isinstance(error, CaptureError)
                    else "capture_unavailable",
                }
                | (
                    {"portal_stage": error.stage}
                    if isinstance(error, CaptureError) and error.stage
                    else {}
                )
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
