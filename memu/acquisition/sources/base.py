"""The source boundary used by import orchestration and source pages.

Modules own discovery, selection semantics and reader construction. They never
open a Host, create jobs, publish memories, or write original content themselves.
"""

from copy import deepcopy

from ..adapters.base import AdapterError


class SourceModule:
    adapter_id = ""
    name = ""
    icon = ""
    description = ""
    unit = ""
    page_kind = ""
    slug = ""
    profiles = ()
    scope_fields = ()
    import_driver = "reader"
    time_filter = False
    preview = False
    message_selection = False
    continuous_updates = True
    primary_media = False
    subscribes = False
    page_limit = 50
    event_types = ("record.upsert", "record.tombstone")

    def profile(self):
        return {
            "name": self.name,
            "icon": self.icon,
            "description": self.description,
            "unit": self.unit,
            "mode": "push" if self.import_driver == "push" else "collect",
            "page_kind": self.page_kind,
            "slug": self.slug,
            "preview": self.preview,
            "message_selection": self.message_selection,
            "continuous_updates": self.continuous_updates,
        }

    def configured_scopes(self, config):
        return [
            {"id": key + ":" + value, "name": value, "kind": key}
            for key in self.scope_fields
            for value in config.get(key, [])
        ]

    def scopes(self, binding):
        return self.configured_scopes(binding["config"])

    def candidates(self, binding, parent=".", cursor="", limit=100, query="", kind=""):
        if not 1 <= limit <= 200:
            raise AdapterError("invalid_limit")
        query = query.strip().casefold()
        rows = sorted(self.scopes(binding), key=lambda row: row["id"])
        rows = [
            row
            for row in rows
            if row["id"] > cursor
            and (not kind or row.get("kind", "unknown") == kind)
            and (
                not query
                or any(
                    query in str(row.get(key, "")).casefold()
                    for key in ("name", "title", "id")
                )
            )
        ]
        return {
            "items": rows[:limit],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            "content_persisted": False,
        }

    def preview_page(self, binding, selection, cursor="", limit=30):
        raise AdapterError("capability_unavailable")

    def validate_time(self, selection):
        if selection.messages and not self.message_selection:
            raise AdapterError("import_messages_unsupported")
        if (selection.start or selection.end) and not self.time_filter:
            raise AdapterError("import_time_unsupported")

    def validate(self, binding, selection):
        self.validate_time(selection)
        if not set(selection.scopes) <= {row["id"] for row in self.scopes(binding)}:
            raise AdapterError("import_scope_denied")
        self.narrow_config(binding["config"], selection)

    def narrow_config(self, config, selection):
        self.validate_time(selection)
        chosen = set(selection.scopes)
        if not chosen <= {row["id"] for row in self.configured_scopes(config)}:
            raise AdapterError("import_scope_denied")
        result = deepcopy(config)
        for field in self.scope_fields:
            result[field] = [
                value
                for value in config.get(field, [])
                if field + ":" + value in chosen
            ]
        return result

    def prepare_plan(self, binding, selection):
        return selection

    def verify_plan(self, binding, selection):
        """Optional check of a frozen candidate revision just before submission."""

    def refresh_selection(self, binding, selection, subscription, now):
        return selection

    def includes(self, record, selection):
        return True

    def timestamp(self, record):
        return record.occurred_at

    def prepare_record(self, record):
        """Remove source-specific evidence containing unselected neighbours."""

    def prepare_reader(self, reader, selection):
        reader.import_selection = selection

    def reader_class(self, profile):
        raise AdapterError("capability_unavailable")

    def config_schema(self):
        return {"anyOf": [self.reader_class(p).config_schema for p in self.profiles]}

    def validate_profile_config(self, profile, config):
        """Optional platform/profile consistency check, separate from JSON Schema."""
