"""ACK-gated JSON-RPC stream between a source reader and the durable Rust host."""

from __future__ import annotations

import json
import os
import resource
import selectors
import subprocess
import sys
import threading
import time
from uuid import uuid4

from .base import AdapterError
from .reader import MAX_WIRE, decode_page, encode_page
from .registry import create


def frame(value: dict) -> bytes:
    raw = (
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        + b"\n"
    )
    if len(raw) > MAX_WIRE:
        raise AdapterError("reader_frame_too_large")
    return raw


def parse(raw: bytes) -> dict:
    if not raw or not raw.endswith(b"\n") or len(raw) > MAX_WIRE:
        raise AdapterError("reader_protocol_invalid")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise AdapterError("reader_protocol_invalid") from None
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise AdapterError("reader_protocol_invalid")
    return value


class SubscriptionReader:
    def __init__(
        self, adapter_id: str, profile: str, config: dict, timeout: float = 120
    ):
        self.adapter_id = adapter_id
        self.profile = profile
        self.config = config
        self.timeout = timeout
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._closed = threading.Event()

    def _start(self):
        with self._lock:
            if self._closed.is_set():
                raise AdapterError("reader_closed")
            if self._process is not None:
                raise AdapterError("reader_already_running")
            self._process = subprocess.Popen(
                [sys.executable, "-m", "memu.acquisition.adapters.subscription"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            return self._process

    def close(self):
        """Interrupt an idle socket reader and reap it; safe from another thread."""
        self._closed.set()
        with self._lock:
            process = self._process
            self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        for pipe in (process.stdin, process.stdout):
            if pipe:
                pipe.close()

    def consume(self, host, stop_event: threading.Event):
        _, checkpoint = host.checkpoint("live", None, "subscription")
        identity = str(uuid4())
        process = self._start()
        request = {
            "jsonrpc": "2.0",
            "id": identity,
            "method": "subscribe",
            "adapter_id": self.adapter_id,
            "profile": self.profile,
            "config": self.config,
            "params": {"cursor": checkpoint.get("reader")},
        }
        try:
            process.stdin.write(frame(request))
            process.stdin.flush()
            expected = 0
            for response in self._responses(process, stop_event):
                if "error" in response:
                    error = response["error"].get("data", {})
                    raise AdapterError(
                        error.get("code", "reader_failed"),
                        error.get("retryable", False),
                    )
                if (
                    response.get("method") != "candidate"
                    or response.get("id") != f"{identity}:{expected}"
                ):
                    raise AdapterError("reader_protocol_invalid")
                try:
                    page = decode_page(response["params"]["page"])
                except (ValueError, KeyError, TypeError):
                    raise AdapterError("reader_protocol_invalid") from None
                # Never acknowledge source data before durable blob/event/checkpoint
                # commit. A failed commit closes the child with no source progress.
                host.accept(page, partition="subscription")
                process.stdin.write(
                    frame(
                        {
                            "jsonrpc": "2.0",
                            "id": response["id"],
                            "result": {"durable_level": "edge_durable"},
                        }
                    )
                )
                process.stdin.flush()
                expected += 1
        except AdapterError:
            if not stop_event.is_set() and not self._closed.is_set():
                raise
        except (BrokenPipeError, OSError, ValueError):
            if not stop_event.is_set() and not self._closed.is_set():
                raise AdapterError(
                    "subscription_reader_disconnected", retryable=True
                ) from None
        finally:
            self.close()

    def _responses(self, process, stop_event):
        buffer = bytearray()
        deadline = time.monotonic() + self.timeout
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not stop_event.is_set() and not self._closed.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdapterError("reader_timeout", retryable=True)
                # Readiness is event driven; the bounded wait also permits prompt
                # stop_event cancellation when a healthy source has no messages.
                if not selector.select(min(1, remaining)):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise AdapterError(
                        "subscription_reader_disconnected", retryable=True
                    )
                buffer.extend(chunk)
                if len(buffer) > MAX_WIRE and b"\n" not in buffer:
                    raise AdapterError("reader_frame_too_large")
                while b"\n" in buffer:
                    boundary = buffer.index(b"\n") + 1
                    raw = bytes(buffer[:boundary])
                    del buffer[:boundary]
                    yield parse(raw)
                    deadline = time.monotonic() + self.timeout


def main() -> int:
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    identity = None
    adapter = None
    try:
        request = parse(sys.stdin.buffer.readline(MAX_WIRE + 1))
        identity = request.get("id")
        if request.get("method") != "subscribe" or not isinstance(identity, str):
            raise AdapterError("method_not_found")
        adapter = create(request["adapter_id"], request["profile"], request["config"])
        subscribe = getattr(adapter, "subscribe", None)
        if subscribe is None:
            raise AdapterError("capability_unavailable")
        for sequence, page in enumerate(
            subscribe(request.get("params", {}).get("cursor"))
        ):
            page_id = f"{identity}:{sequence}"
            sys.stdout.buffer.write(
                frame(
                    {
                        "jsonrpc": "2.0",
                        "id": page_id,
                        "method": "candidate",
                        "params": {"page": encode_page(page)},
                    }
                )
            )
            sys.stdout.buffer.flush()
            # Blocking here is deliberate: generator state and the next source
            # read cannot advance until the host confirms durable custody.
            ack = parse(sys.stdin.buffer.readline(MAX_WIRE + 1))
            if ack.get("id") != page_id or ack.get("result") != {
                "durable_level": "edge_durable"
            }:
                raise AdapterError("durable_ack_required")
        raise AdapterError("subscription_disconnected", retryable=True)
    except Exception as error:
        code = error.code if isinstance(error, AdapterError) else "reader_incompatible"
        try:
            sys.stdout.buffer.write(
                frame(
                    {
                        "jsonrpc": "2.0",
                        "id": identity,
                        "error": {
                            "code": -32000,
                            "message": code,
                            "data": {
                                "code": code,
                                "retryable": getattr(error, "retryable", False),
                            },
                        },
                    }
                )
            )
            sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError):
            pass
        return 1
    finally:
        if adapter is not None and callable(getattr(adapter, "close", None)):
            adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
