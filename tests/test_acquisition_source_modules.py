"""Source contracts remain independent of persistence and projection policy."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from memu.acquisition.adapters.base import AdapterError, Attachment, Page, Snapshot
from memu.acquisition.adapters.registry import adapter_class, manifest
from memu.acquisition.sources.selection import Selection
from memu.acquisition.sources.reader import SelectedReader, prepare_reader
from memu.acquisition.sources import get_source, sources
from memu.acquisition.sources.base import SourceModule


def test_registry_is_lazy_and_does_not_load_persistence_or_provider_readers():
    script = """
import sys
from memu.acquisition.sources import sources
assert len(sources()) == 9
assert not any(name.startswith(('memu.acquisition.importing', 'memu.acquisition.projection', 'memu.acquisition.db')) for name in sys.modules)
assert 'memu.acquisition.adapters.qq' not in sys.modules
assert 'memu.acquisition.adapters.host' not in sys.modules
assert 'sqlalchemy' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert len({source.slug for source in sources()}) == 9
    assert get_source("qq.napcat").profile()["page_kind"] == "chats"
    assert get_source("browser.ai-history").profile()["page_kind"] == "conversations"
    assert not get_source("screen.native").continuous_updates
    assert get_source("screen.native").profile()["continuous_updates"] is False
    assert get_source("qq.napcat").profile()["continuous_updates"] is True
    with pytest.raises(AdapterError, match="adapter_unknown"):
        get_source("arbitrary.module")
    with pytest.raises(AdapterError, match="source_profile_unknown"):
        adapter_class("qq.napcat", "arbitrary.module")


def test_a_registered_source_controls_filtering_without_importer_branches(monkeypatch):
    from memu.acquisition.sources import registry

    class ExtraSource(SourceModule):
        adapter_id = "fixture.extra"
        scope_fields = ("items",)
        time_filter = True

        def includes(self, record, selection):
            return "items:" + record.object_id in selection.scopes

        def timestamp(self, record):
            return record.metadata.get("source_time")

    class Reader:
        def read(self, *args):
            return Page(
                [
                    Snapshot(
                        "chosen",
                        b"selected",
                        metadata={"source_time": "2026-10-28T10:00:00Z"},
                        attachments=[Attachment("image", b"not selected")],
                    ),
                    Snapshot(
                        "elsewhere",
                        b"NEVER PERSIST",
                        metadata={"source_time": "2026-10-28T10:00:00Z"},
                    ),
                    Snapshot(
                        "chosen",
                        b"outside time",
                        metadata={"source_time": "2026-10-27T10:00:00Z"},
                    ),
                ],
                {"cursor": "next"},
            )

    source = ExtraSource()
    monkeypatch.setitem(registry._BY_ID, source.adapter_id, source)
    binding = {
        "adapter_id": source.adapter_id,
        "source_profile": "fixture",
        "config": {"items": ["chosen", "elsewhere"]},
    }
    selection = Selection(scopes=["items:chosen"], start="2026-10-28T00:00:00Z")
    source.validate(binding, selection)
    result = SelectedReader(binding, selection, Reader()).read("backfill")
    assert [r.raw for r in result.records] == [b"selected"]
    assert result.records[0].attachments[0].data is None
    assert result.cursor["examined"] == 3 and result.cursor["selected"] == 1
    assert not result.scope_complete
    with pytest.raises(AdapterError, match="import_scope_denied"):
        source.validate(binding, Selection(scopes=["items:missing"]))


def test_file_module_freezes_metadata_without_opening_originals(tmp_path, monkeypatch):
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("original A")
    (root / "b.md").write_text("original B")
    source = get_source("filesystem.typora")
    binding = {"config": {"root": str(root)}}
    original = Path.read_bytes

    def no_original_reads(path):
        if path.is_relative_to(root):
            raise AssertionError("metadata discovery opened original content")
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", no_original_reads)
        first = source.candidates(binding, limit=1)
        assert first["items"][0]["id"] == "a.md"
        second = source.candidates(binding, cursor=first["next_cursor"], limit=1)
        assert second["items"][0]["id"] == "b.md" and second["next_cursor"] is None
        frozen = source.prepare_plan(binding, Selection(scopes=["."]))
        source.verify_plan(binding, frozen)
    (root / "arrived.md").write_text("future arrival")
    assert source.prepare_plan(binding, frozen).scopes == ["a.md", "b.md"]
    (root / "a.md").write_text("changed after plan")
    with pytest.raises(AdapterError, match="import_candidate_changed"):
        source.verify_plan(binding, frozen)
    (root / "link.md").symlink_to(root / "a.md")
    with pytest.raises(AdapterError, match="import_scope_denied"):
        source.prepare_plan(binding, Selection(scopes=["link.md"]))


def test_candidate_search_filters_before_pagination_and_does_not_search_body(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    (root / "folder").mkdir()
    (root / "folder/inside.md").write_text("inside")
    (root / "a.md").write_text("needle appears in body only")
    (root / "b-needle.md").write_text("not indexed")
    (root / "c-needle.md").write_text("not indexed")
    source = get_source("filesystem.typora")
    binding = {"config": {"root": str(root)}}
    first = source.candidates(binding, query="NEEDLE", kind="file", limit=1)
    assert [item["id"] for item in first["items"]] == ["b-needle.md"]
    second = source.candidates(
        binding, query="needle", kind="file", cursor=first["next_cursor"], limit=1
    )
    assert [item["id"] for item in second["items"]] == ["c-needle.md"]
    assert second["next_cursor"] is None
    assert source.candidates(binding, query="inside")["items"] == []
    assert source.candidates(binding, kind="directory")["items"][0]["id"] == "folder"

    notion = SourceModule()
    notion.scope_fields = ("page_ids", "database_ids")
    catalog = {
        "config": {
            "page_ids": ["a", "b-match", "c-match"],
            "database_ids": ["match-db"],
        }
    }
    first = notion.candidates(catalog, query="match", kind="page_ids", limit=1)
    assert first["items"][0]["id"] == "page_ids:b-match"
    second = notion.candidates(
        catalog, query="match", kind="page_ids", cursor=first["next_cursor"], limit=1
    )
    assert second["items"][0]["id"] == "page_ids:c-match"
    assert second["next_cursor"] is None


def test_chat_browsing_is_a_separate_transient_capability(monkeypatch):
    from memu.acquisition.sources import browsing

    seen = []
    monkeypatch.setattr(
        browsing,
        "browse",
        lambda binding, **kwargs: seen.append(kwargs) or {"items": []},
    )
    source = get_source("qq.napcat")
    source.candidates({}, query="match", kind="group", cursor="cursor", limit=200)
    assert seen == [
        {"query": "match", "kind": "group", "cursor": "cursor", "limit": 100}
    ]
    with pytest.raises(AdapterError, match="capability_unavailable"):
        get_source("screen.native").preview_page({}, Selection(scopes=["one"]))


def test_local_ai_discovery_does_not_parse_and_reader_receives_only_selected_paths(
    tmp_path, monkeypatch
):
    root = tmp_path / "client"
    (root / "sessions").mkdir(parents=True)
    (root / "sessions/chosen.jsonl").write_text(
        "not valid JSON; discovery must not parse"
    )
    (root / "sessions/private.jsonl").write_text("unselected")
    source = get_source("ai.local-logs")
    binding = {"config": {"root": str(root)}, "source_profile": "codex"}
    choices = source.scopes(binding)
    assert {row["id"] for row in choices} == {
        "sessions/chosen.jsonl",
        "sessions/private.jsonl",
    }
    selection = Selection(scopes=["sessions/chosen.jsonl"])
    source.validate(binding, selection)
    reader = adapter_class(source.adapter_id, "codex")(binding["config"])
    prepare_reader(reader, selection)
    assert [str(p.relative_to(root)) for p in reader.access.files(reader.patterns)] == [
        "sessions/chosen.jsonl"
    ]
    assert source.includes(
        Snapshot("id", b"", metadata={"file": "sessions/chosen.jsonl"}), selection
    )
    assert not source.includes(
        Snapshot("id", b"", metadata={"file": "sessions/private.jsonl"}), selection
    )


@pytest.mark.parametrize("identity", ["qq.napcat", "wechat.localdb"])
def test_chat_refresh_preserves_fixed_history_and_daily_reconciliation(identity):
    source = get_source(identity)
    now = datetime.now(timezone.utc)
    selection = Selection(scopes=["one"], start=now - timedelta(days=10))
    subscription = {
        "watermark": now - timedelta(minutes=10),
        "reconciled_at": now - timedelta(hours=2),
    }
    updated = source.refresh_selection({}, selection, subscription, now)
    assert updated.start == now - timedelta(minutes=15)
    fixed = selection.model_copy(update={"end": now - timedelta(days=1)})
    assert source.refresh_selection({}, fixed, subscription, now) == fixed
    subscription["reconciled_at"] = now - timedelta(days=2)
    assert source.refresh_selection({}, selection, subscription, now) == selection


def test_media_contract_and_legacy_manifests_stay_compatible():
    immich = get_source("immich.assets")
    config = {"all_assets": True, "album_ids": []}
    selection = Selection(scopes=["all_assets"])
    immich.validate({"config": config}, selection)
    assert immich.narrow_config(config, selection) == config
    assert immich.primary_media and get_source("screen.native").primary_media
    assert (
        immich.timestamp(
            Snapshot("id", b"", occurred_at="wrong", metadata={"taken_at": "native"})
        )
        == "native"
    )
    for source in sources():
        value = manifest(source.adapter_id)
        assert value["supported_source_profiles"] == list(source.profiles)
        assert value["event_types"] == list(source.event_types)
