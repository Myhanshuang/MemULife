"""Discovery metadata remains bounded, transient, and tied to its source."""

import pytest

from memu.acquisition.adapters.base import AdapterError
from memu.acquisition.sources.discovery import Discovery


def test_titles_times_and_counts_are_metadata_only_and_provider_bound():
    catalog = Discovery()
    item = {
        "id": "conversation",
        "locator": "conversation",
        "title": '<project> "设计评审"',
        "provider": "claude",
        "revision": "opaque",
        "created_at": "2026-10-01T10:00:00+08:00",
        "updated_at": "2026-10-02T03:00:00.123Z",
        "message_count": 12,
    }
    result = catalog.offer("tenant", "source", [item], provider="claude")
    assert result["content_persisted"] is False
    row = catalog.get("tenant", "source")[0]
    assert row["name"] == row["title"] == item["title"]
    assert row["message_count"] == 12
    assert row["provider"] == "claude"
    assert row["created_at"] == item["created_at"]
    assert not catalog.get("another-tenant", "source")
    with pytest.raises(AdapterError, match="import_catalog_invalid"):
        catalog.offer("tenant", "source", [item], provider="chatgpt")
    assert catalog.get("tenant", "source") == [row]


@pytest.mark.parametrize(
    "field,value",
    [
        ("raw", "private body"),
        ("messages", []),
        ("title", "x" * 513),
        ("title", "line\nline"),
        ("title", {}),
        ("title", "broken\ud800unicode"),
        ("locator", "broken\udfff"),
        ("provider", "other-platform"),
        ("provider", {}),
        ("created_at", "2026-10-01T10:00:00"),
        ("updated_at", "2026-02-30T10:00:00Z"),
        ("updated_at", "2026-10-01T10:00:00+00:60"),
        ("updated_at", "2026-10-01T24:00:00Z"),
        ("updated_at", 1780000000),
        ("message_count", True),
        ("message_count", -1),
        ("message_count", 1.5),
        ("message_count", 2**53),
    ],
)
def test_invalid_metadata_does_not_enter_the_catalog(field, value):
    catalog = Discovery()
    with pytest.raises(AdapterError, match="import_catalog_invalid"):
        catalog.offer("tenant", "source", [{"id": "id", "locator": "id", field: value}])
    assert catalog.get("tenant", "source") == []


def test_missing_metadata_is_unknown_and_older_clients_remain_compatible():
    catalog = Discovery()
    catalog.offer(
        "tenant", "source", [{"id": "old", "locator": "old"}], provider="grok"
    )
    row = catalog.get("tenant", "source")[0]
    assert row["name"] == "old"
    assert row["provider"] == "grok"
    assert (
        row["title"]
        is row["created_at"]
        is row["updated_at"]
        is row["message_count"]
        is None
    )


def test_global_encoded_budget_is_atomic_and_released_on_expiry(monkeypatch):
    import memu.acquisition.sources.discovery as exchange

    catalog = Discovery()
    item = {"id": "one", "locator": "one", "title": "测试" * 100}
    catalog.offer("tenant", "a", [item])
    row = catalog.get("tenant", "a")[0]
    size = catalog.values[("tenant", "a")][2]
    monkeypatch.setattr(exchange, "MAX_CATALOG_BYTES", size + 10)
    with pytest.raises(AdapterError, match="import_catalog_capacity"):
        catalog.offer("tenant", "b", [item])
    assert catalog.get("tenant", "a") == [row]
    assert catalog.get("tenant", "b") == []
    future = exchange.time.monotonic() + exchange.TTL + 1
    monkeypatch.setattr(exchange.time, "monotonic", lambda: future)
    catalog.offer("tenant", "b", [item])
    assert catalog.get("tenant", "a") == []
    assert catalog.get("tenant", "b")[0]["title"] == item["title"]
