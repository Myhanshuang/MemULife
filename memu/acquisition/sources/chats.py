"""Shared refresh semantics for chat histories, not their transport formats."""

from datetime import timedelta

from ..adapters.base import AdapterError
from .base import SourceModule


class ChatSource(SourceModule):
    page_kind = "chats"
    unit = "聊天"
    time_filter = True
    preview = True
    scope_fields = ("conversations",)

    def candidates(self, binding, parent=".", cursor="", limit=100, query="", kind=""):
        from .browsing import browse

        if not 1 <= limit <= 200:
            raise AdapterError("invalid_limit")
        return browse(
            binding, cursor=cursor, limit=min(limit, 100), query=query, kind=kind
        )

    def preview_page(self, binding, selection, cursor="", limit=30):
        from .browsing import preview

        return preview(binding, selection, cursor=cursor, limit=limit)

    def refresh_selection(self, binding, selection, subscription, now):
        if not selection.end and now - subscription["reconciled_at"] < timedelta(
            days=1
        ):
            start = subscription["watermark"] - timedelta(minutes=5)
            if selection.start:
                start = max(start, selection.start)
            return selection.model_copy(update={"start": start})
        return selection
