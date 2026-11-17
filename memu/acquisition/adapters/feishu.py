"""Read-only Feishu Docx source with resumable folder and Wiki discovery.

The durable host owns checkpoint acceptance. A failed page or mixed document
revision raises before returning a candidate cursor, so it is retried unchanged.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any
from urllib.parse import quote

import httpx
from jsonschema import Draft202012Validator

from .base import AdapterError, Attachment, Page, Snapshot
from .http import SourceHTTP, environment_secret, identifier, json_bytes
from .oauth import FeishuOAuth, OAUTH_CONFIG_SCHEMA


CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "base_url": {"type": "string", "default": "https://open.feishu.cn/open-apis"},
        "access_token_ref": {
            "type": "string",
            "pattern": "^env:[A-Za-z_][A-Za-z0-9_]*$",
        },
        "oauth": OAUTH_CONFIG_SCHEMA,
        "principal_id": {"type": "string", "minLength": 1},
        "browse_accessible": {"type": "boolean"},
        "folder_tokens": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "document_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "wiki_spaces": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "revision_strategy": {"enum": ["fixed", "current"], "default": "current"},
        "download_media": {"type": "boolean", "default": True},
        "max_attachment_bytes": {
            "type": "integer",
            "minimum": 1,
            "maximum": 32 * 1024 * 1024,
        },
        "max_block_pages": {"type": "integer", "minimum": 1, "maximum": 1000},
        "allow_insecure_http": {"type": "boolean", "default": False},
    },
    "required": ["principal_id"],
    "oneOf": [{"required": ["access_token_ref"]}, {"required": ["oauth"]}],
    "additionalProperties": False,
    "anyOf": [
        {"required": [key], "properties": {key: {"minItems": 1}}}
        for key in ("folder_tokens", "document_ids", "wiki_spaces")
    ] + [{"required": ["browse_accessible"], "properties": {"browse_accessible": {"const": True}}}],
}

_TEXT_BLOCKS = {
    "page",
    "text",
    "bullet",
    "ordered",
    "code",
    "quote",
    "todo",
    "callout",
} | {f"heading{number}" for number in range(1, 10)}
_STRUCTURAL_BLOCKS = {
    "table",
    "table_cell",
    "grid",
    "grid_column",
    "divider",
    "iframe",
    "image",
    "file",
}


class FeishuAdapter:
    adapter_id = "feishu.documents"
    profile_id = "docx-v1"
    config_schema = CONFIG_SCHEMA

    def __init__(self, config: dict, client: httpx.Client | None = None) -> None:
        if list(Draft202012Validator(CONFIG_SCHEMA).iter_errors(config)):
            raise AdapterError("config_invalid")
        self.config = deepcopy(config)
        self.http = SourceHTTP(
            config.get("base_url", "https://open.feishu.cn/open-apis"),
            client=client,
            allow_insecure_http=config.get("allow_insecure_http", False),
        )
        binding = {
            key: config.get(key)
            for key in ("principal_id", "folder_tokens", "document_ids", "wiki_spaces")
        }
        binding["base_url"] = self.http.base_url
        self.binding = hashlib.sha256(json_bytes(binding)).hexdigest()
        self.oauth = (
            FeishuOAuth(config["oauth"], self.http, config["principal_id"])
            if "oauth" in config
            else None
        )
        self.verified_identity = False
        self.last_warnings: list[str] = []

    def close(self) -> None:
        self.http.close()

    def _headers(self) -> dict[str, str]:
        token = (
            self.oauth.access_token()
            if self.oauth
            else environment_secret(self.config["access_token_ref"])
        )
        return {"Authorization": "Bearer " + token}

    def _get(self, path: str, params: dict | None = None) -> tuple[dict, bytes]:
        envelope, raw = self.http.json(
            "GET", path, headers=self._headers(), params=params
        )
        code = envelope.get("code")
        if type(code) is not int:
            raise AdapterError("incompatible_schema")
        if code != 0:
            if code in (99991661, 99991663, 99991664, 99991668):
                error = "auth_required"
            elif code in (99991672, 99991679, 99991665, 1770032, 1770033):
                error = "source_access_denied"
            elif code in (99991400, 99991401):
                error = "throttled"
            else:
                error = "source_request_rejected"
            self.http.last_error = error
            raise AdapterError(error, retryable=error == "throttled")
        value = envelope.get("data")
        if not isinstance(value, dict):
            raise AdapterError("incompatible_schema")
        return value, raw

    def _verify_identity(self) -> None:
        data, _ = self._get("/authen/v1/user_info")
        if data.get("open_id") != self.config["principal_id"]:
            raise AdapterError("account_scope_mismatch")
        self.verified_identity = True

    def probe(self) -> dict:
        self._verify_identity()
        checks = []
        for folder in self.config.get("folder_tokens", []):
            self._get("/drive/v1/files", {"folder_token": folder, "page_size": 1})
            checks.append({"kind": "folder", "id": folder, "readable": True})
        for document in self.config.get("document_ids", []):
            self._document(document, {"type": "docx", "token": document})
            checks.append({"kind": "docx", "id": document, "readable": True})
        for space in self.config.get("wiki_spaces", []):
            self._get(
                f"/wiki/v2/spaces/{quote(space, safe='')}/nodes", {"page_size": 1}
            )
            checks.append({"kind": "wiki", "id": space, "readable": True})
        return {
            "adapter_id": self.adapter_id,
            "profile_id": self.profile_id,
            "state": "ready",
            "principal_id": self.config["principal_id"],
            "identity": "user",
            "checks": checks,
            "oauth": self.oauth.last_report
            if self.oauth
            else {"auth_protocol": "external_token"},
            "capabilities": {
                "discover": True,
                "backfill": "current_readable_versions",
                "poll": True,
                "reconcile": True,
                "subscribe": False,
                "read_only": True,
                "background_refresh": self.oauth.last_report["background_refresh"]
                if self.oauth
                else False,
                "attachments": "mixed",
            },
            "conditions": [
                "User access token supplied through credential reference or initialized OAuth broker",
                "Only selected folders, Docx documents and Wiki spaces",
                "Periodic full scans compensate for absent event subscriptions",
                "403/404 never imply deletion",
            ],
        }

    def health(self) -> dict:
        return self.http.health() | {
            "identity_verified": self.verified_identity,
            "warnings": self.last_warnings,
            "background_refresh": self.oauth.last_report["background_refresh"]
            if self.oauth
            else False,
            "event_subscription": "capability_unavailable",
        }

    def _initial_cursor(self, mode: str) -> dict:
        queue = [
            {"kind": "folder", "id": value, "page_token": None}
            for value in self.config.get("folder_tokens", [])
        ]
        queue += [
            {"kind": "wiki", "id": value, "parent": None, "page_token": None}
            for value in self.config.get("wiki_spaces", [])
        ]
        return {
            "version": 1,
            "binding": self.binding,
            "mode": mode,
            "queue": queue,
            "pending": [
                {"type": "docx", "token": value}
                for value in self.config.get("document_ids", [])
            ],
            "seen_directories": [],
            "seen_documents": [],
            "seen_pages": [],
            "warnings": [],
            "complete": False,
        }

    def read(self, mode: str, cursor: dict | None = None, limit: int = 50) -> Page:
        if mode not in ("discover", "backfill", "poll", "live", "reconcile"):
            raise AdapterError("capability_unavailable")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AdapterError("limit_invalid")
        # Every batch verifies the token's account, including after secret rotation.
        self._verify_identity()
        state = deepcopy(cursor) if cursor else self._initial_cursor(mode)
        if (
            state.get("binding") != self.binding
            or state.get("mode") != mode
            or state.get("version") != 1
        ):
            raise AdapterError("cursor_scope_mismatch")
        if state.get("complete"):
            state = self._initial_cursor(mode)
        records: list[Snapshot] = []
        while len(records) < limit and (state["pending"] or state["queue"]):
            if state["pending"]:
                entry = state["pending"].pop(0)
                record, warnings = self._entry(entry, state)
                state["warnings"] = sorted(set(state["warnings"] + warnings))
                if record is not None:
                    records.append(record)
            else:
                records.append(self._listing(state, limit))
            if len(state["seen_directories"]) + len(state["seen_documents"]) > 100000:
                raise AdapterError("inventory_budget_exceeded")
        complete = not state["pending"] and not state["queue"]
        state["complete"] = complete
        self.last_warnings = state["warnings"]
        return Page(
            records=records,
            cursor=state,
            has_more=not complete,
            scope_complete=complete and not state["warnings"],
            warnings=state["warnings"],
        )

    def _listing(self, state: dict, limit: int) -> Snapshot:
        location = state["queue"][0]
        requested_location = deepcopy(location)
        key = f"{location['kind']}:{location['id']}:{location.get('parent') or ''}"
        page_key = key + ":" + (location["page_token"] or "")
        if page_key in state["seen_pages"]:
            raise AdapterError("pagination_cycle")
        params: dict[str, Any] = {"page_size": min(limit, 50)}
        if location["page_token"]:
            params["page_token"] = location["page_token"]
        if location["kind"] == "folder":
            params["folder_token"] = location["id"]
            data, raw = self._get("/drive/v1/files", params)
            entries = data.get("files")
        else:
            if location.get("parent"):
                params["parent_node_token"] = location["parent"]
            data, raw = self._get(
                f"/wiki/v2/spaces/{quote(location['id'], safe='')}/nodes", params
            )
            entries = data.get("items")
        if not isinstance(entries, list) or any(
            not isinstance(item, dict) for item in entries
        ):
            raise AdapterError("incompatible_schema")
        has_more = data.get("has_more")
        if type(has_more) is not bool:
            raise AdapterError("incompatible_schema")
        next_token = data.get("next_page_token", data.get("page_token"))
        if has_more and (not isinstance(next_token, str) or not next_token):
            raise AdapterError("pagination_invalid")
        state["seen_pages"].append(page_key)
        for entry in entries:
            state["pending"].append(
                {
                    "entry": entry,
                    "location": deepcopy(location),
                    "wiki": location["kind"] == "wiki",
                }
            )
        if has_more:
            location["page_token"] = next_token
        else:
            state["queue"].pop(0)
            if key not in state["seen_directories"]:
                state["seen_directories"].append(key)
        return Snapshot(
            object_id="listing:" + hashlib.sha256(page_key.encode()).hexdigest(),
            raw=raw,
            metadata={
                "kind": "directory_listing",
                "location": requested_location,
                "complete_page": True,
            },
        )

    def _entry(self, pending: dict, state: dict) -> tuple[Snapshot | None, list[str]]:
        entry = pending.get("entry", pending)
        location = pending.get("location")
        warnings: list[str] = []
        if pending.get("wiki"):
            node = identifier(entry.get("node_token"))
            kind, token = entry.get("obj_type"), identifier(entry.get("obj_token"))
            if entry.get("has_child"):
                child = {
                    "kind": "wiki",
                    "id": location["id"],
                    "parent": node,
                    "page_token": None,
                }
                self._enqueue(state, child)
        else:
            kind, token = entry.get("type"), identifier(entry.get("token"))
            if kind == "shortcut":
                info = entry.get("shortcut_info", {})
                kind, token = (
                    info.get("target_type"),
                    identifier(info.get("target_token")),
                )
        if kind == "folder":
            self._enqueue(state, {"kind": "folder", "id": token, "page_token": None})
            return Snapshot(
                object_id=f"folder:{token}",
                raw=json_bytes(entry),
                metadata={"kind": "folder", "location": location},
            ), warnings
        if kind != "docx":
            warnings.append("unsupported_content_type")
            return Snapshot(
                object_id=f"{kind}:{token}",
                raw=json_bytes(entry),
                metadata={
                    "kind": kind,
                    "location": location,
                    "coverage": "unsupported_content_type",
                },
            ), warnings
        if token in state["seen_documents"]:
            # Preserve each shortcut/Wiki relation, while fetching a body once per scan.
            relation = {"entry": entry, "location": location, "document_id": token}
            return Snapshot(
                object_id="location:"
                + hashlib.sha256(json_bytes(relation)).hexdigest(),
                raw=json_bytes(relation),
                metadata={"kind": "location", "document_id": token},
            ), warnings
        record, warnings = self._document(token, entry)
        record.metadata["location"] = location
        state["seen_documents"].append(token)
        return record, warnings

    @staticmethod
    def _enqueue(state: dict, location: dict) -> None:
        key = f"{location['kind']}:{location['id']}:{location.get('parent') or ''}"
        queued = {
            (item["kind"], item["id"], item.get("parent")) for item in state["queue"]
        }
        if (
            key not in state["seen_directories"]
            and (location["kind"], location["id"], location.get("parent")) not in queued
        ):
            state["queue"].append(location)

    def _document(self, token: str, entry: dict) -> tuple[Snapshot, list[str]]:
        path = f"/docx/v1/documents/{quote(token, safe='')}"
        before, before_raw = self._get(path)
        document = before.get("document")
        if not isinstance(document, dict) or document.get("document_id") != token:
            raise AdapterError("incompatible_schema")
        revision = document.get("revision_id")
        if type(revision) is not int or revision < 0:
            raise AdapterError("incompatible_schema")
        strategy = self.config.get("revision_strategy", "current")
        pages: list[str] = []
        blocks: list[dict] = []
        page_token = None
        seen_tokens: set[str] = set()
        snapshot_bytes = 0
        for _ in range(self.config.get("max_block_pages", 100)):
            params = {
                "document_revision_id": revision if strategy == "fixed" else -1,
                "page_size": 500,
            }
            if page_token:
                params["page_token"] = page_token
            page, raw = self._get(path + "/blocks", params)
            items = page.get("items")
            if not isinstance(items, list) or any(
                not isinstance(item, dict) for item in items
            ):
                raise AdapterError("incompatible_schema")
            if (
                "document_revision_id" in page
                and page["document_revision_id"] != revision
            ):
                raise AdapterError("snapshot_changed", retryable=True)
            snapshot_bytes += len(raw)
            if snapshot_bytes > 32 * 1024 * 1024:
                raise AdapterError("document_budget_exceeded")
            pages.append(raw.decode("utf-8"))
            blocks.extend(items)
            if page.get("has_more") is False:
                break
            page_token = page.get("page_token")
            if (
                page.get("has_more") is not True
                or not isinstance(page_token, str)
                or not page_token
                or page_token in seen_tokens
            ):
                raise AdapterError("pagination_invalid")
            seen_tokens.add(page_token)
        else:
            raise AdapterError("document_budget_exceeded")
        after, after_raw = self._get(path)
        after_document = after.get("document", {})
        if after_document.get("document_id") != token:
            raise AdapterError("incompatible_schema")
        if strategy == "current" and after_document.get("revision_id") != revision:
            raise AdapterError("snapshot_changed", retryable=True)
        text, attachments, warnings = self._blocks(token, str(revision), blocks)
        snapshot = {
            "format": "feishu.docx.raw.v1",
            "entry": entry,
            "document_before": before_raw.decode("utf-8"),
            "block_pages": pages,
            "document_after": after_raw.decode("utf-8"),
            "revision_strategy": strategy,
        }
        return Snapshot(
            object_id=f"docx:{token}",
            raw=json_bytes(snapshot),
            text=text,
            revision=str(revision),
            metadata={
                "kind": "document",
                "document_id": token,
                "title": document.get("title"),
                "source_actor_id": None,
                "actor_role": "unknown",
                "warnings": warnings,
                "attachments_complete": all(
                    item.data is not None for item in attachments
                ),
            },
            attachments=attachments,
        ), warnings

    def _blocks(
        self, document: str, revision: str, blocks: list[dict]
    ) -> tuple[str, list[Attachment], list[str]]:
        lines: list[str] = []
        attachments: list[Attachment] = []
        warnings: list[str] = []
        for block in blocks:
            block_id = identifier(block.get("block_id"))
            recognized = False
            for kind in _TEXT_BLOCKS:
                section = block.get(kind)
                if not isinstance(section, dict):
                    continue
                recognized = True
                elements = section.get("elements", [])
                if not isinstance(elements, list):
                    raise AdapterError("incompatible_schema")
                pieces = []
                for element in elements:
                    if not isinstance(element, dict):
                        raise AdapterError("incompatible_schema")
                    text_run = element.get("text_run")
                    if not isinstance(text_run, dict):
                        warnings.append("text_projection_partial")
                        continue
                    content = text_run.get("content")
                    if not isinstance(content, str):
                        raise AdapterError("incompatible_schema")
                    pieces.append(content)
                text = "".join(pieces)
                if text:
                    lines.append(text)
            for role in ("image", "file"):
                media = block.get(role)
                if not isinstance(media, dict):
                    continue
                recognized = True
                file_token = media.get("token")
                if not isinstance(file_token, str) or not file_token:
                    warnings.append("media_reference_missing")
                    continue
                ref = {
                    "attachment_id": f"{block_id}:{file_token}",
                    "file_token": file_token,
                    "document_id": document,
                    "revision": revision,
                    "block_id": block_id,
                    "role": role,
                    "name": media.get("name"),
                    "scope_binding": self.binding,
                }
                attachment = Attachment(
                    ref["attachment_id"],
                    source_uri=f"feishu:media:{file_token}",
                    metadata=ref,
                )
                if self.config.get("download_media", True):
                    try:
                        attachment = self._download(ref)
                    except AdapterError as error:
                        attachment.metadata["error_code"] = str(error)
                        warnings.append("attachment_pending")
                else:
                    warnings.append("attachment_pending")
                attachments.append(attachment)
            if not recognized and not any(kind in block for kind in _STRUCTURAL_BLOCKS):
                warnings.append("unknown_block_type")
            if "iframe" in block:
                warnings.append("external_content_not_copied")
        return "\n".join(lines), attachments, sorted(set(warnings))

    def _download(self, reference: dict) -> Attachment:
        token = identifier(reference.get("file_token"))
        raw, mime = self.http.request(
            "GET",
            f"/drive/v1/medias/{quote(token, safe='')}/download",
            headers=self._headers(),
            max_bytes=self.config.get("max_attachment_bytes", 16 * 1024 * 1024),
        )
        return Attachment(
            reference["attachment_id"],
            data=raw,
            media_type=mime,
            source_uri=f"feishu:media:{token}",
            status="available",
            metadata=dict(reference),
        )

    def fetch_attachment(self, reference: dict) -> Attachment:
        # This endpoint accepts only refs emitted by an authorized document read.
        # The native host persists the reference with its source binding.
        identifier(reference.get("document_id"))
        identifier(reference.get("attachment_id"))
        if not reference.get("revision") or not reference.get("block_id"):
            raise AdapterError("attachment_reference_invalid")
        if reference.get("scope_binding") != self.binding:
            raise AdapterError("attachment_scope_mismatch")
        self._verify_identity()
        return self._download(reference)
