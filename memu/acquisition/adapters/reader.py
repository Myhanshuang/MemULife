"""Bounded JSON requests to isolated readers, with optional framed process reuse.

Reader failure leaves the host cursor untouched. stdout contains protocol only;
third-party diagnostics and exceptions never become source data or credentials.
"""

import base64
from dataclasses import asdict
import json
import math
import os
import resource
import selectors
import struct
import subprocess
import sys
import time
from uuid import uuid4

from .base import AdapterError, Attachment, Page, Snapshot, validate_page
from .registry import create

MAX_WIRE = 96 * 1024 * 1024


def encode_page(page: Page) -> dict:
    validate_page(page)
    value = asdict(page)
    for record in value["records"]:
        record["raw"] = base64.b64encode(record["raw"]).decode("ascii")
        for attachment in record["attachments"]:
            data = attachment["data"]
            attachment["data"] = (
                base64.b64encode(data).decode("ascii") if data is not None else None
            )
    return value


def decode_page(value: dict) -> Page:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("records"), list)
        or len(value["records"]) > 100
    ):
        raise AdapterError("reader_protocol_invalid")
    records = []
    for record in value["records"]:
        row = record.copy()
        row["raw"] = base64.b64decode(row["raw"], validate=True)
        attachments = []
        for attachment in row.pop("attachments"):
            item = attachment.copy()
            if item["data"] is not None:
                item["data"] = base64.b64decode(item["data"], validate=True)
            attachments.append(Attachment(**item))
        records.append(Snapshot(**row, attachments=attachments))
    page = Page(
        records=records,
        **{
            key: value[key]
            for key in ("cursor", "has_more", "scope_complete", "warnings")
        },
    )
    validate_page(page)
    return page


class IsolatedReader:
    def __init__(
        self,
        adapter_id: str,
        profile: str,
        config: dict,
        timeout: float = 90,
        selection: dict | None = None,
        persistent: bool = False,
    ):
        self.persistent, self.process, self.calls = persistent, None, 0
        self.selection = selection
        self.adapter_id, self.profile, self.config, self.timeout = (
            adapter_id,
            profile,
            config,
            timeout,
        )

    def call(self, method: str, params: dict | None = None) -> dict:
        identity = str(uuid4())
        request = {
            "jsonrpc": "2.0",
            "id": identity,
            "method": method,
            "params": params or {},
            "adapter_id": self.adapter_id,
            "profile": self.profile,
            "config": self.config,
        }
        try:
            environment = None
            if self.adapter_id == 'qq.napcat' and self.profile == 'napcat-webui':
                from .napcat_webui import reader_environment
                environment = reader_environment(self.config)
            if self.persistent:
                output = self.exchange(json.dumps(request).encode())
            else:
                child = subprocess.run(
                [sys.executable, "-m", "memu.acquisition.adapters.reader"],
                input=json.dumps(request).encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                timeout=self.timeout,
                check=False,
                )
                if child.returncode:
                    raise AdapterError('reader_failed', True)
                output = child.stdout
        except subprocess.TimeoutExpired:
            raise AdapterError("reader_timeout", True) from None
        if len(output) > MAX_WIRE:
            raise AdapterError("reader_failed", True)
        try:
            response = json.loads(output)
            if not isinstance(response, dict):
                raise ValueError
            if response.get("jsonrpc") != "2.0" or response.get("id") != identity:
                raise ValueError
            if "error" in response:
                data = response["error"]["data"]
                raise AdapterError(data["code"], data.get("retryable", False))
            return response["result"]
        except (KeyError, ValueError, TypeError):
            raise AdapterError("reader_protocol_invalid") from None

    def exchange(self, payload):
        """One timed framed request; cursor ownership stays in the parent outbox."""
        if len(payload) > MAX_WIRE:
            raise AdapterError('reader_request_too_large')
        if self.calls >= 256:
            self.close()
        if self.process is None:
            self.process = subprocess.Popen([sys.executable, '-m', 'memu.acquisition.adapters.reader', '--stream'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            self.calls = 0
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
        process = self.process
        outgoing = memoryview(struct.pack('!I', len(payload)) + payload)
        received, expected = bytearray(), None
        deadline = time.monotonic() + self.timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdin, selectors.EVENT_WRITE)
                selector.register(process.stdout, selectors.EVENT_READ)
                while expected is None or len(received) < expected + 4:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AdapterError('reader_timeout', True)
                    for key, _ in selector.select(remaining):
                        if key.fileobj is process.stdin:
                            sent = os.write(key.fd, outgoing[:65536])
                            outgoing = outgoing[sent:]
                            if not outgoing:
                                selector.unregister(process.stdin)
                        else:
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                raise AdapterError('reader_failed', True)
                            received.extend(chunk)
                            if expected is None and len(received) >= 4:
                                expected = struct.unpack('!I', received[:4])[0]
                                if expected > MAX_WIRE:
                                    raise AdapterError('reader_protocol_invalid')
                if len(received) != expected + 4:
                    raise AdapterError('reader_protocol_invalid')
            self.calls += 1
            return bytes(received[4:])
        except Exception as error:
            self.close()
            if isinstance(error, AdapterError):
                raise
            raise AdapterError('reader_failed', True) from None

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            process.stdin.close()
            process.stdout.close()

    def probe(self) -> dict:
        return self.call("probe")

    def health(self) -> dict:
        return self.call("health")

    def read(self, mode: str, cursor: dict | None = None, limit: int = 50) -> Page:
        return decode_page(
            self.call(
                "read",
                {
                    "mode": "discover" if mode == "discovery" else mode,
                    "cursor": cursor,
                    "limit": limit,
                    **({"selection": self.selection} if self.selection else {}),
                },
            )
        )


def handle(wire, session):
    request = {}
    try:
        if len(wire) > MAX_WIRE:
            raise AdapterError("reader_request_too_large")
        request = json.loads(wire)
        if not isinstance(request, dict):
            request = {}
            raise AdapterError("protocol_invalid")
        if request.get("jsonrpc") != "2.0" or request.get("method") not in {
            "probe",
            "health",
            "read",
        }:
            raise AdapterError("method_not_found")
        binding = json.dumps([request['adapter_id'], request['profile'], request['config'],
                              request.get('params', {}).get('selection')], sort_keys=True)
        if session and session['binding'] != binding:
            raise AdapterError('source_binding_mismatch')
        fresh = not session
        if fresh:
            session.update(binding=binding, adapter=create(request['adapter_id'], request['profile'], request['config']))
        adapter = session['adapter']
        if request["method"] == "read":
            params = dict(request["params"])
            if "selection" in params:
                from ..sources.selection import Selection
                from ..sources.reader import prepare_reader

                selection = Selection.model_validate(params.pop('selection'))
                if fresh:
                    prepare_reader(adapter, selection)
            result = encode_page(adapter.read(**params))
        else:
            result = getattr(adapter, request["method"])()
        response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
    except Exception as error:
        code = error.code if isinstance(error, AdapterError) else "reader_incompatible"
        response = {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "error": {
                "code": -32000,
                "message": code,
                "data": {"code": code, "retryable": getattr(error, "retryable", False)},
            },
        }
    return json.dumps(response, ensure_ascii=False, allow_nan=False).encode()


def main() -> int:
    # Persistent readers retain a bounded decode cache, never a committed cursor.
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    streaming = '--stream' in sys.argv[1:]
    session = {}
    while True:
        if streaming:
            header = sys.stdin.buffer.read(4)
            if not header:
                return 0
            if len(header) != 4:
                return 1
            length = struct.unpack('!I', header)[0]
            if length > MAX_WIRE:
                return 1
            wire = sys.stdin.buffer.read(length)
            if len(wire) != length:
                return 1
        else:
            wire = sys.stdin.buffer.read(MAX_WIRE + 1)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        hard = resource.getrlimit(resource.RLIMIT_CPU)[1]
        limit = math.ceil(usage.ru_utime + usage.ru_stime) + 90
        resource.setrlimit(resource.RLIMIT_CPU, (limit if hard == resource.RLIM_INFINITY else min(limit, hard), hard))
        output = handle(wire, session)
        if len(output) > MAX_WIRE:
            return 1
        sys.stdout.buffer.write((struct.pack('!I', len(output)) if streaming else b'') + output)
        sys.stdout.buffer.flush()
        if not streaming:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
