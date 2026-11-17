"""Bounded in-memory thumbnails for the selected album; never import originals."""

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import json
from urllib.parse import quote

from PIL import Image, ImageOps

from ..adapters.base import AdapterError
from ..adapters.http import json_bytes


def thumbnail(reader, item):
    identity = item["id"].removeprefix("asset_ids:")
    try:
        raw, mime = reader.http.request(
            "GET", f"/assets/{quote(identity, safe='')}/thumbnail", headers=reader._headers(),
            params={"size": "thumbnail"}, max_bytes=2 * 1024 * 1024)
        if mime not in {"image/jpeg", "image/png", "image/webp", "image/avif"}:
            raise AdapterError("media_preview_unavailable")
        with Image.open(BytesIO(raw)) as original:
            if original.width * original.height > 16_000_000:
                raise AdapterError("media_preview_unavailable")
            image = ImageOps.exif_transpose(original)
            image.thumbnail((480, 480))
            output = BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=75)
        return {**item, "thumbnail": "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()}
    except AdapterError as error:
        code = "immich_preview_permission_required" if error.code == "source_access_denied" else "media_preview_unavailable"
        return {**item, "preview_error": code}
    except (OSError, ValueError, Image.DecompressionBombError):
        return {**item, "preview_error": "media_preview_unavailable"}


def preview(reader, parent, cursor, limit):
    binding = hashlib.sha256(json_bytes([reader.binding, parent])).hexdigest()
    upstream = ""
    if cursor:
        try:
            state = json.loads(cursor)
            if state["binding"] != binding or not isinstance(state["upstream"], str):
                raise ValueError()
            upstream = state["upstream"]
        except (ValueError, KeyError, TypeError):
            raise AdapterError("import_preview_cursor_invalid") from None
    page = reader.catalog(parent, upstream, limit)
    if not page["items"] and page["next_cursor"]:
        page = reader.catalog(parent, page["next_cursor"], limit)
    if len(page["items"]) > limit:
        raise AdapterError("import_preview_budget_exceeded")
    # The search page has checked ownership, visibility and date bounds before thumbnails are requested.
    rows = []
    if page["items"]:
        first = thumbnail(reader, page["items"][0])
        if first.get("preview_error") == "immich_preview_permission_required":
            rows = [{**item, "preview_error": first["preview_error"]} for item in page["items"]]
        else:
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="immich-preview") as pool:
                rows = [first, *pool.map(lambda item: thumbnail(reader, item), page["items"][1:])]
    next_cursor = json.dumps({"binding": binding, "upstream": page["next_cursor"]}) if page["next_cursor"] else None
    return {"items": rows, "next_cursor": next_cursor, "content_persisted": False,
            "warnings": sorted({r["preview_error"] for r in rows if r.get("preview_error")})}
