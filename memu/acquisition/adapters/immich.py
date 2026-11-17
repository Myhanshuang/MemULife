"""Immich metadata and asset references, pinned to an installed OpenAPI.

The server's search API is an inventory, not a durable change feed. Incremental
reads use fixed time windows with overlap, and reconciliation repeats inventory.
Only the host may commit the returned candidate window/checkpoint.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx
from jsonschema import Draft202012Validator

from .base import AdapterError, Attachment, Page, Snapshot
from .http import SourceHTTP, environment_secret, identifier, json_bytes


CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "base_url": {"type": "string"},
        "api_key_ref": {"type": "string", "pattern": "^env:[A-Za-z_][A-Za-z0-9_]*$"},
        "deployment_id": {"type": "string", "minLength": 1},
        "owner_id": {"type": "string", "minLength": 1},
        "schema_path": {"type": "string", "minLength": 1},
        "schema_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "server_version": {
            "type": "string",
            "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+(-rc\\.[0-9]+)?$",
        },
        "album_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "all_assets": {"type": "boolean", "default": False},
        "visibilities": {
            "type": "array",
            "items": {"enum": ["timeline", "archive", "hidden", "locked"]},
            "minItems": 1,
            "uniqueItems": True,
        },
        "include_trash": {"type": "boolean", "default": False},
        "original_mode": {"enum": ["copy", "reference"], "default": "reference"},
        "overlap_seconds": {"type": "integer", "minimum": 60, "maximum": 86400},
        "max_attachment_bytes": {
            "type": "integer",
            "minimum": 1,
            "maximum": 32 * 1024 * 1024,
        },
        "allow_insecure_http": {"type": "boolean", "default": False},
    },
    "required": [
        "base_url",
        "api_key_ref",
        "deployment_id",
        "owner_id",
        "schema_path",
        "schema_sha256",
        "server_version",
        "visibilities",
    ],
    "additionalProperties": False,
    "oneOf": [
        {
            "required": ["album_ids"],
            "properties": {
                "album_ids": {"minItems": 1},
                "all_assets": {"const": False},
            },
        },
        {
            "required": ["all_assets"],
            "properties": {"all_assets": {"const": True}, "album_ids": {"maxItems": 0}},
        },
    ],
}


class ImmichAdapter:
    adapter_id = "immich.assets"
    profile_id = "installed-openapi"
    config_schema = CONFIG_SCHEMA

    def __init__(
        self,
        config: dict,
        client: httpx.Client | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if list(Draft202012Validator(CONFIG_SCHEMA).iter_errors(config)):
            raise AdapterError("config_invalid")
        self.config = deepcopy(config)
        # Accept old bindings, but never copy originals (including deferred fetches).
        self.config["original_mode"] = "reference"
        self.http = SourceHTTP(
            config["base_url"],
            client=client,
            allow_insecure_http=config.get("allow_insecure_http", False),
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.schema = self._load_schema()
        self.search_schema = self._operation_schema("/search/metadata", "post")
        properties = self._resolve(self.search_schema).get("properties", {})
        self.structured = "filter" in properties and "cursor" in properties
        if (
            not self.structured
            and not {"updatedAfter", "updatedBefore", "page", "visibility"}
            <= properties.keys()
        ):
            raise AdapterError("incompatible_schema")
        for path in (
            "/server/version",
            "/users/me",
            "/assets/{id}",
        ):
            if "get" not in self.schema.get("paths", {}).get(path, {}):
                raise AdapterError("incompatible_schema")
        binding = {
            key: config.get(key)
            for key in (
                "deployment_id",
                "owner_id",
                "schema_sha256",
                "server_version",
                "album_ids",
                "all_assets",
                "visibilities",
                "include_trash",
                "original_mode",
            )
        }
        binding["base_url"] = self.http.base_url
        self.binding = hashlib.sha256(json_bytes(binding)).hexdigest()
        self.last_warnings: list[str] = []
        self.verified = False

    def _load_schema(self) -> dict:
        try:
            path = Path(self.config["schema_path"])
            if path.stat().st_size > 16 * 1024 * 1024:
                raise AdapterError("schema_too_large")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != self.config["schema_sha256"]:
                raise AdapterError("schema_checksum_mismatch")
            value = json.loads(raw)
        except (OSError, ValueError):
            raise AdapterError("schema_unavailable") from None
        if (
            not isinstance(value, dict)
            or value.get("info", {}).get("version") != self.config["server_version"]
        ):
            raise AdapterError("schema_version_mismatch")
        return value

    def _resolve(self, schema: dict) -> dict:
        reference = schema.get("$ref")
        if reference:
            if not reference.startswith("#/components/schemas/"):
                raise AdapterError("incompatible_schema")
            schema = (
                self.schema.get("components", {})
                .get("schemas", {})
                .get(reference.rsplit("/", 1)[-1])
            )
        if not isinstance(schema, dict):
            raise AdapterError("incompatible_schema")
        return schema

    def _operation_schema(self, path: str, method: str) -> dict:
        try:
            return self.schema["paths"][path][method]["requestBody"]["content"][
                "application/json"
            ]["schema"]
        except (KeyError, TypeError):
            raise AdapterError("incompatible_schema") from None

    def close(self) -> None:
        self.http.close()

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": environment_secret(self.config["api_key_ref"])}

    def _get(self, path: str) -> tuple[dict, bytes]:
        return self.http.json("GET", path, headers=self._headers())

    def _verify(self) -> None:
        version, _ = self._get("/server/version")
        if any(
            type(version.get(key)) is not int for key in ("major", "minor", "patch")
        ):
            raise AdapterError("incompatible_schema")
        current = ".".join(str(version[key]) for key in ("major", "minor", "patch"))
        if version.get("prerelease") is not None:
            if type(version["prerelease"]) is not int:
                raise AdapterError("incompatible_schema")
            current += f"-rc.{version['prerelease']}"
        if current != self.config["server_version"]:
            raise AdapterError("incompatible_server_version")
        owner, _ = self._get("/users/me")
        if owner.get("id") != self.config["owner_id"]:
            raise AdapterError("account_scope_mismatch")
        self.verified = True

    def probe(self) -> dict:
        self._verify()
        for visibility in self.config["visibilities"]:
            self._search(self._query(visibility, 1, None, None, None))
        return {
            "adapter_id": self.adapter_id,
            "profile_id": self.profile_id,
            "state": "ready",
            "server_version": self.config["server_version"],
            "schema_sha256": self.config["schema_sha256"],
            "deployment_id": self.config["deployment_id"],
            "owner_id": self.config["owner_id"],
            "pagination": "cursor" if self.structured else "page",
            "original_mode": self.config.get("original_mode", "copy"),
            "capabilities": {
                "discover": True,
                "backfill": True,
                "poll": True,
                "reconcile": True,
                "subscribe": False,
                "read_only": True,
                "phone_coverage": "unknown",
                "deletion": "no_missing_object_tombstones",
            },
            "conditions": [
                "Exact server and OpenAPI version required",
                "Own assets in selected scope only",
                "Fixed windows with overlap and periodic full reconciliation",
                "Webhook setup and smartphone backup queue are external",
                "Originals larger than the configured budget remain pending",
            ],
        }

    def health(self) -> dict:
        return self.http.health() | {
            "identity_verified": self.verified,
            "warnings": self.last_warnings,
            "phone_coverage": "unknown",
            "server_version": self.config["server_version"],
        }

    def _initial_cursor(self, mode: str, previous_upper: str | None = None) -> dict:
        upper = self.clock()
        if upper.tzinfo is None:
            raise AdapterError("clock_timezone_missing")
        lower = None
        if previous_upper and mode in ("poll", "live"):
            try:
                lower = (
                    datetime.fromisoformat(previous_upper)
                    - timedelta(seconds=self.config.get("overlap_seconds", 600))
                ).isoformat()
            except (ValueError, TypeError):
                raise AdapterError("cursor_invalid") from None
        return {
            "version": 1,
            "binding": self.binding,
            "mode": mode,
            "visibility_index": 0,
            "page": 1,
            "next_cursor": None,
            "lower": lower,
            "upper": upper.isoformat(),
            "last_complete_upper_bound": previous_upper,
            "pass": 1,
            "seen_pages": [],
            "warnings": [],
            "complete": False,
            "window_done": False,
            "pending": [],
        }

    def read(self, mode: str, cursor: dict | None = None, limit: int = 50) -> Page:
        if mode not in ("discover", "backfill", "poll", "live", "reconcile"):
            raise AdapterError("capability_unavailable")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AdapterError("limit_invalid")
        self._verify()
        state = deepcopy(cursor) if cursor else self._initial_cursor(mode)
        if (
            state.get("binding") != self.binding
            or state.get("mode") != mode
            or state.get("version") != 1
        ):
            raise AdapterError("cursor_scope_mismatch")
        if state.get("complete"):
            state = self._initial_cursor(mode, state["last_complete_upper_bound"])
        records: list[Snapshot] = []
        if not state["pending"] and not state["window_done"]:
            visibility = self.config["visibilities"][state["visibility_index"]]
            body = self._query(visibility, limit, state["lower"], state["upper"], state)
            page, page_raw = self._search(body)
            assets = page.get("assets")
            if not isinstance(assets, dict) or not isinstance(
                assets.get("items"), list
            ):
                raise AdapterError("incompatible_schema")
            if len(assets["items"]) > limit:
                raise AdapterError("page_budget_exceeded")
            for item in assets["items"]:
                if not isinstance(item, dict):
                    raise AdapterError("incompatible_schema")
                self._check_scope(item, visibility)
            listing_key = json_bytes(
                {
                    "binding": self.binding,
                    "visibility": visibility,
                    "page": state["page"],
                    "cursor": state["next_cursor"],
                }
            )
            records.append(
                Snapshot(
                    object_id="listing:" + hashlib.sha256(listing_key).hexdigest(),
                    raw=page_raw,
                    metadata={
                        "kind": "asset_listing",
                        "visibility": visibility,
                        "query": body,
                        "complete_page": True,
                    },
                )
            )
            state["pending"] = assets["items"]
            self._advance(state, assets)
        warnings: list[str] = []
        while state["pending"] and len(records) < limit:
            item = state["pending"].pop(0)
            record, gaps = self._asset(item)
            records.append(record)
            warnings.extend(gaps)
        state["complete"] = state["window_done"] and not state["pending"]
        if state["complete"]:
            state["last_complete_upper_bound"] = state["upper"]
        state["warnings"] = sorted(set(state["warnings"] + warnings))
        self.last_warnings = state["warnings"]
        return Page(
            records,
            state,
            has_more=not state["complete"],
            scope_complete=state["complete"] and not state["warnings"],
            warnings=state["warnings"],
        )

    def _advance(self, state: dict, assets: dict) -> None:
        expected_key = "nextCursor" if self.structured else "nextPage"
        if expected_key not in assets:
            raise AdapterError("incompatible_schema")
        next_value = assets[expected_key]
        if next_value is not None:
            if (
                not isinstance(next_value, str)
                or not next_value
                or next_value in state["seen_pages"]
            ):
                raise AdapterError("pagination_cycle")
            state["seen_pages"].append(next_value)
            if self.structured:
                state["next_cursor"] = next_value
            else:
                try:
                    next_page = int(next_value)
                except ValueError:
                    raise AdapterError("pagination_invalid") from None
                if next_page <= state["page"]:
                    raise AdapterError("pagination_cycle")
                state["page"] = next_page
        elif not self.structured and state["pass"] == 1:
            # Repeat the same fixed window to mitigate moving offset boundaries.
            state.update(page=1, next_cursor=None, seen_pages=[], **{"pass": 2})
        else:
            state["visibility_index"] += 1
            state.update(page=1, next_cursor=None, seen_pages=[], **{"pass": 1})
            state["window_done"] = state["visibility_index"] == len(
                self.config["visibilities"]
            )

    def _query(
        self,
        visibility: str,
        limit: int,
        lower: str | None,
        upper: str | None,
        state: dict | None,
        asset_id: str | None = None,
    ) -> dict:
        body: dict = {"size": limit, "withExif": True, "withStacked": True}
        albums = self.config.get("album_ids")
        if self.structured:
            filters: dict = {"visibility": {"eq": visibility}}
            if albums:
                filters["albumIds"] = {"any": albums}
            if asset_id:
                filters["id"] = {"eq": asset_id}
            if upper or lower:
                filters["updatedAt"] = {}
                if upper:
                    filters["updatedAt"]["lte"] = upper
                if lower:
                    filters["updatedAt"]["gte"] = lower
            if not self.config.get("include_trash", False):
                filters["trashedAt"] = {"eq": None}
            body["filter"] = filters
            if state and state["next_cursor"]:
                body["cursor"] = state["next_cursor"]
        else:
            body.update(
                visibility=visibility,
                withDeleted=self.config.get("include_trash", False),
                page=state["page"] if state else 1,
            )
            if albums:
                body["albumIds"] = albums
            if asset_id:
                body["id"] = asset_id
            if upper:
                body["updatedBefore"] = upper
            if lower:
                body["updatedAfter"] = lower
        return body

    def _search(self, body: dict) -> tuple[dict, bytes]:
        # Check fields against the supplied installed schema, rather than guessing
        # that a server still accepts a removed filter after an upgrade.
        properties = self._resolve(self.search_schema).get("properties", {})
        if not body.keys() <= properties.keys():
            raise AdapterError("incompatible_schema")
        if self.structured:
            filters = self._resolve(properties["filter"]).get("properties", {})
            if not body["filter"].keys() <= filters.keys():
                raise AdapterError("incompatible_schema")
        return self.http.json(
            "POST", "/search/metadata", headers=self._headers(), body=body
        )

    def _check_scope(self, item: dict, visibility: str) -> None:
        identifier(item.get("id"))
        if item.get("ownerId") != self.config["owner_id"]:
            raise AdapterError("account_scope_mismatch")
        if item.get("visibility") != visibility:
            raise AdapterError("asset_scope_mismatch")
        if item.get("isTrashed") and not self.config.get("include_trash", False):
            raise AdapterError("asset_scope_mismatch")

    def _asset(self, item: dict) -> tuple[Snapshot, list[str]]:
        asset_id = identifier(item.get("id"))
        details, raw = self._get(f"/assets/{quote(asset_id, safe='')}")
        self._check_scope(details, item["visibility"])
        if details.get("id") != asset_id:
            raise AdapterError("incompatible_schema")
        warnings: list[str] = []
        original_mode = "reference"
        ref = {
            "attachment_id": f"original:{asset_id}",
            "asset_id": asset_id,
            "role": "original",
            "owner_id": self.config["owner_id"],
            "deployment_id": self.config["deployment_id"],
            "source_checksum_sha1_base64": details.get("checksum"),
            "file_name": details.get("originalFileName"),
        }
        attachment = Attachment(
            ref["attachment_id"],
            source_uri=f"immich:asset:{asset_id}:original",
            metadata=ref,
        )
        attachment.status = "referenced"
        attachment.media_type = ("video/" if details.get("type") == "VIDEO" else "image/") + "*"
        attachment.metadata["reason"] = "reference_only"
        if not details.get("hasMetadata"):
            warnings.append("metadata_pending")
        # A trashed asset is retained with its source status. It is not a permanent
        # deletion, and phone-side "free space" never turns into a tombstone.
        metadata = {
            "kind": "asset",
            "deployment_id": self.config["deployment_id"],
            "owner_id": self.config["owner_id"],
            "asset_id": asset_id,
            "file_name": details.get("originalFileName"),
            "media_type": details.get("type"),
            "original_mode": original_mode,
            "original_copied": attachment.data is not None,
            "phone_coverage": "unknown",
            "source_status": "trashed"
            if details.get("isTrashed")
            else "offline"
            if details.get("isOffline")
            else "available",
            "live_photo_video_id": details.get("livePhotoVideoId"),
            "stack": details.get("stack"),
            "created_at": details.get("createdAt"),
            "taken_at": details.get("fileCreatedAt"),
            "local_datetime": details.get("localDateTime"),
            "updated_at": details.get("updatedAt"),
            "is_edited": details.get("isEdited"),
            "warnings": warnings,
        }
        if details.get("isEdited"):
            if "get" in self.schema.get("paths", {}).get("/assets/{id}/edits", {}):
                edits, edits_raw = self._get(
                    f"/assets/{quote(asset_id, safe='')}/edits"
                )
                metadata["edits"] = edits
            else:
                edits_raw = None
                warnings.append("edit_parameters_unavailable")
        else:
            edits_raw = None
        snapshot_raw = json_bytes(
            {
                "format": "immich.asset.raw.v1",
                "detail": raw.decode("utf-8"),
                "listing_item": item,
                "edits": edits_raw.decode("utf-8") if edits_raw else None,
            }
        )
        text = (
            details.get("exifInfo", {}).get("description")
            if isinstance(details.get("exifInfo"), dict)
            else None
        )
        if text is not None and not isinstance(text, str):
            raise AdapterError("incompatible_schema")
        text = "# Immich 资源引用\n\n" + json.dumps({
            "asset_id": asset_id, "文件名": details.get("originalFileName"),
            "类型": details.get("type"), "拍摄时间": details.get("fileCreatedAt"),
            "描述": text, "内容依据": "仅保存来源元数据和资产引用，未读取图片或视频原件",
        }, ensure_ascii=False)
        return Snapshot(
            object_id=f"asset:{asset_id}",
            raw=snapshot_raw,
            text=text,
            revision=None,  # updatedAt alone is not a trustworthy unique revision.
            occurred_at=None,
            metadata=metadata,
            attachments=[attachment],
        ), warnings

    def fetch_attachment(self, reference: dict) -> Attachment:
        raise AdapterError("capability_unavailable")
