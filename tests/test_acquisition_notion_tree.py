"""Notion hierarchy authorization and precise selected-page reads."""

import json
from uuid import UUID

import httpx
import pytest

from memu.acquisition.adapters.base import AdapterError
from memu.acquisition.adapters.notion import NotionAdapter
from memu.acquisition.sources.notion import source
from memu.acquisition.sources.selection import Selection


def uid(number):
    return str(UUID(int=number))


ROOT, BLOCK, CHILD, DATABASE, DATA_SOURCE, ENTRY, SIBLING, OTHER, NESTED = map(
    uid, range(1, 10)
)
STAMP = "2026-10-28T01:00:00Z"


def ref(kind, identity):
    return {"type": kind, kind: identity}


def paged(rows, cursor=None):
    return {
        "object": "list",
        "results": rows,
        "has_more": bool(cursor),
        "next_cursor": cursor,
    }


class Fixture:
    def __init__(self, monkeypatch, **config):
        self.calls = []
        self.parents = {
            ROOT: {"type": "workspace", "workspace": True},
            BLOCK: ref("page_id", ROOT),
            CHILD: ref("block_id", BLOCK),
            DATABASE: ref("page_id", ROOT),
            DATA_SOURCE: ref("database_id", DATABASE),
            ENTRY: ref("data_source_id", DATA_SOURCE),
            SIBLING: ref("data_source_id", DATA_SOURCE),
            OTHER: {"type": "workspace", "workspace": True},
            NESTED: ref("page_id", ENTRY),
        }
        self.after_blocks = None
        self.after_query = None
        self.archived = set()
        self.config = {
            "workspace_id": "fixture-workspace",
            "token_ref": "env:NOTION_TREE_FIXTURE",
            "page_ids": [ROOT],
        } | config
        self.binding = {
            "tenant_id": "fixture-tenant",
            "source_instance_id": "fixture-source",
            "config": self.config,
        }
        monkeypatch.setenv("NOTION_TREE_FIXTURE", "fixture-token-only")
        monkeypatch.setattr("memu.acquisition.adapters.notion.NotionAdapter", self.reader)

    def reader(self, config=None):
        return NotionAdapter(
            config or self.config,
            httpx.Client(transport=httpx.MockTransport(self.dispatch)),
        )

    def page(self, identity):
        return {
            "object": "page",
            "id": identity,
            "parent": self.parents[identity],
            "last_edited_time": STAMP,
            "archived": identity in self.archived,
            "properties": {
                "Name": {
                    "id": "title",
                    "type": "title",
                    "title": [{"plain_text": "title-" + identity[-2:]}],
                },
                "Unselected": {
                    "id": "text",
                    "type": "rich_text",
                    "rich_text": [{"plain_text": "UNSELECTED_PROPERTY"}],
                },
            },
        }

    def dispatch(self, request):
        self.calls.append((request.method, request.url.path, dict(request.url.params)))
        path = request.url.path.removeprefix("/v1")
        if path == "/users/me":
            value = {"bot": {"workspace_id": "fixture-workspace"}}
        elif path.startswith("/pages/") and "/properties/" not in path:
            value = self.page(path.split("/")[2])
        elif "/properties/" in path:
            value = paged([])
        elif path == f"/databases/{DATABASE}":
            value = {
                "object": "database",
                "id": DATABASE,
                "parent": self.parents[DATABASE],
                "title": [{"plain_text": "Library"}],
                "data_sources": [{"id": DATA_SOURCE, "name": "Entries"}],
            }
        elif path == f"/data_sources/{DATA_SOURCE}":
            value = {
                "object": "data_source",
                "id": DATA_SOURCE,
                "parent": self.parents[DATA_SOURCE],
                "title": [{"plain_text": "Entries"}],
            }
        elif path == f"/data_sources/{DATA_SOURCE}/query":
            body = json.loads(request.content)
            value = (
                paged([self.page(SIBLING)])
                if body.get("start_cursor")
                else paged([self.page(ENTRY)], "second-page")
            )
            if self.after_query:
                self.after_query()
        elif path == f"/blocks/{BLOCK}":
            value = {
                "object": "block",
                "id": BLOCK,
                "type": "toggle",
                "parent": self.parents[BLOCK],
                "toggle": {"rich_text": [{"plain_text": "PRIVATE_BLOCK_BODY"}]},
            }
        elif path.endswith("/children"):
            identity = path.split("/")[2]
            if identity == ROOT:
                rows = [
                    {
                        "id": BLOCK,
                        "type": "toggle",
                        "has_children": True,
                        "toggle": {"rich_text": [{"plain_text": "PRIVATE_BLOCK_BODY"}]},
                    },
                    {
                        "id": DATABASE,
                        "type": "child_database",
                        "child_database": {"title": "Library"},
                    },
                ]
            elif identity == BLOCK:
                rows = [
                    {
                        "id": CHILD,
                        "type": "child_page",
                        "child_page": {"title": "Nested page"},
                    },
                    {
                        "id": OTHER,
                        "type": "link_to_page",
                        "has_children": True,
                        "link_to_page": ref("page_id", OTHER),
                    },
                ]
            elif identity == ENTRY:
                rows = [
                    {
                        "id": uid(20),
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"plain_text": "SELECTED_BODY"}]},
                    },
                    {
                        "id": NESTED,
                        "type": "child_page",
                        "child_page": {"title": "Nested reference"},
                    },
                ]
            elif identity in {CHILD, SIBLING, NESTED}:
                rows = [
                    {
                        "id": uid(21),
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [{"plain_text": "BODY-" + identity}]
                        },
                    }
                ]
            else:
                pytest.fail("unrecognized fixture children")
            value = paged(rows)
            if self.after_blocks:
                self.after_blocks(identity)
        else:
            pytest.fail("unrecognized fixture request: " + path)
        return httpx.Response(200, json=value)


def test_page_nested_blocks_and_database_entries_are_browsed_without_content_leak(
    monkeypatch,
):
    fixture = Fixture(monkeypatch)
    roots = source.candidates(fixture.binding)
    assert roots["items"][0]["id"] == "page_ids:" + ROOT
    first = source.candidates(fixture.binding, parent="page_ids:" + ROOT)
    assert [item["id"] for item in first["items"]] == ["database_ids:" + DATABASE]
    assert first["next_cursor"] and first["content_persisted"] is False
    second = source.candidates(
        fixture.binding, parent="page_ids:" + ROOT, cursor=first["next_cursor"]
    )
    assert [item["id"] for item in second["items"]] == ["page_ids:" + CHILD]
    assert second["next_cursor"] is None
    database = source.candidates(fixture.binding, parent="database_ids:" + DATABASE)
    assert database["items"][0]["selection_kind"] == "container"
    assert database["items"][0]["id"] == "data_source_ids:" + DATA_SOURCE
    entries = source.candidates(
        fixture.binding, parent="data_source_ids:" + DATA_SOURCE, limit=1
    )
    assert entries["items"][0]["id"] == "page_ids:" + ENTRY
    last = source.candidates(
        fixture.binding,
        parent="data_source_ids:" + DATA_SOURCE,
        limit=1,
        cursor=entries["next_cursor"],
    )
    assert last["items"][0]["id"] == "page_ids:" + SIBLING
    assert [item["id"] for item in entries["breadcrumbs"]] == [
        ".",
        "page_ids:" + ROOT,
        "database_ids:" + DATABASE,
        "data_source_ids:" + DATA_SOURCE,
    ]
    wire = json.dumps([roots, first, second, database, entries, last])
    assert "PRIVATE_BLOCK_BODY" not in wire and "UNSELECTED_PROPERTY" not in wire
    assert not any(f"/blocks/{ENTRY}/children" in call[1] for call in fixture.calls)


def test_empty_search_pages_keep_continuation_and_cursors_bind_scope_and_config(
    monkeypatch,
):
    fixture = Fixture(monkeypatch)
    page = source.candidates(fixture.binding, parent="page_ids:" + ROOT, query="Nested")
    assert page["items"] == [] and page["next_cursor"]
    later = source.candidates(
        fixture.binding,
        parent="page_ids:" + ROOT,
        query="Nested",
        cursor=page["next_cursor"],
    )
    assert len(later["items"]) == 1
    with pytest.raises(AdapterError, match="notion_cursor_expired"):
        source.candidates(
            fixture.binding,
            parent="database_ids:" + DATABASE,
            query="Nested",
            cursor=page["next_cursor"],
        )
    with pytest.raises(AdapterError, match="notion_cursor_expired"):
        source.candidates(
            fixture.binding | {"source_instance_id": "different"},
            parent="page_ids:" + ROOT,
            query="Nested",
            cursor=page["next_cursor"],
        )


def test_unrelated_page_and_reparented_child_cannot_be_planned(monkeypatch):
    fixture = Fixture(monkeypatch)
    with pytest.raises(AdapterError, match="import_scope_denied"):
        source.prepare_plan(fixture.binding, Selection(scopes=["page_ids:" + OTHER]))
    chosen = source.prepare_plan(
        fixture.binding, Selection(scopes=["page_ids:" + CHILD])
    )
    fixture.parents[CHILD] = ref("page_id", OTHER)
    with pytest.raises(AdapterError, match="import_scope_denied"):
        source.verify_plan(fixture.binding, chosen)


def test_moving_a_selected_page_within_allowed_roots_invalidates_path_plan(monkeypatch):
    fixture = Fixture(monkeypatch)
    chosen = source.prepare_plan(
        fixture.binding, Selection(scopes=["page_ids:" + CHILD])
    )
    fixture.parents[CHILD] = ref("page_id", ROOT)
    with pytest.raises(AdapterError, match="import_plan_stale"):
        source.verify_plan(fixture.binding, chosen)


def test_exact_database_entry_read_excludes_siblings_and_nested_pages(monkeypatch):
    fixture = Fixture(monkeypatch, include_children=True)
    chosen = source.prepare_plan(
        fixture.binding, Selection(scopes=["page_ids:" + ENTRY])
    )
    reader = fixture.reader()
    source.prepare_reader(reader, chosen)
    result = reader.read("backfill")
    assert [record.object_id for record in result.records] == [ENTRY]
    assert not result.has_more
    assert "SELECTED_BODY" in result.records[0].text
    assert not any(
        f"/blocks/{id}/children" in call[1]
        for id in (SIBLING, NESTED)
        for call in fixture.calls
    )
    assert not any(call[0] == "POST" for call in fixture.calls)


def test_page_preview_reads_selected_body_and_checks_scope_and_continuation(monkeypatch):
    fixture = Fixture(monkeypatch)
    original = fixture.dispatch
    def dispatch(request):
        response = original(request)
        if request.url.path == f"/v1/blocks/{ENTRY}/children":
            value = response.json()
            value["results"][0]["paragraph"]["rich_text"][0]["plain_text"] = "SELECTED_BODY" * 500
            return httpx.Response(200, json=value)
        return response
    fixture.dispatch = dispatch
    selected = Selection(scopes=["page_ids:" + ENTRY])
    first = source.preview_page(fixture.binding, selected, limit=1)
    assert "SELECTED_BODY" in first["items"][0]["text"]
    assert first["content_persisted"] is False and first["next_cursor"]
    later = source.preview_page(fixture.binding, selected, first["next_cursor"], limit=1)
    assert later["items"][0]["text"] and later["next_cursor"]
    assert not any(f"/blocks/{id}/children" in call[1] for id in (SIBLING, NESTED) for call in fixture.calls)
    with pytest.raises(AdapterError, match="notion_cursor_expired"):
        source.preview_page(fixture.binding | {"source_instance_id": "other"}, selected, first["next_cursor"])
    with pytest.raises(AdapterError, match="import_scope_denied"):
        source.preview_page(fixture.binding, Selection(scopes=["page_ids:" + OTHER]))
    with pytest.raises(AdapterError, match="import_scope_denied"):
        source.preview_page(fixture.binding, Selection(scopes=["database_ids:" + DATABASE]))


def test_container_selection_reads_rows_but_not_their_child_pages(monkeypatch):
    fixture = Fixture(monkeypatch, include_children=True)
    chosen = source.prepare_plan(
        fixture.binding, Selection(scopes=["database_ids:" + DATABASE])
    )
    reader = fixture.reader()
    source.prepare_reader(reader, chosen)
    cursor, records = None, []
    for _ in range(12):
        result = reader.read("backfill", cursor)
        records.extend(result.records)
        cursor = result.cursor
        if not result.has_more:
            break
    assert {record.object_id for record in records} == {ENTRY, SIBLING}
    assert not any(f"/blocks/{NESTED}/children" in call[1] for call in fixture.calls)


def test_reparent_during_read_discards_body_before_caller_receives_record(monkeypatch):
    fixture = Fixture(monkeypatch)
    chosen = source.prepare_plan(
        fixture.binding, Selection(scopes=["page_ids:" + ENTRY])
    )
    reader = fixture.reader()
    source.prepare_reader(reader, chosen)
    fixture.after_blocks = lambda identity: (
        fixture.parents.update({ENTRY: ref("page_id", OTHER)})
        if identity == ENTRY
        else None
    )
    with pytest.raises(AdapterError, match="import_scope_denied"):
        reader.read("backfill")


def test_archived_selected_page_can_emit_tombstone_without_body_fetch(monkeypatch):
    fixture = Fixture(monkeypatch)
    chosen = source.prepare_plan(
        fixture.binding, Selection(scopes=["page_ids:" + ENTRY])
    )
    fixture.archived.update({ENTRY, ROOT})
    reader = fixture.reader()
    source.prepare_reader(reader, chosen)
    result = reader.read("backfill")
    assert result.records[0].deleted
    assert not any(f"/blocks/{ENTRY}/children" in call[1] for call in fixture.calls)


def test_ancestry_cycles_and_scope_field_forgery_fail_closed(monkeypatch):
    fixture = Fixture(monkeypatch)
    fixture.parents[CHILD] = ref("page_id", CHILD)
    with pytest.raises(AdapterError, match="notion_tree_cycle"):
        source.prepare_plan(fixture.binding, Selection(scopes=["page_ids:" + CHILD]))
    with pytest.raises(AdapterError, match="import_scope_denied"):
        source.candidates(fixture.binding, parent="block_ids:" + BLOCK)


def test_moving_a_container_during_query_does_not_return_ids_for_persistence(
    monkeypatch,
):
    fixture = Fixture(monkeypatch)
    chosen = source.prepare_plan(
        fixture.binding, Selection(scopes=["data_source_ids:" + DATA_SOURCE])
    )
    reader = fixture.reader()
    source.prepare_reader(reader, chosen)
    fixture.after_query = lambda: fixture.parents.update(
        {DATABASE: ref("page_id", OTHER)}
    )
    with pytest.raises(AdapterError, match="import_scope_denied"):
        reader.read("backfill")
    assert not any(f"/blocks/{ENTRY}/children" in call[1] for call in fixture.calls)


def test_moving_a_nested_block_during_browse_discards_candidates(monkeypatch):
    fixture = Fixture(monkeypatch)
    first = source.candidates(fixture.binding, parent="page_ids:" + ROOT)
    fixture.after_blocks = lambda identity: (
        fixture.parents.update({BLOCK: ref("page_id", OTHER)})
        if identity == BLOCK
        else None
    )
    with pytest.raises(AdapterError, match="import_scope_denied"):
        source.candidates(
            fixture.binding, parent="page_ids:" + ROOT, cursor=first["next_cursor"]
        )


def test_request_budget_stops_ancestry_and_cursor_store_has_bounded_lifetime(
    monkeypatch,
):
    from memu.acquisition.sources import notion_tree

    fixture = Fixture(monkeypatch)
    monkeypatch.setattr(notion_tree, "MAX_REQUESTS", 1)
    with pytest.raises(AdapterError, match="notion_tree_budget_exceeded"):
        source.prepare_plan(fixture.binding, Selection(scopes=["page_ids:" + CHILD]))
    store = notion_tree.CursorStore()
    monkeypatch.setattr(notion_tree.time, "monotonic", lambda: 100)
    state = {"pending": [ROOT], "visited": []}
    token = store.put("binding", state)
    state["pending"].append(OTHER)
    assert store.get(token, "binding")["pending"] == [ROOT]
    monkeypatch.setattr(notion_tree.time, "monotonic", lambda: 701)
    with pytest.raises(AdapterError, match="notion_cursor_expired"):
        store.get(token, "binding")
    with pytest.raises(AdapterError, match="notion_tree_budget_exceeded"):
        store.put("binding", {"pending": ["x" * 65536]})
