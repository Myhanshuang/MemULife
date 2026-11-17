"""Cloud-source scenarios use HTTP fixtures; none imply real-account support."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

import httpx
import pytest

from memu.acquisition.adapters.base import AdapterError
from memu.acquisition.adapters.feishu import FeishuAdapter
from memu.acquisition.adapters.http import SourceHTTP
from memu.acquisition.adapters.immich import ImmichAdapter


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("FEISHU_FIXTURE_TOKEN", "fixture-secret-not-a-real-token")
    monkeypatch.setenv("IMMICH_FIXTURE_KEY", "fixture-api-key-not-real")


def feishu_config(**overrides):
    return {
        "access_token_ref": "env:FEISHU_FIXTURE_TOKEN",
        "principal_id": "user-1",
        "folder_tokens": ["root"],
        **overrides,
    }


def ok(data):
    return httpx.Response(200, json={"code": 0, "data": data})


def document_response(doc="doc-1", revision=7):
    return ok(
        {"document": {"document_id": doc, "revision_id": revision, "title": "原文标题"}}
    )


def text_block(text="你好 🧪", block_id="b1"):
    return {
        "block_id": block_id,
        "block_type": 2,
        "text": {"elements": [{"text_run": {"content": text}}]},
    }


def feishu_handler(request):
    path = request.url.path
    if path.endswith("/authen/v1/user_info"):
        return ok({"open_id": "user-1"})
    if path.endswith("/drive/v1/files"):
        return ok(
            {
                "files": [{"token": "doc-1", "type": "docx", "parent_token": "root"}],
                "has_more": False,
            }
        )
    if path.endswith("/documents/doc-1"):
        return document_response()
    if path.endswith("/documents/doc-1/blocks"):
        return ok({"items": [text_block()], "has_more": False})
    raise AssertionError(request.url)


def finish(adapter, mode="poll", limit=50):
    cursor = None
    records = []
    for _ in range(20):
        page = adapter.read(mode, cursor, limit)
        assert len(page.records) <= limit
        records.extend(page.records)
        cursor = page.cursor
        if not page.has_more:
            return records, page
    raise AssertionError("scan did not finish")


def test_feishu_exact_raw_and_stable_doc_survive_page_boundaries():
    requests = []

    def handler(request):
        requests.append(request)
        return feishu_handler(request)

    adapter = FeishuAdapter(
        feishu_config(), httpx.Client(transport=httpx.MockTransport(handler))
    )
    records, page = finish(adapter, limit=1)
    document = next(record for record in records if record.object_id == "docx:doc-1")
    assert document.text == "你好 🧪"
    assert document.revision == "7"
    raw = json.loads(document.raw)
    assert json.loads(raw["block_pages"][0])["data"]["items"][0]["block_id"] == "b1"
    assert "fixture-secret" not in document.raw.decode()
    assert page.scope_complete is True
    assert all(
        request.headers["authorization"].startswith("Bearer ") for request in requests
    )


def test_feishu_recursive_pages_shortcut_and_wiki_real_doc_identity():
    document_gets = []

    def handler(request):
        path = request.url.path
        if path.endswith("/drive/v1/files"):
            folder = request.url.params["folder_token"]
            if folder == "root" and "page_token" not in request.url.params:
                return ok(
                    {
                        "files": [{"token": "nested", "type": "folder"}],
                        "has_more": True,
                        "next_page_token": "second",
                    }
                )
            if folder == "root":
                assert request.url.params["page_token"] == "second"
                return ok(
                    {
                        "files": [
                            {
                                "token": "shortcut-1",
                                "type": "shortcut",
                                "shortcut_info": {
                                    "target_type": "docx",
                                    "target_token": "doc-1",
                                },
                            }
                        ],
                        "has_more": False,
                    }
                )
            return ok(
                {"files": [{"token": "sheet-1", "type": "sheet"}], "has_more": False}
            )
        if "/wiki/v2/" in path:
            return ok(
                {
                    "items": [
                        {
                            "node_token": "wiki-node",
                            "obj_type": "docx",
                            "obj_token": "doc-1",
                            "has_child": False,
                        }
                    ],
                    "has_more": False,
                }
            )
        if path.endswith("/documents/doc-1"):
            document_gets.append(path)
        return feishu_handler(request)

    adapter = FeishuAdapter(
        feishu_config(wiki_spaces=["space-1"]),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    records, page = finish(adapter, limit=2)
    assert sum(record.object_id == "docx:doc-1" for record in records) == 1
    assert len(document_gets) == 2  # before/after one body, not one fetch per shortcut
    assert any(
        record.metadata.get("document_id") == "doc-1"
        and record.metadata["kind"] == "location"
        for record in records
    )
    assert "unsupported_content_type" in page.warnings
    assert not page.scope_complete


def test_feishu_mixed_revision_does_not_advance_input_cursor():
    reads = 0

    def handler(request):
        nonlocal reads
        if request.url.path.endswith("/documents/doc-1"):
            reads += 1
            return document_response(revision=7 if reads == 1 else 8)
        return feishu_handler(request)

    adapter = FeishuAdapter(
        feishu_config(), httpx.Client(transport=httpx.MockTransport(handler))
    )
    first = adapter.read("poll", None, 1)
    before = deepcopy(first.cursor)
    with pytest.raises(AdapterError, match="snapshot_changed") as error:
        adapter.read("poll", first.cursor, 1)
    assert error.value.retryable
    assert first.cursor == before


def test_feishu_fixed_revision_is_requested_for_all_pages():
    def handler(request):
        if request.url.path.endswith("/blocks"):
            assert request.url.params["document_revision_id"] == "7"
            if "page_token" not in request.url.params:
                return ok(
                    {
                        "items": [text_block("第一段")],
                        "has_more": True,
                        "page_token": "more",
                    }
                )
            return ok({"items": [text_block("第二段", "b2")], "has_more": False})
        return feishu_handler(request)

    adapter = FeishuAdapter(
        feishu_config(revision_strategy="fixed"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    records, _ = finish(adapter)
    assert next(r for r in records if r.text).text == "第一段\n第二段"


@pytest.mark.parametrize(
    "status, code",
    [
        (401, "auth_required"),
        (403, "source_access_denied"),
        (404, "source_unavailable"),
        (429, "throttled"),
        (503, "source_offline"),
    ],
)
def test_cloud_errors_are_classified_without_payload_or_false_tombstone(status, code):
    secret = "fixture-secret-must-not-appear"

    def handler(request):
        return httpx.Response(
            status, json={"error": secret}, headers={"Retry-After": "120"}
        )

    adapter = FeishuAdapter(
        feishu_config(), httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(AdapterError) as error:
        adapter.read("poll")
    assert error.value.code == code
    assert secret not in str(error.value)
    if status == 429:
        assert adapter.health()["retry_after_seconds"] == 120


def test_feishu_account_change_and_cursor_rebinding_fail_closed():
    adapter = FeishuAdapter(
        feishu_config(principal_id="other"),
        httpx.Client(transport=httpx.MockTransport(feishu_handler)),
    )
    with pytest.raises(AdapterError, match="account_scope_mismatch"):
        adapter.probe()
    adapter = FeishuAdapter(
        feishu_config(), httpx.Client(transport=httpx.MockTransport(feishu_handler))
    )
    cursor = adapter.read("poll", None, 1).cursor
    cursor["binding"] = "another-scope"
    with pytest.raises(AdapterError, match="cursor_scope_mismatch"):
        adapter.read("poll", cursor)


def test_feishu_attachment_denial_keeps_text_and_reports_gap():
    def handler(request):
        if request.url.path.endswith("/blocks"):
            return ok(
                {
                    "items": [
                        text_block(),
                        {
                            "block_id": "img",
                            "block_type": 27,
                            "image": {"token": "media-1"},
                        },
                    ],
                    "has_more": False,
                }
            )
        if "/medias/" in request.url.path:
            return httpx.Response(403)
        return feishu_handler(request)

    adapter = FeishuAdapter(
        feishu_config(), httpx.Client(transport=httpx.MockTransport(handler))
    )
    records, page = finish(adapter)
    doc = next(record for record in records if record.text)
    assert doc.text == "你好 🧪"
    assert doc.attachments[0].status == "pending"
    assert doc.attachments[0].metadata["error_code"] == "source_access_denied"
    assert "attachment_pending" in page.warnings


def immich_schema(structured=True):
    fields = [
        "size",
        "withExif",
        "withStacked",
        "id",
        "albumIds",
        "visibility",
        "page",
        "updatedAfter",
        "updatedBefore",
        "withDeleted",
    ]
    properties = {field: {} for field in fields}
    if structured:
        properties.update(
            filter={"$ref": "#/components/schemas/SearchFilter"},
            cursor={"type": "string"},
        )
    return {
        "openapi": "3.0.0",
        "info": {"version": "3.2.0" if structured else "2.6.1"},
        "paths": {
            "/search/metadata": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/MetadataSearchDto"
                                }
                            }
                        }
                    }
                }
            }
        }
        | {
            path: {"get": {}}
            for path in (
                "/server/version",
                "/users/me",
                "/assets/{id}",
                "/assets/{id}/original",
            )
        },
        "components": {
            "schemas": {
                "MetadataSearchDto": {"type": "object", "properties": properties},
                "SearchFilter": {
                    "type": "object",
                    "properties": {
                        field: {}
                        for field in (
                            "albumIds",
                            "id",
                            "visibility",
                            "updatedAt",
                            "trashedAt",
                        )
                    },
                },
            }
        },
    }


def immich_config(tmp_path, structured=True, **overrides):
    schema = immich_schema(structured)
    raw = json.dumps(schema).encode()
    path = tmp_path / "immich-openapi.json"
    path.write_bytes(raw)
    return {
        "base_url": "https://photos.example/api",
        "api_key_ref": "env:IMMICH_FIXTURE_KEY",
        "deployment_id": "server-1",
        "owner_id": "owner-1",
        "schema_path": str(path),
        "schema_sha256": hashlib.sha256(raw).hexdigest(),
        "server_version": schema["info"]["version"],
        "album_ids": ["album-1"],
        "visibilities": ["timeline"],
        **overrides,
    }


def asset(asset_id="asset-1", **overrides):
    return {
        "id": asset_id,
        "ownerId": "owner-1",
        "visibility": "timeline",
        "isTrashed": False,
        "isOffline": False,
        "hasMetadata": True,
        "originalFileName": "照片.jpg",
        "createdAt": "2026-10-21T08:00:00Z",
        "updatedAt": "2026-10-21T08:00:00Z",
        "fileCreatedAt": "2019-01-02T03:04:05Z",
        "exifInfo": {"description": "旅行照片"},
        "livePhotoVideoId": "video-1",
        **overrides,
    }


def immich_handler(request):
    path = request.url.path
    if path.endswith("/server/version"):
        return httpx.Response(200, json={"major": 3, "minor": 2, "patch": 0})
    if path.endswith("/users/me"):
        return httpx.Response(200, json={"id": "owner-1"})
    if path.endswith("/search/metadata"):
        return httpx.Response(
            200, json={"assets": {"items": [asset()], "nextCursor": None}}
        )
    if path.endswith("/assets/asset-1"):
        return httpx.Response(200, json=asset())
    if path.endswith("/assets/asset-1/original"):
        return httpx.Response(
            200, content=b"original-photo-bytes", headers={"Content-Type": "image/jpeg"}
        )
    raise AssertionError(request.url)


def test_immich_pinned_schema_reference_and_three_time_axes(tmp_path):
    adapter = ImmichAdapter(
        immich_config(tmp_path),
        httpx.Client(transport=httpx.MockTransport(immich_handler)),
    )
    records, page = finish(adapter, limit=1)
    photo = next(record for record in records if record.object_id == "asset:asset-1")
    assert photo.attachments[0].data is None
    assert photo.attachments[0].media_type == "image/*"
    assert photo.metadata["taken_at"].startswith("2019")
    assert photo.metadata["created_at"].startswith("2026")
    assert photo.metadata["live_photo_video_id"] == "video-1"
    assert photo.occurred_at is None
    assert page.scope_complete
    assert "fixture-api-key" not in photo.raw.decode()


def test_immich_window_advances_only_after_last_asset_and_overlaps(tmp_path):
    calls = []
    now = datetime(2026, 10, 22, 1, tzinfo=timezone.utc)

    def handler(request):
        if request.url.path.endswith("/search/metadata"):
            calls.append(json.loads(request.content))
        return immich_handler(request)

    adapter = ImmichAdapter(
        immich_config(tmp_path),
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: now,
    )
    first = adapter.read("poll", None, 1)
    assert first.cursor["last_complete_upper_bound"] is None
    assert first.has_more and len(first.cursor["pending"]) == 1
    second = adapter.read("poll", first.cursor, 1)
    assert second.cursor["last_complete_upper_bound"] == now.isoformat()
    assert not second.has_more
    now = datetime(2026, 10, 22, 2, tzinfo=timezone.utc)
    adapter.read("poll", second.cursor, 1)
    assert calls[-1]["filter"]["updatedAt"] == {
        "gte": "2026-10-22T00:50:00+00:00",
        "lte": now.isoformat(),
    }
    assert "takenAfter" not in calls[-1]


def test_immich_multiple_pages_keep_fixed_window_and_limit(tmp_path):
    seen = []

    def handler(request):
        if request.url.path.endswith("/search/metadata"):
            body = json.loads(request.content)
            seen.append(body)
            if "cursor" not in body:
                return httpx.Response(
                    200,
                    json={"assets": {"items": [asset()], "nextCursor": "next-opaque"}},
                )
            assert body["cursor"] == "next-opaque"
            return httpx.Response(
                200, json={"assets": {"items": [asset("asset-2")], "nextCursor": None}}
            )
        if request.url.path.endswith("/assets/asset-2"):
            return httpx.Response(200, json=asset("asset-2"))
        if request.url.path.endswith("/assets/asset-2/original"):
            return httpx.Response(200, content=b"other")
        return immich_handler(request)

    adapter = ImmichAdapter(
        immich_config(tmp_path), httpx.Client(transport=httpx.MockTransport(handler))
    )
    records, _ = finish(adapter, limit=1)
    assert {r.object_id for r in records if r.text} == {
        "asset:asset-1",
        "asset:asset-2",
    }
    assert seen[0]["filter"]["updatedAt"] == seen[1]["filter"]["updatedAt"]


def test_immich_offset_fallback_repeats_window_before_advancing(tmp_path):
    pages = []

    def handler(request):
        if request.url.path.endswith("/server/version"):
            return httpx.Response(200, json={"major": 2, "minor": 6, "patch": 1})
        if request.url.path.endswith("/search/metadata"):
            body = json.loads(request.content)
            pages.append(body)
            return httpx.Response(
                200,
                json={
                    "assets": {
                        "items": [],
                        "nextPage": "2" if body["page"] == 1 else None,
                    }
                },
            )
        return immich_handler(request)

    adapter = ImmichAdapter(
        immich_config(tmp_path, structured=False),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    _, page = finish(adapter)
    assert [body["page"] for body in pages] == [1, 2, 1, 2]
    assert len({body["updatedBefore"] for body in pages}) == 1
    assert page.cursor["last_complete_upper_bound"] is not None


@pytest.mark.parametrize("mode", ["copy", "reference"])
def test_immich_reference_mode_never_claims_original_copied(tmp_path, mode):
    def handler(request):
        assert not request.url.path.endswith("/original")
        return immich_handler(request)

    adapter = ImmichAdapter(
        immich_config(tmp_path, original_mode=mode),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    records, _ = finish(adapter)
    photo = next(record for record in records if record.text)
    assert not photo.metadata["original_copied"]
    assert photo.attachments[0].data is None
    assert photo.attachments[0].status == "referenced"


def test_immich_reference_does_not_need_original_endpoint(tmp_path):
    def handler(request):
        if request.url.path.endswith("/original"):
            raise httpx.ReadError("contains secret URL", request=request)
        return immich_handler(request)

    adapter = ImmichAdapter(
        immich_config(tmp_path), httpx.Client(transport=httpx.MockTransport(handler))
    )
    records, page = finish(adapter)
    photo = next(record for record in records if record.text)
    assert photo.attachments[0].status == "referenced"
    assert "original_pending" not in page.warnings
    assert page.scope_complete


def test_immich_wrong_version_schema_or_owner_are_never_silently_accepted(tmp_path):
    config = immich_config(tmp_path)
    bad_schema = config | {"schema_sha256": "0" * 64}
    with pytest.raises(AdapterError, match="schema_checksum_mismatch"):
        ImmichAdapter(bad_schema)

    def handler(request):
        if request.url.path.endswith("/server/version"):
            return httpx.Response(200, json={"major": 4, "minor": 0, "patch": 0})
        return immich_handler(request)

    adapter = ImmichAdapter(
        config, httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(AdapterError, match="incompatible_server_version"):
        adapter.read("poll")
    adapter = ImmichAdapter(
        config | {"owner_id": "someone-else"},
        httpx.Client(transport=httpx.MockTransport(immich_handler)),
    )
    with pytest.raises(AdapterError, match="account_scope_mismatch"):
        adapter.read("poll")


def test_immich_mismatched_or_shared_owner_never_enters_spool(tmp_path):
    def handler(request):
        if request.url.path.endswith("/search/metadata"):
            return httpx.Response(
                200,
                json={
                    "assets": {
                        "items": [asset(ownerId="shared-owner")],
                        "nextCursor": None,
                    }
                },
            )
        return immich_handler(request)

    adapter = ImmichAdapter(
        immich_config(tmp_path), httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(AdapterError, match="account_scope_mismatch"):
        adapter.read("poll")


def test_immich_missing_resource_is_not_a_tombstone(tmp_path):
    def handler(request):
        if request.url.path.endswith("/assets/asset-1"):
            return httpx.Response(404)
        return immich_handler(request)

    adapter = ImmichAdapter(
        immich_config(tmp_path), httpx.Client(transport=httpx.MockTransport(handler))
    )
    first = adapter.read("poll", None, 1)
    cursor = deepcopy(first.cursor)
    with pytest.raises(AdapterError, match="source_unavailable"):
        adapter.read("poll", first.cursor, 1)
    assert first.cursor == cursor


def test_immich_deferred_original_download_is_disabled(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/search/metadata"):
            body = json.loads(request.content)
            assert body["filter"]["albumIds"] == {"any": ["album-1"]}
            assert body["filter"]["id"] == {"eq": "asset-1"}
            return httpx.Response(
                200, json={"assets": {"items": [], "nextCursor": None}}
            )
        return immich_handler(request)

    adapter = ImmichAdapter(
        immich_config(tmp_path), httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(AdapterError, match="capability_unavailable"):
        adapter.fetch_attachment(
            {
                "attachment_id": "original:asset-1",
                "asset_id": "asset-1",
                "role": "original",
                "owner_id": "owner-1",
                "deployment_id": "server-1",
            }
        )
    assert not any(request.url.path.endswith("/original") for request in requests)


def test_http_does_not_follow_cross_origin_redirect_or_accept_oversized_response():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://attacker.example/"})

    http = SourceHTTP(
        "https://source.example",
        client=httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ),
    )
    with pytest.raises(AdapterError, match="redirect_refused"):
        http.request("GET", "/data", headers={"Authorization": "Bearer private"})
    http = SourceHTTP(
        "https://source.example",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"12345")
            )
        ),
        max_bytes=4,
    )
    with pytest.raises(AdapterError, match="response_too_large"):
        http.request("GET", "/data", headers={})


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example",
        "https://user:secret@example.test",
        "https://example.test?token=secret",
        "file:///tmp/a",
    ],
)
def test_http_rejects_implicit_credentials_and_unsafe_url(url):
    with pytest.raises(AdapterError, match="source_url_invalid"):
        SourceHTTP(url)
