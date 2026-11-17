"""File tree discovery and finite file/version selections."""

from .base import SourceModule
from ..adapters.base import AdapterError


class FilesSource(SourceModule):
    adapter_id = "filesystem.typora"
    name, icon, unit = "文件与笔记", "文", "文件夹 / 文件"
    description = "选择任意类型文件；提取可读内容，无法解析的保留原件。"
    page_kind, slug = "files", "files"
    profiles = ("linux-markdown-v1",)
    import_driver = "files"
    page_limit = 1  # A binary file may fill the byte budget by itself.

    def scopes(self, binding):
        return self.candidates(binding)["items"]

    def candidates(self, binding, parent=".", cursor="", limit=100, query="", kind=""):
        from .file_selection import candidates

        if not 1 <= limit <= 200:
            raise AdapterError("invalid_limit")
        return candidates(binding["config"], parent, cursor, limit, query, kind)

    def validate(self, binding, selection):
        self.validate_time(selection)

    def prepare_plan(self, binding, selection):
        from .file_selection import freeze

        return freeze(binding["config"], selection)

    def verify_plan(self, binding, selection):
        from .file_selection import validate_frozen

        validate_frozen(binding["config"], selection)

    def refresh_selection(self, binding, selection, subscription, now):
        return self.prepare_plan(binding, selection)

    def config_schema(self):
        return {
            "type": "object",
            "properties": {
                "root": {"type": "string", "minLength": 1},
                "debounce_ms": {"type": "integer", "minimum": 100},
                "scan_interval_seconds": {"type": "integer", "minimum": 1},
                "max_file_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 32 * 1024 * 1024,
                },
            },
            "required": ["root"],
            "additionalProperties": False,
        }


source = FilesSource()
