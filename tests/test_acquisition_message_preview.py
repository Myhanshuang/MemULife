"""Canonical message boundaries and bounded previews without any persistence."""

from copy import deepcopy
import hashlib
import json
import time

import pytest
from pydantic import ValidationError

from memu.acquisition.adapters.base import AdapterError, Attachment, Page, Snapshot
from memu.acquisition.sources.message_preview import binding_hash as fingerprint
from memu.acquisition.sources.message_preview import MessagePreviews, binding_hash
from memu.acquisition.sources.messages import clean_message, restrict_page, wire
from memu.acquisition.sources.selection import Selection


def message(identity="m1", text="选中的文字🙂\n第二行", **changes):
    return {"id": identity, "role": "user", "text": text, "occurred_at": None} | changes


def binding():
    return {
        "tenant_id": "tenant",
        "source_instance_id": "source",
        "config": {"provider": "chatgpt", "account_id": "account"},
        "_preview_key": "owner",
    }


def selection(rows):
    return Selection(
        scopes=["conversation"],
        messages={"conversation": {m["id"]: clean_message(m)["digest"] for m in rows}},
    )


def page(rows):
    return Page(
        [
            Snapshot(
                "conversation",
                wire(
                    {
                        "format": "memulife.browser-messages.v1",
                        "provider": "chatgpt",
                        "native_conversation_id": "conversation",
                        "messages": rows,
                    }
                ),
                "\n\n".join(m["text"] for m in rows),
                metadata={
                    "native_conversation_id": "conversation",
                    "selection": "messages",
                    "message_ids": [m["id"] for m in rows],
                    "attachments": "excluded",
                    "actor": {
                        "role": "unknown",
                        "source_actor_id": None,
                        "display_name": None,
                    },
                },
            )
        ],
        {
            "conversation_id": "conversation",
            "native_revision": None,
            "scope_complete": False,
            "observed_at": "2026-10-28T01:02:03.000Z",
        },
    )


def test_digest_matches_javascript_utf8_array_and_binding_fingerprint():
    row = message()
    expected = hashlib.sha256(
        '["m1","user","选中的文字🙂\\n第二行",null]'.encode()
    ).hexdigest()
    assert clean_message(row)["digest"] == expected
    assert clean_message(row | {"digest": expected}, digest=True)["digest"] == expected
    config = binding()
    assert binding_hash(config) == fingerprint(
        {k: v for k, v in config.items() if k != "_preview_key"}
    )
    assert binding_hash(config | {"_preview_key": "other"}) == binding_hash(config)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"id": "x" * 513},
        {"id": "m\x7f"},
        {"id": "m\ud800"},
        {"role": "unexpected"},
        {"text": ""},
        {"text": "\ud800"},
        {"extra": "private"},
        {"occurred_at": "2026-10-28T01:02:03Z"},
        {"occurred_at": "2026-10-28T01:02:03.000+00:00"},
        {"occurred_at": "2026-10-28T01:02:03.000"},
        {"occurred_at": "0001-01-01T00:00:00.000+01:00"},
        {"occurred_at": "9999-12-31T23:59:59.999-01:00"},
        {"occurred_at": 42},
    ],
)
def test_noncanonical_messages_rejected(changes):
    with pytest.raises(AdapterError):
        clean_message(message() | changes)


def test_preview_is_ephemeral_paginated_and_claimed_once():
    pool, config = MessagePreviews(), binding()
    pending = pool.read(config, "conversation", "locator")
    identity = pending["request_id"]
    assert pending["status"] == "pending" and not pending["content_persisted"]
    assert pending["items"] == []
    assert pool.read(config, "conversation", "locator")["request_id"] == identity
    claim = pool.claim(identity)
    assert claim == {
        "request_id": identity,
        "conversation": {"id": "conversation", "locator": "locator", "revision": None},
    }
    assert pool.claim(identity) is None
    # Routine permission checks only inspect metadata and do not consume the claim.
    assert set(pool.pending("tenant", "source")[0]) == {
        "id",
        "owner",
        "conversation",
        "claimed",
    }
    rows = [clean_message(message(str(i))) for i in range(31)]
    assert pool.complete(identity, rows) == {"accepted": 31, "content_persisted": False}
    first = pool.read(config, "conversation", "locator", identity)
    assert first["items"] == rows[:30] and first["total"] == 31
    assert (
        pool.read(config, "conversation", "locator", first["next_cursor"])["items"]
        == rows[30:]
    )
    with pytest.raises(AdapterError, match="import_preview_expired"):
        pool.complete(identity, rows)


@pytest.mark.parametrize("change", ["key", "source", "conversation", "binding"])
def test_preview_cursor_is_bound_to_owner_and_source_configuration(change):
    pool, config = MessagePreviews(), binding()
    identity = pool.read(config, "conversation", "locator")["request_id"]
    pool.claim(identity)
    pool.complete(identity, [clean_message(message())])
    other = deepcopy(config)
    conversation = "conversation"
    if change == "key":
        other["_preview_key"] = "other-manager"
    elif change == "source":
        other["source_instance_id"] = "other-source"
    elif change == "conversation":
        conversation = "other-conversation"
    else:
        other["config"]["account_id"] = "new-account"
    with pytest.raises(AdapterError, match="import_preview_cursor_invalid"):
        pool.read(other, conversation, "locator", identity)


def test_preview_expiry_unclaimed_completion_and_retry_lease():
    pool, config = MessagePreviews(), binding()
    identity = pool.read(config, "conversation", "locator")["request_id"]
    with pytest.raises(AdapterError, match="import_preview_expired"):
        pool.complete(identity, [])
    assert pool.claim(identity)
    pool.values[identity]["claimed"] = time.monotonic() - 151
    assert pool.claim(identity)
    event = pool.values[identity]["event"]
    pool.values[identity]["expires"] = time.monotonic() - 1
    with pytest.raises(AdapterError, match="import_preview_expired"):
        pool.owner(identity)
    assert event.is_set() and not pool.values


@pytest.mark.parametrize(
    "payload", ["wrong-digest", "duplicate", "extra", "count", "bytes"]
)
def test_preview_never_truncates_or_accepts_unknown_data(payload):
    pool, config = MessagePreviews(), binding()
    identity = pool.read(config, "conversation", "locator")["request_id"]
    pool.claim(identity)
    row = clean_message(message())
    rows = [row]
    if payload == "wrong-digest":
        rows = [row | {"digest": "0" * 64}]
    elif payload == "duplicate":
        rows = [row, row]
    elif payload == "extra":
        rows = [row | {"raw": "PRIVATE"}]
    elif payload == "count":
        rows = [clean_message(message(str(i))) for i in range(501)]
    else:
        rows = [clean_message(message(text="字" * (310 * 1024)))]
    with pytest.raises(AdapterError):
        pool.complete(identity, rows)
    assert pool.values[identity]["messages"] == []
    assert pool.values[identity]["bytes"] == 0
    assert pool.values[identity]["status"] == "pending"


def test_failed_preview_only_accepts_stable_error_code():
    pool, config = MessagePreviews(), binding()
    identity = pool.read(config, "conversation", "locator")["request_id"]
    pool.claim(identity)
    with pytest.raises(AdapterError):
        pool.complete(identity, [], "error: PRIVATE RESPONSE")
    with pytest.raises(AdapterError):
        pool.complete(identity, [clean_message(message())], "network_error")
    pool.complete(identity, [], "network_error")
    result = pool.read(config, "conversation", "locator", identity)
    assert result["status"] == "failed" and result["error_code"] == "network_error"
    assert result["items"] == [] and not result["content_persisted"]


def test_selected_identity_is_stable_for_set_and_distinct_from_full_conversation():
    rows = [message(), message("m2", "another", role="assistant")]
    scope = selection(rows)
    first = restrict_page(page(rows), scope, "chatgpt")
    reverse = restrict_page(page(list(reversed(rows))), scope, "chatgpt")
    subset = restrict_page(page(rows[:1]), selection(rows[:1]), "chatgpt")
    assert first.records[0].object_id == reverse.records[0].object_id
    assert first.records[0].object_id.startswith("selected-messages:")
    assert subset.records[0].object_id != first.records[0].object_id
    assert json.loads(first.records[0].raw)["messages"] == rows


@pytest.mark.parametrize(
    "field",
    [
        "raw",
        "text",
        "metadata",
        "revision",
        "occurred_at",
        "attachment",
        "deleted",
        "cursor",
        "cursor-null",
        "cursor-scope",
        "duplicate-json",
    ],
)
def test_selection_rejects_every_persistent_side_channel(field):
    rows = [message()]
    candidate = page(rows)
    record = candidate.records[0]
    if field == "raw":
        value = json.loads(record.raw)
        value["messages"].append(message("private", "PRIVATE"))
        record.raw = wire(value)
    elif field == "text":
        record.text += "PRIVATE"
    elif field == "metadata":
        record.metadata["private"] = "PRIVATE"
    elif field in {"revision", "occurred_at"}:
        setattr(record, field, "PRIVATE")
    elif field == "attachment":
        record.attachments = [Attachment("private", b"PRIVATE")]
    elif field == "deleted":
        record.deleted = True
    elif field == "cursor":
        candidate.cursor["private"] = "PRIVATE"
    elif field == "cursor-null":
        candidate.cursor["observed_at"] = None
    elif field == "cursor-scope":
        candidate.cursor["conversation_id"] = "unselected"
    else:
        record.raw = record.raw.replace(b'"text":', b'"text":"PRIVATE","text":')
    with pytest.raises(AdapterError):
        restrict_page(candidate, selection(rows), "chatgpt")


def test_whole_and_selected_records_can_coexist_without_expanding_selection():
    rows = [message()]
    candidate = page(rows)
    whole = Snapshot("whole", b"full response", "full text", metadata={"native": True})
    candidate.records.insert(0, whole)
    scope = selection(rows).model_copy(update={"scopes": ["whole", "conversation"]})
    result = restrict_page(candidate, scope, "chatgpt")
    assert result.records[0] == whole
    assert result.records[1].object_id.startswith("selected-messages:")


def test_selection_budget_is_total_and_uses_utf8_bytes():
    digest = clean_message(message())["digest"]
    chosen = {str(i): digest for i in range(500)}
    assert len(Selection(scopes=["c"], messages={"c": chosen}).messages["c"]) == 500
    with pytest.raises(ValidationError):
        Selection(scopes=["a", "b"], messages={"a": chosen, "b": {"extra": digest}})
    unicode_ids = {f"字{i}": digest for i in range(500)}
    assert Selection(scopes=["c"], messages={"c": unicode_ids})
    with pytest.raises(ValidationError):
        Selection(
            scopes=["c"],
            messages={"c": {f"{i}" + "字" * 300: digest for i in range(500)}},
        )


@pytest.mark.parametrize("identity", ["id\x00", "id\x7f", "id\ud800"])
@pytest.mark.parametrize("field", ["scope", "message-id"])
def test_selection_rejects_controls_and_surrogates_before_budget_encoding(
    identity, field
):
    digest = clean_message(message())["digest"]
    value = (
        {"scopes": [identity], "messages": {identity: {"m": digest}}}
        if field == "scope"
        else {"scopes": ["c"], "messages": {"c": {identity: digest}}}
    )
    with pytest.raises(ValidationError):
        Selection.model_validate(value)
