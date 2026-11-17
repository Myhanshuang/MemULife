"""Configured-root labels use bounded metadata endpoints, never full imports."""

from uuid import UUID

import httpx
import pytest

from memu.acquisition.adapters.feishu import FeishuAdapter
from memu.acquisition.adapters.notion import NotionAdapter, API_VERSION
from memu.acquisition.sources import get_source


STAMP = "2026-10-28T00:00:00Z"


def identity(index):
    return str(UUID(int=index))


def notion_transport(monkeypatch, handler, config=None):
    calls = []
    monkeypatch.setenv("CATALOG_NOTION", "fixture-only")
    config = config or {"page_ids": [identity(1)]}
    values = {"workspace_id": "workspace", "token_ref": "env:CATALOG_NOTION"} | config

    def dispatch(request):
        calls.append(request.url.path)
        assert request.method == "GET"
        assert request.headers["Notion-Version"] == API_VERSION
        assert request.headers["Authorization"] == "Bearer fixture-only"
        if request.url.path == "/v1/users/me":
            return httpx.Response(200, json={"bot": {"workspace_id": "workspace"}})
        assert "/blocks" not in request.url.path
        assert "/properties/" not in request.url.path
        return handler(request)

    def factory(config):
        return NotionAdapter(
            config, httpx.Client(transport=httpx.MockTransport(dispatch))
        )

    monkeypatch.setattr("memu.acquisition.adapters.notion.NotionAdapter", factory)
    return {"config": values}, calls


def entity(kind, identifier, title):
    value = {
        "object": kind,
        "id": identifier,
        "last_edited_time": STAMP,
        "title": [{"plain_text": title}],
    }
    if kind == "page":
        value["properties"] = {
            "Name": {"type": "title", "title": value.pop("title")},
            "private": {
                "type": "rich_text",
                "rich_text": [{"plain_text": "DO NOT RETURN"}],
            },
        }
    return httpx.Response(200, json=value)


def test_notion_root_names_types_and_timestamps_without_expanding_scope(monkeypatch):
    def handler(request):
        collection, value = request.url.path.split("/")[-2:]
        return entity(
            {"pages": "page", "databases": "database", "data_sources": "data_source"}[
                collection
            ],
            value,
            collection + " title",
        )

    binding, calls = notion_transport(
        monkeypatch,
        handler,
        {
            "page_ids": [identity(1)],
            "database_ids": [identity(2)],
            "data_source_ids": [identity(3)],
        },
    )
    result = get_source("notion.workspace").candidates(binding)
    assert len(result["items"]) == 3 and result["next_cursor"] is None
    assert {row["type_label"] for row in result["items"]} == {
        "页面",
        "数据库",
        "数据源",
    }
    assert {row["name"] for row in result["items"]} == {
        "pages title",
        "databases title",
        "data_sources title",
    }
    assert all(row["updated_at"] == STAMP for row in result["items"])
    assert not result["content_persisted"] and "DO NOT RETURN" not in str(result)
    assert calls == [
        "/v1/users/me",
        "/v1/data_sources/" + identity(3),
        "/v1/databases/" + identity(2),
        "/v1/pages/" + identity(1),
    ]


def test_notion_bounds_root_window_and_continues_an_empty_search_page(monkeypatch):
    binding, calls = notion_transport(
        monkeypatch,
        lambda r: entity("page", r.url.path.rsplit("/", 1)[1], "other"),
        {"page_ids": [identity(i) for i in range(1, 26)]},
    )
    source = get_source("notion.workspace")
    result = source.candidates(binding, limit=100, query="missing")
    assert result["items"] == []
    assert result["scanned_count"] == 20
    assert result["next_cursor"] == "page_ids:" + identity(20)
    assert len(calls) == 21
    second = source.candidates(binding, cursor=result["next_cursor"], query="other")
    assert len(second["items"]) == 5 and second["next_cursor"] is None
    assert len(calls) == 27


@pytest.mark.parametrize(
    "status,code",
    [
        (403, "source_access_denied"),
        (404, "source_unavailable"),
        (429, "throttled"),
        (503, "source_offline"),
    ],
)
def test_lookup_errors_retain_configured_ids_without_inventing_names(
    monkeypatch, status, code
):
    binding, calls = notion_transport(
        monkeypatch,
        lambda r: httpx.Response(
            status, json={"message": "private upstream diagnostic"}
        ),
    )
    result = get_source("notion.workspace").candidates(binding)
    row = result["items"][0]
    assert row["name"] == identity(1) and row["title"] is None
    assert row["metadata_status"] == "unavailable" and row["error_code"] == code
    assert result["warnings"] == [code]
    assert "private upstream" not in str(result) and len(calls) == 2


def test_notion_mismatched_identity_and_malformed_title_are_not_accepted(monkeypatch):
    binding, _ = notion_transport(
        monkeypatch, lambda r: entity("page", identity(2), "foreign title")
    )
    result = get_source("notion.workspace").candidates(binding)
    assert result["items"][0]["error_code"] == "source_binding_mismatch"
    assert "foreign title" not in str(result)

    binding, _ = notion_transport(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={
                "object": "page",
                "id": identity(1),
                "properties": {
                    "title": {
                        "type": "title",
                        "title": [{"plain_text": {"bad": "value"}}],
                    }
                },
            },
        ),
    )
    result = get_source("notion.workspace").candidates(binding)
    assert result["items"][0]["error_code"] == "incompatible_schema"


def test_feishu_document_metadata_only_and_folder_id_fallback(monkeypatch):
    monkeypatch.setenv("CATALOG_FEISHU", "fixture-only")
    calls = []

    def dispatch(request):
        calls.append(request.url.path)
        assert request.method == "GET"
        if request.url.path.endswith("/authen/v1/user_info"):
            data = {"open_id": "principal"}
        elif request.url.path.endswith("/docx/v1/documents/chosen"):
            data = {
                "document": {
                    "document_id": "chosen",
                    "title": "Project notes",
                    "revision_id": 7,
                }
            }
        else:
            raise AssertionError(
                "unexpected source enumeration or body read: " + request.url.path
            )
        return httpx.Response(200, json={"code": 0, "data": data})

    monkeypatch.setattr(
        "memu.acquisition.adapters.feishu.FeishuAdapter",
        lambda config: FeishuAdapter(
            config, httpx.Client(transport=httpx.MockTransport(dispatch))
        ),
    )
    binding = {
        "config": {
            "principal_id": "principal",
            "access_token_ref": "env:CATALOG_FEISHU",
            "document_ids": ["chosen"],
            "folder_tokens": ["root"],
            "wiki_spaces": ["wiki"],
        }
    }
    result = get_source("feishu.documents").candidates(binding)
    assert len(calls) == 2 and len(result["items"]) == 3
    document = next(row for row in result["items"] if row["kind"] == "document_ids")
    assert document["name"] == "Project notes" and document["revision"] == "7"
    assert document["updated_at"] is None
    assert {
        row["name"] for row in result["items"] if row["kind"] != "document_ids"
    } == {"root", "wiki"}
    assert result["warnings"] == ["metadata_unavailable"]
    get_source("feishu.documents").candidates(binding, kind="folder_tokens")
    assert len(calls) == 2  # unsupported roots need no identity or list request


def test_immich_album_fallback_never_initializes_reader(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("album labels must not probe the paused Immich service")

    monkeypatch.setattr("memu.acquisition.adapters.immich.ImmichAdapter", forbidden)
    result = get_source("immich.assets").candidates(
        {"config": {"album_ids": ["album"]}}
    )
    assert result["items"][0]["name"] == "album"
    assert result["items"][0]["type_label"] == "相册"
    assert result["warnings"] == ["metadata_unavailable"]
