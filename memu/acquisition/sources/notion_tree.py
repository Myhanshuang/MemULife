"""Read-only Notion hierarchy, with live ancestry checks and volatile cursors.

Only names and structural references leave this module. Block bodies returned by
Notion's children API are discarded; relations and synced/linked blocks are not
traversal edges. A client-supplied ID or breadcrumb is never an authorization.
"""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import secrets
import threading
import time

from ..adapters.base import AdapterError
from ..adapters.notion import native_id
from .document_catalog import plain_title, restrict, safe_time

FIELDS = {
    "page_ids": "pages",
    "database_ids": "databases",
    "data_source_ids": "data_sources",
}
OBJECTS = {
    "page_ids": "page",
    "database_ids": "database",
    "data_source_ids": "data_source",
    "block_ids": "block",
}
PARENTS = {
    "page_id": "page_ids",
    "database_id": "database_ids",
    "data_source_id": "data_source_ids",
    "block_id": "block_ids",
}
LABELS = {"page_ids": "页面", "database_ids": "数据库", "data_source_ids": "数据源"}
DESCRIPTIONS = {
    "page_ids": "此页面",
    "database_ids": "此数据库中的条目",
    "data_source_ids": "此数据源中的条目",
}
MAX_DEPTH, MAX_REQUESTS, MAX_BYTES, SECONDS = 32, 96, 16 * 1024 * 1024, 25


def scope(value, *, blocks=False):
    try:
        field, identity = value.split(":", 1)
        if field not in FIELDS and not (blocks and field == "block_ids"):
            raise ValueError()
        return field + ":" + native_id(identity)
    except (AttributeError, ValueError):
        raise AdapterError("import_scope_denied") from None


def configured_roots(config):
    return {
        field + ":" + native_id(identity)
        for field in FIELDS
        for identity in config.get(field, [])
    }


def path_revision(path):
    return "notion-tree-v1:" + hashlib.sha256("\n".join(path).encode()).hexdigest()


def clean_title(value):
    if not isinstance(value, str):
        return ""
    return "".join(
        char for char in value if ord(char) >= 32 and not 0xD800 <= ord(char) <= 0xDFFF
    ).strip()[:300]


def candidate(identity, title=None, updated_at=None):
    field = identity.split(":", 1)[0]
    title = clean_title(title)
    return {
        "id": identity,
        "name": title or identity.split(":", 1)[1],
        "title": title or None,
        "kind": field,
        "type_label": LABELS[field],
        "updated_at": safe_time(updated_at),
        "can_expand": True,
        "selection_kind": "page" if field == "page_ids" else "container",
        "scope_description": DESCRIPTIONS[field],
    }


class CursorStore:
    """Opaque, immutable pagination state: no block bodies or durable files."""

    def __init__(self):
        self.rows, self.lock = {}, threading.Lock()

    def put(self, binding, state):
        if len(json.dumps(state).encode()) > 64 * 1024:
            raise AdapterError("notion_tree_budget_exceeded")
        with self.lock:
            now = time.monotonic()
            self.rows = {key: row for key, row in self.rows.items() if row[0] > now}
            if len(self.rows) >= 128:
                raise AdapterError("notion_tree_capacity")
            token = secrets.token_urlsafe(24)
            self.rows[token] = (now + 600, binding, deepcopy(state))
            return token

    def get(self, token, binding):
        with self.lock:
            row = self.rows.get(token)
            if not row or row[0] <= time.monotonic() or row[1] != binding:
                raise AdapterError("notion_cursor_expired")
            return deepcopy(row[2])


cursors = CursorStore()


class Tree:
    def __init__(self, reader, roots):
        self.reader, self.roots = reader, set(roots)
        self.cache = {}
        self.deleted_leaf = None
        self.start_requests, self.start_bytes = reader.requests, reader.bytes_received
        self.deadline = time.monotonic() + SECONDS

    def request(self, method, path, **kwargs):
        if (
            time.monotonic() >= self.deadline
            or self.reader.requests - self.start_requests >= MAX_REQUESTS
            or self.reader.bytes_received - self.start_bytes >= MAX_BYTES
        ):
            raise AdapterError("notion_tree_budget_exceeded", True)
        value = self.reader.request(method, path, **kwargs)
        if self.reader.bytes_received - self.start_bytes > MAX_BYTES:
            raise AdapterError("notion_tree_budget_exceeded", True)
        return value

    def get(self, identity):
        if identity in self.cache:
            return self.cache[identity]
        field, identifier = scope(identity, blocks=True).split(":", 1)
        value = self.request(
            "GET", "/" + FIELDS.get(field, "blocks") + "/" + identifier
        )
        if (
            value.get("object") != OBJECTS[field]
            or native_id(value.get("id")) != identifier
        ):
            raise AdapterError("source_binding_mismatch")
        deleted = bool(value.get("archived") or value.get("in_trash"))
        deleting = self.cache.get(self.deleted_leaf, {}).get("deleted", False)
        if deleted and identity != self.deleted_leaf and not deleting:
            raise AdapterError("import_scope_denied")
        title = value.get("title", [])
        if field == "page_ids":
            properties = value.get("properties", {})
            if not isinstance(properties, dict):
                raise AdapterError("incompatible_schema")
            title = next(
                (
                    p.get("title", [])
                    for p in properties.values()
                    if isinstance(p, dict) and p.get("type") == "title"
                ),
                [],
            )
        if not isinstance(title, list):
            raise AdapterError("incompatible_schema")
        # Retain only structural metadata, never arbitrary properties or rich text.
        result = {
            "parent": value.get("parent"),
            "title": clean_title(plain_title(title)),
            "updated_at": value.get("last_edited_time"),
            "type": value.get("type"),
            "deleted": deleted,
        }
        if field == "database_ids":
            sources = value.get("data_sources")
            if not isinstance(sources, list) or len(sources) > 200:
                raise AdapterError("incompatible_schema")
            result["data_sources"] = [
                {"id": native_id(row.get("id")), "name": row.get("name")}
                for row in sources
                if isinstance(row, dict)
            ]
        self.cache[identity] = result
        return result

    def ancestry(self, identity, *, roots=None):
        """Return root-to-object native edges; no relation or linked-page edges."""
        allowed = self.roots if roots is None else set(roots)
        chain, seen = [], set()
        current = scope(identity, blocks=True)
        for _ in range(MAX_DEPTH):
            if current in seen:
                raise AdapterError("notion_tree_cycle")
            seen.add(current)
            chain.append(current)
            info = self.get(current)
            if current in allowed:
                return list(reversed(chain))
            if info.get("type") in {"synced_block", "link_to_page", "link_preview"}:
                raise AdapterError("import_scope_denied")
            parent = info.get("parent")
            if not isinstance(parent, dict) or parent.get("type") not in PARENTS:
                raise AdapterError("import_scope_denied")
            kind = parent["type"]
            current = PARENTS[kind] + ":" + native_id(parent.get(kind))
        raise AdapterError("notion_tree_depth_exceeded")

    def breadcrumbs(self, path):
        return [{"id": ".", "title": "根目录"}] + [
            {
                "id": node,
                "title": self.get(node)["title"] or LABELS[node.split(":", 1)[0]],
            }
            for node in path
            if not node.startswith("block_ids:")
        ]

    def check_page(self, identity, selected, revisions):
        """An exact page or a direct row of a selected database/data source."""
        self.cache.clear()  # Read-before/read-after must not reuse an old parent.
        self.deleted_leaf = "page_ids:" + native_id(identity)
        path = self.ancestry(self.deleted_leaf)
        leaf = path[-1]
        if leaf in selected:
            anchor = leaf
        elif len(path) >= 2 and path[-2].startswith("data_source_ids:"):
            anchor = (
                path[-2]
                if path[-2] in selected
                else (
                    path[-3]
                    if len(path) >= 3
                    and path[-3].startswith("database_ids:")
                    and path[-3] in selected
                    else None
                )
            )
        else:
            anchor = None
        if not anchor:
            raise AdapterError("import_scope_denied")
        if revisions.get(anchor) != path_revision(path[: path.index(anchor) + 1]):
            raise AdapterError("import_plan_stale")

    def check_container(self, identity, selected, revisions):
        self.cache.clear()
        path = self.ancestry(identity)
        anchor = identity if identity in selected else None
        if (
            not anchor
            and identity.startswith("data_source_ids:")
            and len(path) >= 2
            and path[-2].startswith("database_ids:")
            and path[-2] in selected
        ):
            anchor = path[-2]
        if not anchor:
            raise AdapterError("import_scope_denied")
        if revisions.get(anchor) != path_revision(path[: path.index(anchor) + 1]):
            raise AdapterError("import_plan_stale")


@contextmanager
def tree(config, *, roots=None):
    from ..adapters.notion import NotionAdapter

    reader = NotionAdapter(config)
    try:
        restrict(reader)
        reader.probe()
        yield Tree(reader, configured_roots(config) if roots is None else roots)
    finally:
        reader.close()


def validate(config, selection, *, verify=False):
    revisions = {}
    with tree(config, roots=set(selection.scopes) if config.get('browse_accessible') else None) as catalog:
        for identity in selection.scopes:
            normalized = scope(identity)
            if normalized != identity:
                raise AdapterError("import_scope_denied")
            revisions[identity] = path_revision(catalog.ancestry(identity))
    if verify and revisions != selection.revisions:
        raise AdapterError("import_plan_stale")
    return revisions


def _next(value, previous):
    if (
        not isinstance(value.get("results"), list)
        or type(value.get("has_more")) is not bool
    ):
        raise AdapterError("incompatible_schema")
    if not value["has_more"]:
        return None
    next_cursor = value.get("next_cursor")
    if (
        not isinstance(next_cursor, str)
        or not next_cursor
        or len(next_cursor) > 2048
        or next_cursor == previous
    ):
        raise AdapterError("pagination_loop")
    return next_cursor


def children(binding, parent, cursor="", limit=100, query="", kind=""):
    if not 1 <= limit <= 200:
        raise AdapterError("invalid_limit")
    parent = scope(parent)
    key = hashlib.sha256(
        json.dumps([binding, parent, limit, query, kind], sort_keys=True).encode()
    ).hexdigest()
    state = (
        cursors.get(cursor, key)
        if cursor
        else {"pending": [], "visited": [], "offset": 0}
    )
    with tree(binding["config"], roots={parent} if binding['config'].get('browse_accessible') else None) as catalog:
        path = catalog.ancestry(parent)
        crumbs = catalog.breadcrumbs(path)
        block_path = None
        field, identity = parent.split(":", 1)
        rows, more = [], False
        if field == "database_ids":
            sources = catalog.get(parent)["data_sources"]
            start = state["offset"]
            for item in sources[start : start + limit]:
                rows.append(
                    candidate("data_source_ids:" + item["id"], item.get("name"))
                )
            state["offset"] += len(rows)
            more = state["offset"] < len(sources)
        elif field == "data_source_ids":
            body = {"page_size": min(limit, 100)}
            if state.get("upstream"):
                body["start_cursor"] = state["upstream"]
            value = catalog.request(
                "POST",
                f"/data_sources/{identity}/query",
                body=body,
                params={"filter_properties": "title"},
            )
            next_cursor = _next(value, state.get("upstream"))
            if next_cursor in state["visited"]:
                raise AdapterError("pagination_loop")
            if next_cursor:
                state["visited"].append(next_cursor)
            for item in value["results"]:
                if (
                    item.get("object") != "page"
                    or item.get("archived")
                    or item.get("in_trash")
                ):
                    continue
                native_parent = item.get("parent", {})
                if (
                    native_parent.get("type") != "data_source_id"
                    or native_id(native_parent.get("data_source_id")) != identity
                ):
                    raise AdapterError("source_binding_mismatch")
                props = item.get("properties", {})
                title = next(
                    (
                        p.get("title", [])
                        for p in props.values()
                        if isinstance(p, dict) and p.get("type") == "title"
                    ),
                    [],
                )
                rows.append(
                    candidate(
                        "page_ids:" + native_id(item.get("id")),
                        plain_title(title),
                        item.get("last_edited_time"),
                    )
                )
            state["upstream"] = next_cursor
            more = bool(next_cursor)
        else:
            if not cursor:
                state["pending"] = [{"id": identity, "cursor": None, "depth": 0}]
            # A call scans one upstream page. Empty filtered pages still continue.
            if state["pending"]:
                frame = state["pending"].pop(0)
                if frame["depth"] >= MAX_DEPTH:
                    raise AdapterError("notion_tree_depth_exceeded")
                token = frame["id"] + ":" + (frame["cursor"] or "")
                if token in state["visited"]:
                    raise AdapterError("pagination_loop")
                state["visited"].append(token)
                if frame["id"] != identity:
                    block_path = catalog.ancestry(
                        "block_ids:" + frame["id"], roots={parent}
                    )
                value = catalog.request(
                    "GET",
                    f"/blocks/{frame['id']}/children",
                    params={"page_size": min(limit, 100)}
                    | ({"start_cursor": frame["cursor"]} if frame["cursor"] else {}),
                )
                next_cursor = _next(value, frame["cursor"])
                if next_cursor:
                    state["pending"].insert(0, dict(frame, cursor=next_cursor))
                for block in value["results"]:
                    if block.get("archived") or block.get("in_trash"):
                        continue
                    block_type = block.get("type")
                    if block_type in {"child_page", "child_database"}:
                        target = (
                            "page_ids:"
                            if block_type == "child_page"
                            else "database_ids:"
                        ) + native_id(block.get("id"))
                        rows.append(
                            candidate(target, block.get(block_type, {}).get("title"))
                        )
                    elif block.get("has_children") and block_type not in {
                        "synced_block",
                        "link_to_page",
                        "link_preview",
                        "unsupported",
                    }:
                        state["pending"].append(
                            {
                                "id": native_id(block.get("id")),
                                "cursor": None,
                                "depth": frame["depth"] + 1,
                            }
                        )
                more = bool(state["pending"])
        # Discard results if the browsed page/container moved during IO.
        catalog.cache.clear()
        if catalog.ancestry(parent) != path:
            raise AdapterError("import_plan_stale")
        if (
            block_path
            and catalog.ancestry(block_path[-1], roots={parent}) != block_path
        ):
            raise AdapterError("import_plan_stale")
    query = query.strip().casefold()
    return {
        "items": [
            row
            for row in rows
            if (not kind or row["kind"] == kind)
            and (not query or query in row["name"].casefold() or query in row["id"])
        ],
        "parent": parent,
        "breadcrumbs": crumbs,
        "next_cursor": cursors.put(key, state) if more else None,
        "content_persisted": False,
    }
