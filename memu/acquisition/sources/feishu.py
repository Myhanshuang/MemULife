"""Feishu explicit document, folder and wiki roots."""

from .base import SourceModule


class FeishuSource(SourceModule):
    adapter_id = "feishu.documents"
    name, icon, unit = "飞书", "飞", "文档 / 文件夹 / 知识库"
    description = "选择文档、文件夹或知识库，保留正文与原始证据。"
    page_kind, slug = "documents", "feishu"
    profiles = ("docx-v1",)
    scope_fields = ("document_ids", "folder_tokens", "wiki_spaces")
    page_limit = 1

    def candidates(self, binding, parent=".", cursor="", limit=100, query="", kind=""):
        from .document_catalog import candidates, feishu_lookup
        if binding['config'].get('browse_accessible'):
            from .feishu_discovery import discover
            return discover(binding, parent, cursor, limit, query, kind)

        # Folder/wiki names require different APIs; retain their explicit IDs.
        lookup = (
            feishu_lookup
            if binding["config"].get("document_ids") and kind in {"", "document_ids"}
            else None
        )
        return candidates(
            self,
            binding,
            cursor=cursor,
            limit=limit,
            query=query,
            kind=kind,
            lookup=lookup,
        )

    def validate(self, binding, selection):
        if not binding['config'].get('browse_accessible'):
            return super().validate(binding, selection)
        from .feishu_discovery import verify
        verify(self.narrow_config(binding['config'], selection))

    def narrow_config(self, config, selection):
        if not config.get('browse_accessible'):
            return super().narrow_config(config, selection)
        from copy import deepcopy
        from .feishu_discovery import scope
        self.validate_time(selection)
        result = deepcopy(config)
        result.pop('browse_accessible', None)
        for field in self.scope_fields:
            result[field] = []
        for value in selection.scopes:
            field, identity = scope(value)
            result[field].append(identity)
        return result

    def reader_class(self, profile):
        from ..adapters.feishu import FeishuAdapter

        return FeishuAdapter


source = FeishuSource()
