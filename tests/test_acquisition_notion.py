import json
from uuid import uuid4

import httpx
import pytest

from memu.acquisition.adapters.base import AdapterError
from memu.acquisition.adapters.notion import NotionAdapter, API_VERSION

PAGE, CHILD, BLOCK, DATABASE, SOURCE = [str(uuid4()) for _ in range(5)]
STAMP = "2026-10-01T12:00:00.000Z"


def rt(text):
    return [{"type": "text", "plain_text": text}]


def listing(rows, cursor=None):
    return {
        "object": "list",
        "results": rows,
        "has_more": bool(cursor),
        "next_cursor": cursor,
    }


def adapter(monkeypatch, handler, **overrides):
    monkeypatch.setenv("NOTION_FIXTURE_TOKEN", "fixture-private-token")

    def dispatch(request):
        assert request.headers["Authorization"] == "Bearer fixture-private-token"
        assert request.headers["Notion-Version"] == API_VERSION
        assert request.url.host == "api.notion.com"
        if request.url.path == "/v1/users/me":
            return httpx.Response(200, json={"bot": {"workspace_id": "workspace"}})
        result = handler(request)
        return (
            result
            if isinstance(result, httpx.Response)
            else httpx.Response(200, json=result)
        )

    return NotionAdapter(
        {
            "workspace_id": "workspace",
            "token_ref": "env:NOTION_FIXTURE_TOKEN",
            "page_ids": [PAGE],
        }
        | overrides,
        httpx.Client(transport=httpx.MockTransport(dispatch)),
    )


def page(identity=PAGE, **extra):
    return {
        "object": "page",
        "id": identity,
        "last_edited_time": STAMP,
        "properties": {},
        "url": "https://notion.so/" + identity,
    } | extra


def test_nested_blocks_paginated_properties_child_scope_and_no_file_download(
    monkeypatch,
):
    calls = []

    def handler(r):
        calls.append(str(r.url))
        path = r.url.path
        if path == f"/v1/pages/{PAGE}":
            return page(
                properties={
                    "名称": {"id": "title", "type": "title"},
                    "相关": {"id": "relation", "type": "relation"},
                }
            )
        if path.endswith("/properties/title"):
            return listing([{"type": "title", "title": {"plain_text": "项目计划"}}])
        if path.endswith("/properties/relation"):
            if r.url.params.get("start_cursor"):
                return listing([{"type": "relation", "relation": {"id": CHILD}}])
            return listing([{"type": "relation", "relation": {"id": DATABASE}}], "next")
        if path == f"/v1/blocks/{PAGE}/children":
            return listing(
                [
                    {
                        "id": BLOCK,
                        "type": "toggle",
                        "has_children": True,
                        "toggle": {"rich_text": rt("详情")},
                    },
                    {
                        "id": CHILD,
                        "type": "child_page",
                        "has_children": True,
                        "child_page": {"title": "子页面"},
                    },
                    {
                        "id": DATABASE,
                        "type": "file",
                        "file": {
                            "type": "file",
                            "file": {
                                "url": "https://untrusted.example/secret",
                                "expiry_time": STAMP,
                            },
                        },
                    },
                ]
            )
        if path == f"/v1/blocks/{BLOCK}/children":
            return listing(
                [
                    {
                        "id": str(uuid4()),
                        "type": "table_row",
                        "table_row": {"cells": [rt("列 A"), rt("列 B")]},
                    },
                    {
                        "id": str(uuid4()),
                        "type": "to_do",
                        "to_do": {"checked": True, "rich_text": rt("验收")},
                    },
                ]
            )
        pytest.fail(path)

    reader = adapter(monkeypatch, handler)
    result = reader.read("backfill")
    record = result.records[0]
    assert (
        "项目计划" in record.text
        and "列 A | 列 B" in record.text
        and "[x] 验收" in record.text
    )
    assert result.cursor["pages"] == [] and not result.has_more
    assert record.attachments[0].data is None
    assert CHILD in record.text  # relationship ID, not fetched content
    assert not any(f"/pages/{CHILD}" in call for call in calls)
    assert "fixture-private-token" not in record.raw.decode()


def test_database_multiple_data_sources_and_child_pages_resume(monkeypatch):
    second = str(uuid4())

    def handler(r):
        path = r.url.path
        if path == f"/v1/databases/{DATABASE}":
            return {"data_sources": [{"id": SOURCE}, {"id": second}]}
        if path.endswith("/query"):
            return listing([page()])
        if path.startswith("/v1/pages/"):
            return page(path.rsplit("/", 1)[-1])
        if path == f"/v1/blocks/{PAGE}/children":
            return listing(
                [{"id": CHILD, "type": "child_page", "child_page": {"title": "nested"}}]
            )
        if path == f"/v1/blocks/{CHILD}/children":
            return listing([])
        pytest.fail(path)

    config = {"page_ids": [], "database_ids": [DATABASE], "include_children": True}
    state, records = None, []
    for _ in range(12):
        result = adapter(monkeypatch, handler, **config).read("backfill", state, 1)
        records.extend(result.records)
        state = result.cursor
        if not result.has_more:
            break
    assert {r.object_id for r in records} == {PAGE, CHILD}
    assert len(records) == 2
    assert not result.has_more


def test_revision_change_and_pagination_loop_fail_before_checkpoint(monkeypatch):
    calls = 0

    def handler(r):
        nonlocal calls
        if "/pages/" in r.url.path:
            calls += 1
            return page(
                last_edited_time=STAMP if calls == 1 else "2026-10-02T00:00:00Z"
            )
        return listing([])

    reader = adapter(monkeypatch, handler)
    with pytest.raises(AdapterError, match="source_changed_during_read"):
        reader.read("backfill")
    reader = adapter(
        monkeypatch,
        lambda r: page() if "/pages/" in r.url.path else listing([], "same"),
    )
    with pytest.raises(AdapterError, match="pagination_loop"):
        reader.read("backfill")


def test_comments_and_api_error_are_explicit(monkeypatch):
    def handler(r):
        if "/pages/" in r.url.path:
            return page()
        if r.url.path == "/v1/comments":
            return listing([{"rich_text": rt("评审意见")}])
        return listing([])

    reader = adapter(monkeypatch, handler, include_comments=True)
    result = reader.read("backfill")
    assert "评审意见" in result.records[0].text
    assert "page_comments_only" in result.warnings
    reader = adapter(
        monkeypatch, lambda r: httpx.Response(429, headers={"Retry-After": "30"})
    )
    with pytest.raises(AdapterError) as error:
        reader.read("backfill")
    assert error.value.retryable


def test_schema_and_unknown_types_preserve_raw(monkeypatch):
    def handler(r):
        return (
            page()
            if "/pages/" in r.url.path
            else listing(
                [{"id": BLOCK, "type": "unsupported", "unsupported": {"native": 4}}]
            )
        )

    record = adapter(monkeypatch, handler).read("backfill").records[0]
    assert json.loads(record.raw)["blocks"][0]["unsupported"] == {"native": 4}
    assert record.metadata["warnings"] == ["block_reference_not_expanded:unsupported"]
