"""Chromium native messaging transport to the local acquisition service."""

import json
from pathlib import Path
import struct
import sys
from uuid import UUID

from .base import AdapterError, Page, Snapshot
from ...persistence import encoded
from types import SimpleNamespace

MAX_MESSAGE = 1024 * 1024
MAX_CAPTURE = 16 * 1024 * 1024
CHUNK_BYTES = 192 * 1024
MAX_TRANSFERS_BYTES = 64 * 1024 * 1024


def capture_page(config, params):
    from ..attachments import decode_attachments
    if len(encoded(params)) > MAX_CAPTURE:
        raise AdapterError("capture_too_large")
    for key in ("provider", "account_id", "workspace_id"):
        if params.get(key) != config["config"].get(key) or not isinstance(
            params.get(key), str
        ):
            raise AdapterError("source_binding_mismatch")
    UUID(params["batch_id"])
    if not isinstance(params.get("records"), list) or len(params["records"]) > 100:
        raise AdapterError("source_page_invalid")
    records = []
    for row in params["records"]:
        if not isinstance(row, dict) or not isinstance(
            row.get("raw"), (str, dict, list)
        ):
            raise AdapterError("source_record_invalid")
        if row.get("text") is not None and not isinstance(row["text"], str):
            raise AdapterError("source_record_invalid")
        if (
            not isinstance(row.get("metadata", {}), dict)
            or type(row.get("deleted", False)) is not bool
        ):
            raise AdapterError("source_record_invalid")
        raw = (
            row["raw"].encode() if isinstance(row["raw"], str) else encoded(row["raw"])
        )
        records.append(
            Snapshot(
                object_id=row["object_id"],
                raw=raw,
                text=row.get("text"),
                revision=row.get("revision"),
                deleted=row.get("deleted", False),
                metadata=row.get("metadata", {}),
                occurred_at=row.get("occurred_at"),
                attachments=decode_attachments(row.get('attachments', [])),
            )
        )
    page = Page(
        records,
        params.get("cursor", {}),
        scope_complete=False,
        warnings=(["attachment_coverage_unverified"] if any(
            r.metadata.get('attachments') != 'excluded' and
            r.metadata.get('attachment_coverage') != 'native_references' for r in records) else []),
    )
    return page


class NativeBridge:
    """Transport only. The center checks selection before durable Host acceptance."""

    def __init__(self, host):
        self.host = host
        self.binding = host.config["config"]
        if (
            host.config["adapter_id"] != "browser.ai-history"
            or self.binding.get("provider") != host.config["source_profile"]
        ):
            raise AdapterError("source_binding_mismatch")

    def dispatch(self, request: dict) -> dict:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            raise AdapterError("protocol_invalid")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise AdapterError("protocol_invalid")
        for key in ("provider", "account_id", "workspace_id"):
            if params.get(key) != self.binding.get(key):
                raise AdapterError("source_binding_mismatch")
        routes = {
            "capture": "capture",
            "capture.chunk": "chunks",
            "capture.commit": "commit",
            "catalog.offer": "catalog",
            "selection.get": "selection",
            "preview.offer": "preview",
            "progress.offer": "progress",
        }
        endpoint = routes.get(request.get("method"))
        if endpoint is None:
            raise AdapterError("method_not_found")
        return self.forward(endpoint, params)

    def forward(self, endpoint, params):
        import httpx
        from ...environment import load_environment
        from ...http_transport import load_tokens

        config = self.host.config
        environment = load_environment()
        token = load_tokens(environment).get(config["source_identity"])
        if not token or not config.get("center_url"):
            raise AdapterError("import_selection_required")
        try:
            with httpx.Client(
                base_url=config["center_url"],
                headers={"Authorization": "Bearer " + token},
                timeout=60,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                path = (
                    "v1/source-imports/" + config["source_instance_id"] + "/" + endpoint
                )
                response = (
                    client.get(
                        path,
                        params={"claim_previews": "true"}
                        if params.get("claim_previews") is True
                        else {},
                    )
                    if endpoint == "selection"
                    else client.post(path, json=params)
                )
                if not response.is_success:
                    raise AdapterError(
                        response.json().get("code", "import_exchange_failed")
                    )
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise AdapterError("import_exchange_unavailable", True) from None

    def capture(self, params):
        return self.forward("capture", params)

    def chunk(self, params):
        return self.forward("chunks", params)

    def commit(self, params):
        return self.forward("commit", params)


class NativeRouter:
    """One extension host routes only to explicitly registered source bindings."""

    def __init__(self, config: dict):
        routes = config.get("routes")
        if not isinstance(routes, list) or not 1 <= len(routes) <= 100:
            raise AdapterError("native_routes_invalid")
        self.bridges = {}
        for path in routes:
            source = json.loads(Path(path).read_bytes())
            if not source.get("enabled", False):
                continue
            bridge = NativeBridge(SimpleNamespace(config=source))
            identity = tuple(
                bridge.binding.get(key)
                for key in ("provider", "account_id", "workspace_id")
            )
            if identity in self.bridges:
                raise AdapterError("duplicate_browser_binding")
            self.bridges[identity] = bridge

    def dispatch(self, request: dict) -> dict:
        if not isinstance(request, dict) or not isinstance(request.get("params"), dict):
            raise AdapterError("protocol_invalid")
        params = request["params"]
        identity = tuple(
            params.get(key) for key in ("provider", "account_id", "workspace_id")
        )
        if not all(isinstance(value, str) for value in identity):
            raise AdapterError("source_binding_mismatch")
        bridge = self.bridges.get(identity)
        if bridge is None:
            raise AdapterError("source_binding_mismatch")
        return bridge.dispatch(request)


def exact_read(stream, size: int) -> bytes:
    parts = []
    remaining = size
    while remaining:
        part = stream.read(remaining)
        if not part:
            break
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


def serve(config: dict, incoming=None, outgoing=None, origin: str | None = None) -> int:
    expected = config.get("extension_origin")
    if not expected or origin != expected:
        raise AdapterError("extension_origin_denied")
    incoming = incoming or sys.stdin.buffer
    outgoing = outgoing or sys.stdout.buffer
    bridge = NativeRouter(config) if "routes" in config else NativeBridge(SimpleNamespace(config=config))
    while True:
        header = exact_read(incoming, 4)
        if not header:
            return 0
        if len(header) != 4:
            return 1
        length = struct.unpack("=I", header)[0]
        if not 0 < length <= MAX_MESSAGE:
            return 1
        raw = exact_read(incoming, length)
        if len(raw) != length:
            return 1
        request = {}
        try:
            request = json.loads(raw)
            result = bridge.dispatch(request)
            response = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
        except (AdapterError, ValueError, KeyError, TypeError, OSError):
            error = sys.exception()
            code = (
                error.code
                if isinstance(error, AdapterError)
                else "native_request_invalid"
            )
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id") if isinstance(request, dict) else None,
                "error": {
                    "code": -32000,
                    "message": code,
                    "data": {
                        "code": code,
                        "retryable": getattr(error, "retryable", False),
                    },
                },
            }
        wire = encoded(response)
        outgoing.write(struct.pack("=I", len(wire)) + wire)
        outgoing.flush()
