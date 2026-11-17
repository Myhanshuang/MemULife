"""Apply selected source scopes before any caller can persist returned records."""

from ..adapters.reader import IsolatedReader
from . import get_source
from .registry import source_for_reader
from .selection import in_time


def narrowed_config(adapter, config, selection):
    return get_source(adapter).narrow_config(config, selection)


class SelectedReader:
    """Cursor includes counters atomically persisted with accepted inbox events."""

    def __init__(self, host_config, selection, reader=None):
        self.adapter = host_config["adapter_id"]
        self.selection = selection
        self.source = get_source(self.adapter)
        config = narrowed_config(self.adapter, host_config["config"], selection)
        self.reader = reader or IsolatedReader(
            self.adapter,
            host_config["source_profile"],
            config,
            selection=selection.model_dump(mode="json"),
            persistent=self.adapter == 'ai.local-logs' and host_config['source_profile'] in {
                'codex', 'claude-code', 'gemini-cli'},
        )

    def close(self):
        if hasattr(self.reader, 'close'):
            self.reader.close()

    def read(self, mode, cursor=None, limit=50):
        state = cursor or {}
        page = self.reader.read(mode, state.get("upstream"), limit)
        kept = []
        missing = 0
        for record in page.records:
            if not self.source.includes(record, self.selection):
                continue
            stamp = self.source.timestamp(record)
            if not in_time(stamp, self.selection):
                missing += int(not stamp)
                continue
            self.source.prepare_record(record)
            if not self.selection.include_attachments and not self.source.primary_media:
                for attachment in record.attachments:
                    attachment.data = None
                    attachment.status = "excluded"
            kept.append(record)
        total = state.get("examined", 0) + len(page.records)
        selected = state.get("selected", 0) + len(kept)
        page.records = kept
        page.cursor = {"upstream": page.cursor, "examined": total, "selected": selected}
        page.scope_complete = False  # A narrowed scan cannot prove global deletion.
        page.warnings = sorted(
            set(page.warnings + (["missing_timestamp_excluded"] if missing else []))
        )
        return page


def prepare_reader(adapter, selection):
    """Push selection into readers that can avoid opening unrelated local data."""
    source_for_reader(adapter).prepare_reader(adapter, selection)
