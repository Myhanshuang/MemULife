"""Read a selected page without persisting content or fetching attachments."""

import hashlib
import json

from ..adapters.base import AdapterError
from .notion_tree import CursorStore

cursors = CursorStore()


def preview(module, binding, selection, cursor="", limit=30):
    if not 1 <= limit <= 30:
        raise AdapterError("invalid_limit")
    if len(selection.scopes) != 1 or not selection.scopes[0].startswith("page_ids:"):
        raise AdapterError("import_scope_denied")
    module.validate(binding, selection)
    key = hashlib.sha256(json.dumps(
        [binding, selection.model_dump(mode="json")], sort_keys=True,
    ).encode()).hexdigest()
    position = cursors.get(cursor, key) if cursor else {"offset": 0}
    selected = module.prepare_plan(binding, selection)
    reader = module.reader_class(binding.get("source_profile"))(binding["config"])
    try:
        module.prepare_reader(reader, selected)
        record = reader.read("backfill", limit=1).records[0]
    finally:
        reader.close()
    if record.deleted:
        raise AdapterError("import_scope_denied")
    revision = hashlib.sha256((record.revision + "\n" + record.text).encode()).hexdigest()
    if cursor and position["revision"] != revision:
        raise AdapterError("source_changed_during_read", True)
    offset = position["offset"]
    text = record.text[offset:offset + limit * 2000]
    end = offset + len(text)
    return {
        "items": [{"id": selection.scopes[0], "title": record.metadata.get("title"), "text": text}],
        "next_cursor": cursors.put(key, {"offset": end, "revision": revision})
        if end < len(record.text) else None,
        "content_persisted": False,
    }
