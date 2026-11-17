"""Canonical selected text messages. Unselected response frames never cross this boundary."""

import hashlib
import json
from datetime import datetime, timezone

from ..adapters.base import AdapterError

FIELDS = {"id", "role", "text", "occurred_at"}
ROLES = {"user", "assistant", "system", "tool", "unknown"}
FORMAT = "memulife.browser-messages.v1"


def wire(value):
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    except (ValueError, UnicodeError, TypeError):
        raise AdapterError("import_message_invalid") from None


def clean_message(value, *, digest=False):
    if not isinstance(value, dict) or set(value) != FIELDS | (
        {"digest"} if digest else set()
    ):
        raise AdapterError("import_message_invalid")
    identity, role, text, stamp = (
        value[k] for k in ("id", "role", "text", "occurred_at")
    )
    if (
        not isinstance(identity, str)
        or not 1 <= len(identity) <= 512
        or any(
            ord(c) < 32 or ord(c) == 127 or "\ud800" <= c <= "\udfff" for c in identity
        )
        or not isinstance(role, str)
        or role not in ROLES
        or not isinstance(text, str)
        or not text
        or len(text) > 900 * 1024
    ):
        raise AdapterError("import_message_invalid")
    if stamp is not None:
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if (
                parsed.tzinfo is None
                or parsed.astimezone(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
                != stamp
            ):
                raise ValueError()
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise AdapterError("import_message_invalid") from None
    result = {"id": identity, "role": role, "text": text, "occurred_at": stamp}
    checksum = hashlib.sha256(wire([identity, role, text, stamp])).hexdigest()
    if digest and value["digest"] != checksum:
        raise AdapterError("import_message_changed")
    return result | {"digest": checksum}


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError("import_message_invalid")
        result[key] = value
    return result


def restrict_page(page, selection, provider):
    """Validate every row before Host sees any bytes; preserve whole-conversation jobs."""
    if selection.messages:
        cursor = page.cursor
        revisions = {record.object_id: record.revision for record in page.records}
        if (
            not isinstance(cursor, dict)
            or set(cursor)
            != {"conversation_id", "native_revision", "scope_complete", "observed_at"}
            or not isinstance(cursor["conversation_id"], str)
            or cursor["conversation_id"] not in revisions
            or cursor["native_revision"] != revisions[cursor["conversation_id"]]
            or cursor["scope_complete"] is not False
        ):
            raise AdapterError("import_message_scope_denied")
        clean_message(
            {
                "id": "cursor",
                "role": "unknown",
                "text": "cursor",
                "occurred_at": cursor["observed_at"],
            }
        )
        if cursor["observed_at"] is None:
            raise AdapterError("import_message_scope_denied")
    for record in page.records:
        if (provider == 'chatgpt' and selection.include_attachments and not selection.messages
                and record.metadata.get('attachment_coverage') != 'native_references'):
            raise AdapterError('connector_upgrade_required')
        if not selection.include_attachments:
            if any(item.data is not None for item in record.attachments):
                raise AdapterError('import_attachment_scope_denied')
            for item in record.attachments:
                item.status = 'excluded'
        chosen = selection.messages.get(record.object_id)
        if chosen is None:
            continue
        try:
            value = json.loads(record.raw, object_pairs_hook=strict_object)
        except (ValueError, UnicodeError):
            raise AdapterError("import_message_invalid") from None
        if (
            not isinstance(value, dict)
            or set(value)
            != {"format", "provider", "native_conversation_id", "messages"}
            or value["format"] != FORMAT
            or value["provider"] != provider
            or value["native_conversation_id"] != record.object_id
            or not isinstance(value["messages"], list)
            or not 1 <= len(value["messages"]) <= 500
        ):
            raise AdapterError("import_message_scope_denied")
        messages = [clean_message(row) for row in value["messages"]]
        if (
            len({m["id"] for m in messages}) != len(messages)
            or {m["id"]: m["digest"] for m in messages} != chosen
        ):
            raise AdapterError("import_message_changed")
        expected_metadata = {
            "native_conversation_id": record.object_id,
            "selection": "messages",
            "message_ids": [m["id"] for m in messages],
            "attachments": "excluded",
            "actor": {"role": "unknown", "source_actor_id": None, "display_name": None},
        }
        if (
            record.metadata != expected_metadata
            or record.text != "\n\n".join(m["text"] for m in messages)
            or record.revision is not None
            or record.occurred_at is not None
            or record.deleted
            or record.attachments
        ):
            raise AdapterError("import_message_scope_denied")
        # A subset must not replace an already imported complete conversation.
        identity = hashlib.sha256(wire([record.object_id, sorted(chosen)])).hexdigest()
        record.object_id = "selected-messages:" + identity
        record.raw = wire(value)
    return page
