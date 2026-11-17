"""Project validated capture records into their original canonical event types."""

import json
from uuid import UUID

from .base import AdapterError, Snapshot


def project(event: dict, record: Snapshot, put) -> dict:
    metadata = record.metadata
    session = metadata.get("capture_session_id")
    try:
        UUID(session)
    except (ValueError, TypeError, AttributeError):
        raise AdapterError("screen_manifest_scope_mismatch") from None
    if metadata.get("stream_id") != event["stream_id"]:
        raise AdapterError("screen_manifest_scope_mismatch")
    kind = metadata.get("native_event_type")
    if kind not in {"screen.segment", "source.coverage"}:
        raise AdapterError("screen_event_type_invalid")
    if kind == "screen.segment":
        from .screen import _payload_validator

        payload = metadata.get("screen_payload", {})
        if not _payload_validator("VideoChunk").is_valid(payload) or (
            payload["device_id"] != event["device_id"]
            or payload["stream_id"] != event["stream_id"]
            or payload["capture_session_id"] != session
        ):
            raise AdapterError("screen_segment_invalid")
        references = {
            a["attachment_id"]: a["blob_ref"]
            for a in event["attachments"]
            if a["status"] == "available"
        }
        if payload["blob_ref"] != references.get("source:video") or payload[
            "manifest_ref"
        ] != references.get("raw"):
            raise AdapterError("screen_payload_evidence_mismatch")
        reference = put(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            "application/json",
        )
    else:
        try:
            gap = json.loads(record.raw)
            valid = (
                gap["format"] == "memulife.screen-segment.v1"
                and gap["kind"] == "capture_gap"
                and gap["device_id"] == event["device_id"]
                and gap["stream_id"] == event["stream_id"]
                and gap["session_id"] == session
                and gap["coverage"]["status"] == "gap"
                and isinstance(gap["coverage"]["reason"], str)
                and bool(gap["coverage"]["reason"])
            )
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            raise AdapterError("screen_manifest_scope_mismatch")
        reference = put(record.raw, record.media_type)
    event["attachments"].append(
        {
            "attachment_id": "host:snapshot",
            "blob_ref": event["payload_ref"],
            "status": "available",
            "source_ref": None,
            "retention_class": "durable",
            "expires_at": None,
        }
    )
    event.update(event_type=kind, payload_ref=reference)
    event["sequence"]["epoch"] = session
    event["origin"]["method"] = "screen_capture"
    if metadata.get("screen_coverage", {}).get("status") in {"gap", "partial"}:
        event["coverage"]["completeness"] = "partial"
    return event
