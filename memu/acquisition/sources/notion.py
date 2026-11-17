"""Notion page, database and data source boundaries."""

from .base import SourceModule


class NotionSource(SourceModule):
    adapter_id = "notion.workspace"
    name, icon, unit = "Notion", "N", "页面 / 数据库 / 数据源"
    description = "导入页面、数据库条目、属性和嵌套内容。"
    page_kind, slug = "documents", "notion"
    profiles = ("api-2026-03-11",)
    scope_fields = ("page_ids", "database_ids", "data_source_ids")
    time_filter = True
    preview = True
    page_limit = 1

    def preview_page(self, binding, selection, cursor="", limit=30):
        from .notion_preview import preview

        return preview(self, binding, selection, cursor, limit)

    def candidates(self, binding, parent=".", cursor="", limit=100, query="", kind=""):
        from .document_catalog import candidates, notion_lookup
        from .notion_tree import candidate, children

        if parent != ".":
            return children(binding, parent, cursor, limit, query, kind)
        if binding['config'].get('browse_accessible'):
            from .notion_discovery import discover
            return discover(binding, cursor, limit, query, kind)
        result = candidates(
            self,
            binding,
            cursor=cursor,
            limit=limit,
            query=query,
            kind=kind,
            lookup=notion_lookup,
        )
        result["items"] = [
            row | candidate(row["id"], row.get("title"), row.get("updated_at"))
            for row in result["items"]
        ]
        result.update(parent=".", breadcrumbs=[{"id": ".", "title": "根目录"}])
        return result

    def configured_scopes(self, config):
        from .notion_tree import configured_roots

        return [
            {"id": item, "name": item.split(":", 1)[1], "kind": item.split(":", 1)[0]}
            for item in sorted(configured_roots(config))
        ]

    def validate(self, binding, selection):
        from ..adapters.base import AdapterError
        from .notion_tree import scope

        self.validate_time(selection)
        if any(scope(value) != value for value in selection.scopes):
            raise AdapterError("import_scope_denied")

    def narrow_config(self, config, selection):
        # Keep authorization roots until prepare_reader receives the selection in
        # the isolated worker. Narrowing here would lose the ancestry boundary.
        from copy import deepcopy

        self.validate({"config": config}, selection)
        return deepcopy(config)

    def prepare_plan(self, binding, selection):
        from .notion_tree import validate

        return selection.model_copy(
            update={"revisions": validate(binding["config"], selection)}
        )

    def verify_plan(self, binding, selection):
        from .notion_tree import validate

        validate(binding["config"], selection, verify=True)

    def prepare_reader(self, reader, selection):
        from .notion_tree import Tree, configured_roots

        super().prepare_reader(reader, selection)
        chosen = set(selection.scopes)
        roots = chosen if reader.config.get('browse_accessible') else configured_roots(reader.config)
        for field in self.scope_fields:
            reader.config[field] = [
                scope.split(":", 1)[1]
                for scope in selection.scopes
                if scope.startswith(field + ":")
            ]
        reader.config["include_children"] = False

        def guard(identity):
            Tree(reader, roots).check_page(identity, chosen, selection.revisions)

        reader.selection_guard = guard

        def container_guard(identity):
            Tree(reader, roots).check_container(identity, chosen, selection.revisions)

        reader.selection_container_guard = container_guard

    def reader_class(self, profile):
        from ..adapters.notion import NotionAdapter

        return NotionAdapter


source = NotionSource()
