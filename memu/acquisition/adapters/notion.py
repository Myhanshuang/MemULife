"""Read-only Notion pages and data sources, pinned to a versioned API.

Explicit roots only. Relations, mentions and linked databases never expand scope.
A page and all its blocks/properties form one revision-checked snapshot.
"""

from copy import deepcopy
from urllib.parse import quote, unquote
from uuid import UUID

from jsonschema import Draft202012Validator

from .base import AdapterError, Attachment, Page, Snapshot
from .http import SourceHTTP, environment_secret, json_bytes

API_VERSION = "2026-03-11"
IDS = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
    "uniqueItems": True,
    "maxItems": 200,
}
CONFIG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["token_ref"],
    "allOf": [{"anyOf": [{"required": ["workspace_id"]}, {"required": ["bot_id"]}]}],
    "properties": {
        "token_ref": {"type": "string", "pattern": "^env:[A-Za-z_][A-Za-z0-9_]*$"},
        "workspace_id": {"type": "string", "minLength": 1},
        "bot_id": {"type": "string", "minLength": 1},
        "browse_accessible": {"type": "boolean"},
        "page_ids": IDS,
        "database_ids": IDS,
        "data_source_ids": IDS,
        "include_children": {"type": "boolean"},
        "include_comments": {"type": "boolean"},
        "max_requests": {"type": "integer", "minimum": 5, "maximum": 200},
    },
    "anyOf": [
        {"required": [k], "properties": {k: {"minItems": 1}}}
        for k in ("page_ids", "database_ids", "data_source_ids")
    ] + [{"required": ["browse_accessible"], "properties": {"browse_accessible": {"const": True}}}],
}


def native_id(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise AdapterError("notion_id_invalid") from None


def rich_text(values):
    return "".join(
        v.get("plain_text", v.get("text", {}).get("content", ""))
        for v in values
        if isinstance(v, dict)
    )


def render_value(value):
    if isinstance(value, list):
        return ", ".join(render_value(v) for v in value)
    if isinstance(value, dict):
        if "plain_text" in value or "text" in value:
            return rich_text([value])
        kind = value.get("type")
        if kind and kind in value:
            return render_value(value[kind])
        return str(
            value.get("name")
            or value.get("start")
            or value.get("id")
            or json_bytes(value).decode()
        )
    return "" if value is None else str(value)


class NotionAdapter:
    adapter_id = "notion.workspace"
    config_schema = CONFIG_SCHEMA

    def __init__(self, config, client=None):
        if list(Draft202012Validator(CONFIG_SCHEMA).iter_errors(config)):
            raise AdapterError("config_invalid")
        self.config = deepcopy(config)
        self.http = SourceHTTP("https://api.notion.com/v1", client=client)
        self.requests = 0
        self.bytes_received = 0

    def close(self):
        self.http.close()

    def request(self, method, path, **kwargs):
        self.requests += 1
        if self.requests > self.config.get("max_requests", 150):
            raise AdapterError("notion_page_budget_exceeded")
        value, raw = self.http.json(
            method,
            path,
            headers={
                "Authorization": "Bearer "
                + environment_secret(self.config["token_ref"]),
                "Notion-Version": API_VERSION,
            },
            **kwargs,
        )
        self.bytes_received += len(raw)
        if self.bytes_received > 32 * 1024 * 1024:
            raise AdapterError("notion_page_budget_exceeded")
        return value

    def probe(self):
        who = self.request("GET", "/users/me")
        if ((self.config.get('bot_id') and who.get('id') != self.config['bot_id']) or
                (self.config.get('workspace_id') and who.get('bot', {}).get('workspace_id') != self.config['workspace_id'])):
            raise AdapterError("account_binding_mismatch")
        return {
            "adapter_id": self.adapter_id,
            "status": "ready",
            "api_version": API_VERSION,
            "read_only": True,
        }

    def health(self):
        return {"status": self.http.last_error or "ready", "read_only": True}

    def listing(self, path, params=None):
        cursor, seen, result = None, set(), []
        while True:
            value = self.request(
                "GET",
                path,
                params=(params or {})
                | {"page_size": 100}
                | ({"start_cursor": cursor} if cursor else {}),
            )
            if not isinstance(value.get("results"), list):
                raise AdapterError("incompatible_schema")
            result.extend(value["results"])
            if not value.get("has_more"):
                return result
            cursor = value.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise AdapterError("pagination_loop")
            seen.add(cursor)

    def _page(self, identity, state):
        guard = getattr(self, "selection_guard", None)
        if guard:
            guard(identity)
        page = self.request("GET", "/pages/" + identity)
        if native_id(page.get("id")) != identity:
            raise AdapterError("source_binding_mismatch")
        stamp = page.get("last_edited_time")
        if not isinstance(stamp, str):
            raise AdapterError("incompatible_schema")
        if page.get("in_trash") or page.get("archived"):
            if guard:
                guard(identity)
            return Snapshot(
                identity,
                json_bytes({"page": page}),
                deleted=True,
                revision=stamp,
                occurred_at=stamp,
                metadata={"warnings": [], "reason": "deleted"},
            )
        raw = {"page": page, "blocks": [], "properties": {}, "comments": []}
        lines, attachments, warnings = [], [], []
        for name, prop in page.get("properties", {}).items():
            kind = prop.get("type")
            if kind in {"title", "rich_text", "relation", "people"}:
                # Page retrieval truncates some properties. Follow the property API,
                # but retain related IDs without retrieving related pages.
                pid = quote(unquote(prop["id"]), safe="")
                values = self.listing(f"/pages/{identity}/properties/{pid}")
                raw["properties"][name] = values
                rendered = render_value(values)
            elif kind == "rollup":
                # Rollup pagination has aggregation semantics; preserve raw and
                # disclose incompleteness instead of inventing a final value.
                rendered = render_value(prop)
                warnings.append("rollup_value_not_expanded")
            else:
                rendered = render_value(prop)
            lines.append(f"{name}: {rendered}")
        pending = [(identity, 0, True)]
        visited = set()
        while pending:
            node, depth, fetch = pending.pop()
            if depth > 32:
                raise AdapterError("notion_block_cycle_or_depth")
            if fetch:
                if node in visited:
                    raise AdapterError("notion_block_cycle_or_depth")
                visited.add(node)
                blocks = self.listing(f"/blocks/{node}/children")
                pending.extend((block, depth, False) for block in reversed(blocks))
                continue
            block = node
            raw["blocks"].append(block)
            kind = block.get("type", "unsupported")
            content = block.get(kind, {})
            line = rich_text(content.get("rich_text", []))
            if kind == "table_row":
                line = " | ".join(rich_text(cell) for cell in content.get("cells", []))
            elif kind == "to_do":
                line = ("[x] " if content.get("checked") else "[ ] ") + line
            elif kind in {"child_page", "child_database"}:
                line = content.get("title", "")
                if kind == "child_page" and self.config.get("include_children", False):
                    child = native_id(block["id"])
                    if child not in state["seen_pages"] and child not in state["pages"]:
                        state["pages"].append(child)
            elif kind in {"image", "file", "pdf", "video", "audio"}:
                reference = content.get(content.get("type"), {}).get("url")
                if reference:
                    attachments.append(
                        Attachment(
                            str(block["id"]),
                            source_uri=reference,
                            status="pending",
                            metadata={
                                "expires_at": content.get("file", {}).get("expiry_time")
                            },
                        )
                    )
                line = (
                    content.get("name", kind)
                    + " "
                    + rich_text(content.get("caption", []))
                )
            elif kind in {"bookmark", "embed", "link_preview"}:
                line = content.get("url", "")
            elif kind == "equation":
                line = content.get("expression", "")
            elif kind in {"unsupported", "synced_block", "link_to_page"}:
                warnings.append("block_reference_not_expanded:" + kind)
            if line:
                lines.append("  " * depth + line)
            if block.get("has_children") and kind not in {
                "child_page",
                "child_database",
                "synced_block",
                "link_to_page",
            }:
                pending.append((native_id(block["id"]), depth + 1, True))
        if self.config.get("include_comments", False):
            raw["comments"] = self.listing("/comments", {"block_id": identity})
            lines.extend(
                "评论: " + rich_text(c.get("rich_text", [])) for c in raw["comments"]
            )
            warnings.append("page_comments_only")
        after = self.request("GET", "/pages/" + identity)
        if (
            after.get("last_edited_time") != stamp
            or after.get("in_trash") != page.get("in_trash")
            or after.get("archived") != page.get("archived")
        ):
            raise AdapterError("source_changed_during_read", True)
        if guard:
            guard(identity)
        encoded = json_bytes(raw)
        if len(encoded) > 16 * 1024 * 1024:
            raise AdapterError("notion_page_budget_exceeded")
        record = Snapshot(
            identity,
            encoded,
            "\n".join(lines),
            revision=stamp,
            deleted=bool(page.get("in_trash") or page.get("archived")),
            occurred_at=stamp,
            attachments=attachments,
            metadata={
                "title": lines[0] if lines else identity,
                "source_uri": page.get("url"),
                **{key: self.config[key] for key in ("workspace_id", "bot_id") if key in self.config},
                "warnings": sorted(set(warnings)),
            },
        )
        return record

    def read(self, mode, cursor=None, limit=50):
        if mode not in {"live", "poll", "backfill", "reconcile", "discover"}:
            raise AdapterError("capability_unavailable")
        self.requests = 0
        self.bytes_received = 0
        state = deepcopy(cursor or {})
        binding = {key: self.config[key] for key in ("workspace_id", "bot_id") if key in self.config}
        if any(state.get(key) not in {None, binding.get(key)} for key in ("workspace_id", "bot_id")):
            raise AdapterError("account_binding_mismatch")
        if not state or state.get("complete"):
            self.probe()
            state = {
                **binding,
                "pages": [native_id(i) for i in self.config.get("page_ids", [])],
                "databases": [
                    native_id(i) for i in self.config.get("database_ids", [])
                ],
                "sources": [
                    native_id(i) for i in self.config.get("data_source_ids", [])
                ],
                "seen_pages": [],
                "seen_sources": [],
            }
        if state["pages"]:
            identity = state["pages"].pop(0)
            if identity in state["seen_pages"]:
                return Page([], state, True)
            record = self._page(identity, state)
            state["seen_pages"].append(identity)
            records, warnings = [record], record.metadata["warnings"]
        elif state["databases"]:
            identity = state["databases"].pop(0)
            guard = getattr(self, "selection_container_guard", None)
            if guard:
                guard("database_ids:" + identity)
            value = self.request("GET", "/databases/" + identity)
            if guard:
                guard("database_ids:" + identity)
            sources = value.get("data_sources")
            if not isinstance(sources, list):
                raise AdapterError("incompatible_schema")
            state["sources"].extend(native_id(s["id"]) for s in sources)
            records, warnings = [], []
        elif state["sources"]:
            identity = state["sources"][0]
            if identity in state["seen_sources"]:
                state["sources"].pop(0)
                return Page([], state, True)
            body = {"page_size": min(limit, 100)}
            if state.get("query_cursor"):
                body["start_cursor"] = state["query_cursor"]
            guard = getattr(self, "selection_container_guard", None)
            if guard:
                guard("data_source_ids:" + identity)
            value = self.request("POST", f"/data_sources/{identity}/query", body=body)
            if guard:
                guard("data_source_ids:" + identity)
            for row in value.get("results", []):
                if row.get("object") == "page":
                    state["pages"].append(native_id(row["id"]))
            next_cursor = value.get("next_cursor")
            if value.get("has_more"):
                if not next_cursor or next_cursor in state.setdefault("query_seen", []):
                    raise AdapterError("pagination_loop")
                state["query_seen"].append(next_cursor)
                state["query_cursor"] = next_cursor
            else:
                state.pop("query_cursor", None)
                state.pop("query_seen", None)
                state["seen_sources"].append(state["sources"].pop(0))
            records, warnings = [], []
        else:
            records, warnings = [], []
        if len(state["seen_pages"]) > 10000:
            raise AdapterError("inventory_budget_exceeded")
        more = bool(state["pages"] or state["sources"] or state["databases"])
        state["complete"] = not more
        return Page(
            records,
            state,
            more,
            False,
            sorted(
                set(
                    warnings
                    + ["notion_visible_scope_only", "files_external_references"]
                )
            ),
        )
