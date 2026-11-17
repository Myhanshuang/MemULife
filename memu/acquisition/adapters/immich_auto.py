"""URL/key connection using the server's own API description and account identity."""

from copy import deepcopy
import hashlib
import json

from jsonschema import Draft202012Validator

from .base import AdapterError, Page
from .http import SourceHTTP, environment_secret, identifier, json_bytes
from .immich import ImmichAdapter
from ..sources.selection import Selection, in_time


SETUP_SCHEMA = {
    "type": "object",
    "properties": {
        "base_url": {"type": "string", "minLength": 1},
        "api_key_ref": {"type": "string", "pattern": "^env:[A-Za-z_][A-Za-z0-9_]*$"},
    },
    "required": ["base_url", "api_key_ref"],
    "additionalProperties": False,
}
CONFIG_SCHEMA = deepcopy(SETUP_SCHEMA)
CONFIG_SCHEMA["properties"].update({
    "album_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
    "asset_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
    "taken_after": {"type": "string"},
    "taken_before": {"type": "string"},
})


class AutoImmichAdapter(ImmichAdapter):
    profile_id = "api-key"
    config_schema = CONFIG_SCHEMA

    def __init__(self, config, client=None, *, clock=None):
        if list(Draft202012Validator(self.config_schema).iter_errors(config)):
            raise AdapterError("config_invalid")
        url = config["base_url"].strip().rstrip("/")
        if not url.endswith("/api"):
            url += "/api"
        http = SourceHTTP(url, client=client, allow_insecure_http=url.startswith("http://"))
        try:
            headers = {"x-api-key": environment_secret(config["api_key_ref"])}
            owner, _ = http.json("GET", "/users/me", headers=headers)
            self.remote_schema, raw = http.json("GET", "/spec.json", headers=headers)
            automatic = {
                "base_url": url, "api_key_ref": config["api_key_ref"],
                "allow_insecure_http": url.startswith("http://"),
                "deployment_id": hashlib.sha256(url.encode()).hexdigest(),
                "owner_id": identifier(owner.get("id")),
                "schema_path": "server:spec.json", "schema_sha256": hashlib.sha256(raw).hexdigest(),
                "server_version": self.remote_schema.get("info", {}).get("version"),
                "visibilities": ["timeline", "archive"],
                **({"album_ids": config["album_ids"]} if config.get("album_ids") else {"all_assets": True}),
            }
            super().__init__(automatic, client=http.client, clock=clock)
            self.http.owns_client = http.owns_client
            self.config.update({k: v for k, v in config.items() if k in {
                "asset_ids", "taken_after", "taken_before"}})
            self.binding = hashlib.sha256(json_bytes([self.binding, config])).hexdigest()
            self._verify()
        except Exception:
            http.close()
            raise

    def _load_schema(self):
        return self.remote_schema

    def _query(self, *args, **kwargs):
        body = super()._query(*args, **kwargs)
        start, end = self.config.get("taken_after"), self.config.get("taken_before")
        if self.structured:
            if start or end:
                body["filter"]["takenAt"] = {
                    **({"gte": start} if start else {}), **({"lt": end} if end else {})}
        else:
            if start:
                body["takenAfter"] = start
            if end:
                body["takenBefore"] = end
        return body

    def _asset(self, item):
        record, warnings = super()._asset(item)
        record.occurred_at = record.metadata.get("taken_at")
        return record, warnings

    def _check_scope(self, item, visibility):
        super()._check_scope(item, visibility)
        selection = Selection(scopes=["all_assets"], start=self.config.get("taken_after"),
                              end=self.config.get("taken_before"))
        if not in_time(item.get("fileCreatedAt"), selection):
            raise AdapterError("asset_scope_mismatch")

    def read(self, mode, cursor=None, limit=50):
        if self.config.get("album_ids") and self.config.get("asset_ids"):
            state = cursor or {"binding": self.binding, "phase": "albums", "upstream": None}
            if state.get("binding") != self.binding or state.get("phase") not in {"albums", "assets", "done"}:
                raise AdapterError("cursor_scope_mismatch")
            if state["phase"] == "done":
                return Page([], state, False)
            # A mixed selection is a union. Individual assets may be outside the selected albums.
            field = "asset_ids" if state["phase"] == "albums" else "album_ids"
            saved = self.config.pop(field)
            try:
                page = self.read(mode, state["upstream"], limit)
            finally:
                self.config[field] = saved
            state = dict(state, upstream=page.cursor)
            if not page.has_more:
                state.update(phase="assets" if state["phase"] == "albums" else "done", upstream=None)
            page.cursor, page.has_more = state, state["phase"] != "done"
            return page
        selected = self.config.get("asset_ids")
        if selected is None:
            page = super().read(mode, cursor, limit)
            # Search responses are catalogue metadata, not imported material.
            page.records = [r for r in page.records if r.metadata.get("kind") == "asset"]
            return page
        if mode not in {"discover", "backfill", "poll", "live", "reconcile"}:
            raise AdapterError("capability_unavailable")
        state = cursor or {"binding": self.binding, "offset": 0}
        offset = state.get("offset")
        if (state.get("binding") != self.binding or type(offset) is not int
                or not 0 <= offset <= len(selected) or not 1 <= limit <= 100):
            raise AdapterError("cursor_scope_mismatch")
        records, warnings = [], []
        for asset_id in selected[offset:offset + limit]:
            # Search enforces dates and visibility before saving asset references.
            for visibility in self.config["visibilities"]:
                body = self._query(visibility, 1, None, None, None, asset_id=asset_id)
                result, _ = self._search(body)
                for item in result.get("assets", {}).get("items", []):
                    if item.get("id") != asset_id:
                        raise AdapterError("asset_scope_mismatch")
                    self._check_scope(item, visibility)
                    record, gaps = self._asset(item)
                    records.append(record)
                    warnings.extend(gaps)
        offset = min(offset + limit, len(selected))
        return Page(records, {"binding": self.binding, "offset": offset}, offset < len(selected),
                    scope_complete=False, warnings=sorted(set(warnings)))

    def catalog(self, parent=".", cursor="", limit=100, query="", kind=""):
        if not 1 <= limit <= 200:
            raise AdapterError("invalid_limit")
        if parent == ".":
            raw, _ = self.http.request("GET", "/albums", headers=self._headers(), params={"isOwned": "true"})
            try:
                albums = json.loads(raw)
            except ValueError:
                raise AdapterError("incompatible_schema") from None
            if not isinstance(albums, list):
                raise AdapterError("incompatible_schema")
            rows = [{"id": "all_assets", "name": "全部照片与视频", "kind": "library"}]
            rows.extend({"id": "album_ids:" + identifier(a.get("id")), "name": a.get("albumName"),
                         "kind": "album", "asset_count": a.get("assetCount")} for a in albums)
            rows = sorted((r for r in rows if (not query or query.casefold() in (r["name"] or "").casefold())
                           and (not kind or r["kind"] == kind) and r["id"] > cursor), key=lambda r: r["id"])
            return {"items": rows[:limit], "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
                    "content_persisted": False}
        if parent != "all_assets" and not parent.startswith("album_ids:"):
            raise AdapterError("import_scope_denied")
        if parent.startswith("album_ids:"):
            self.config["album_ids"] = [identifier(parent.partition(":")[2])]
        try:
            state = json.loads(cursor) if cursor else {"page": 1, "next_cursor": None, "visibility": 0}
            if (not isinstance(state, dict) or type(state.get("visibility")) is not int
                    or not 0 <= state["visibility"] < len(self.config["visibilities"])
                    or type(state.get("page")) is not int or state["page"] < 1
                    or not isinstance(state.get("next_cursor"), (str, type(None)))):
                raise ValueError()
        except (ValueError, TypeError):
            raise AdapterError("source_cursor_invalid") from None
        visibility = self.config["visibilities"][state["visibility"]]
        body = self._query(visibility, limit, None, None, state)
        if query:
            if self.structured:
                body["filter"]["originalFileName"] = {"like": "%" + query + "%"}
            else:
                body["originalFileName"] = query
        result, _ = self._search(body)
        assets = result.get("assets", {})
        rows = []
        for item in assets.get("items", []):
            self._check_scope(item, visibility)
            rows.append({"id": "asset_ids:" + item["id"], "name": item.get("originalFileName") or item["id"],
                         "kind": "asset", "media_type": item.get("type"),
                         "duration": item.get("duration"),
                         "taken_at": item.get("fileCreatedAt"), "size_bytes": (item.get("exifInfo") or {}).get("fileSizeInByte")})
        next_value = assets.get("nextCursor" if self.structured else "nextPage")
        if next_value:
            if self.structured:
                state["next_cursor"] = next_value
            else:
                state["page"] = int(next_value)
        else:
            state.update(visibility=state["visibility"] + 1, page=1, next_cursor=None)
        return {"items": rows, "next_cursor": json.dumps(state) if state["visibility"] < len(self.config["visibilities"]) else None,
                "content_persisted": False}
