"""Static registration only: source configuration cannot import executable code."""

from ..adapters.base import AdapterError
from . import browser, feishu, files, immich, local_ai, notion, qq, screen, wechat

_SOURCES = tuple(
    module.source
    for module in (files, qq, wechat, feishu, notion, immich, browser, local_ai, screen)
)
_BY_ID = {source.adapter_id: source for source in _SOURCES}
_READER_ALIASES = {
    "local." + name: "ai.local-logs"
    for name in (
        "codex",
        "claude-code",
        "gemini-cli",
        "opencode",
        "cursor",
        "logs",
        "sqlite",
    )
}


def sources():
    return _SOURCES


def get_source(adapter_id):
    try:
        return _BY_ID[adapter_id]
    except KeyError:
        raise AdapterError("adapter_unknown") from None


def source_for_reader(reader):
    identity = getattr(reader, "adapter_id", "")
    return get_source(_READER_ALIASES.get(identity, identity))
