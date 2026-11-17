"""Local AI client files; enumerate names before reading selected conversations."""

from copy import deepcopy

from .base import SourceModule


class LocalAISource(SourceModule):
    adapter_id = "ai.local-logs"
    name, icon, unit = "本地 AI 日志", "⌘", "日志文件"
    description = "选择日志文件，导入指定时间内的完整记录。"
    page_kind, slug = "logs", "local-ai"
    profiles = ("codex", "claude-code", "gemini-cli", "opencode", "cursor")
    time_filter = True

    def scopes(self, binding):
        from ..adapters.local.common import LocalAccess

        values, profile = binding["config"], binding["source_profile"]
        access = LocalAccess(values)
        reader = self.reader_class(profile)(values)
        if hasattr(reader, "patterns"):
            paths = access.files(reader.patterns)
        elif profile == "cursor":
            paths = reader._databases()
        else:
            paths = reader._legacy_files()
            if reader.profile != "legacy-json" and reader.database.is_file():
                paths.append(reader.database)
        return [
            {
                "id": str(p.relative_to(access.root)),
                "name": str(p.relative_to(access.root)),
                "kind": "log",
            }
            for p in paths
        ]

    def narrow_config(self, config, selection):
        return deepcopy(config)

    def includes(self, record, selection):
        key = record.metadata.get(
            "file", record.metadata.get("database", record.metadata.get("path", ""))
        )
        return any(
            key == scope or key.startswith(scope.rstrip("/") + "/")
            for scope in selection.scopes
        )

    def prepare_reader(self, reader, selection):
        super().prepare_reader(reader, selection)
        if hasattr(reader, "access"):
            original = reader.access.files
            chosen = set(selection.scopes)
            selected = {}

            def selected_files(patterns):
                if patterns not in selected:
                    selected[patterns] = [p for p in original(patterns)
                                          if str(p.relative_to(reader.access.root)) in chosen]
                return selected[patterns]

            reader.access.files = selected_files
        if getattr(reader, "adapter_id", "") == "local.cursor":
            original_databases = reader._databases
            reader._databases = lambda: [
                p
                for p in original_databases()
                if str(p.relative_to(reader.access.root)) in selection.scopes
            ]

    def reader_class(self, profile):
        from ..adapters.local import (
            CodexAdapter,
            ClaudeCodeAdapter,
            GeminiCLIAdapter,
            OpenCodeAdapter,
            CursorAdapter,
        )

        return {
            "codex": CodexAdapter,
            "claude-code": ClaudeCodeAdapter,
            "gemini-cli": GeminiCLIAdapter,
            "opencode": OpenCodeAdapter,
            "cursor": CursorAdapter,
        }[profile]


source = LocalAISource()
