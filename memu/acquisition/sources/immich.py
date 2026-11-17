"""Immich albums, asset timestamps and primary media handling."""

from copy import deepcopy

from ..adapters.base import AdapterError
from .base import SourceModule


class ImmichSource(SourceModule):
    adapter_id = "immich.assets"
    name, icon, unit = "Immich", "相", "相册"
    description = "按相册和拍摄时间选择照片与视频，仅保存资源引用，原件保留在 Immich。"
    page_kind, slug = "media", "immich"
    profiles = ("api-key", "installed-openapi")
    scope_fields = ("album_ids",)
    time_filter = True
    preview = True
    primary_media = True
    page_limit = 1

    def candidates(self, binding, parent=".", cursor="", limit=100, query="", kind=""):
        if binding.get("source_profile") == "api-key":
            reader = self.reader_class("api-key")(binding["config"])
            try:
                return reader.catalog(parent, cursor, limit, query, kind)
            finally:
                reader.close()
        from .document_catalog import candidates

        # The installed-version adapter has no verified album metadata endpoint.
        return candidates(
            self, binding, cursor=cursor, limit=limit, query=query, kind=kind
        )

    def configured_scopes(self, config):
        if "api_key_ref" in config and "schema_path" not in config:
            return [{"id": "all_assets", "name": "全部照片与视频", "kind": "library"}]
        if config.get("all_assets"):
            return [
                {"id": "all_assets", "name": "已授权的全部资产", "kind": "all_assets"}
            ]
        return super().configured_scopes(config)

    def narrow_config(self, config, selection):
        if "api_key_ref" in config and "schema_path" not in config:
            self.validate_time(selection)
            result = deepcopy(config)
            for field in ("album_ids", "asset_ids", "taken_after", "taken_before"):
                result.pop(field, None)
            chosen = set(selection.scopes)
            if not chosen or any(s != "all_assets" and not (
                    s.startswith(("album_ids:", "asset_ids:")) and s.partition(":")[2]) for s in chosen):
                raise AdapterError("import_scope_denied")
            if "all_assets" not in chosen:
                for field in ("album_ids", "asset_ids"):
                    values = sorted(s.partition(":")[2] for s in chosen if s.startswith(field + ":"))
                    if values:
                        result[field] = values
            if selection.start:
                result["taken_after"] = selection.start.isoformat()
            if selection.end:
                result["taken_before"] = selection.end.isoformat()
            return result
        if config.get("all_assets"):
            if set(selection.scopes) != {"all_assets"}:
                raise AdapterError("import_scope_denied")
            return deepcopy(config)
        return super().narrow_config(config, selection)

    def validate(self, binding, selection):
        if binding.get("source_profile") == "api-key":
            self.narrow_config(binding["config"], selection)
        else:
            super().validate(binding, selection)

    def timestamp(self, record):
        return record.metadata.get("taken_at")

    def preview_page(self, binding, selection, cursor="", limit=30):
        if binding.get("source_profile") != "api-key":
            raise AdapterError("immich_preview_unsupported")
        if (len(selection.scopes) != 1 or not 1 <= limit <= 30
                or not (selection.scopes[0] == "all_assets" or selection.scopes[0].startswith("album_ids:"))):
            raise AdapterError("import_preview_invalid")
        from .immich_preview import preview

        reader = self.reader_class("api-key")(self.narrow_config(binding["config"], selection))
        try:
            return preview(reader, selection.scopes[0], cursor, limit)
        finally:
            reader.close()

    def reader_class(self, profile):
        if profile == "api-key":
            from ..adapters.immich_auto import AutoImmichAdapter

            return AutoImmichAdapter
        from ..adapters.immich import ImmichAdapter

        return ImmichAdapter


source = ImmichSource()
