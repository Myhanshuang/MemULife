"""Source-reader contracts for paginated material delivery.

Readers return candidate cursors. Import orchestration confirms each cursor
after all required records in its page have been durably handed off.
"""

from dataclasses import dataclass, field
from typing import Protocol


class AdapterError(Exception):
    """A stable, non-sensitive error code suitable for logs and JSON-RPC."""

    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass
class Attachment:
    attachment_id: str
    data: bytes | None = None
    media_type: str = "application/octet-stream"
    source_uri: str | None = None
    status: str = "pending"
    metadata: dict = field(default_factory=dict)


@dataclass
class Snapshot:
    object_id: str
    raw: bytes
    text: str | None = None
    media_type: str = "application/json"
    revision: str | None = None
    deleted: bool = False
    occurred_at: str | None = None
    metadata: dict = field(default_factory=dict)
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class Page:
    records: list[Snapshot]
    cursor: dict
    has_more: bool = False
    scope_complete: bool = False
    warnings: list[str] = field(default_factory=list)
    watch_checkpoint: dict = field(default_factory=dict)


def validate_page(page: Page) -> None:
    """Reject malformed reader output before saving blobs or advancing a cursor."""
    if (
        not isinstance(page, Page)
        or not isinstance(page.records, list)
        or len(page.records) > 100
        or not isinstance(page.cursor, dict)
        or type(page.has_more) is not bool
        or type(page.scope_complete) is not bool
        or not isinstance(page.warnings, list)
        or any(not isinstance(warning, str) for warning in page.warnings)
    ):
        raise AdapterError("source_page_invalid")
    for record in page.records:
        if (
            not isinstance(record, Snapshot)
            or not isinstance(record.raw, bytes)
            or not isinstance(record.object_id, str)
            or not record.object_id
            or record.text is not None
            and not isinstance(record.text, str)
            or not isinstance(record.metadata, dict)
            or type(record.deleted) is not bool
            or not isinstance(record.attachments, list)
            or record.revision is not None
            and not isinstance(record.revision, str)
        ):
            raise AdapterError("source_record_invalid")
        names = set()
        for attachment in record.attachments:
            if (
                not isinstance(attachment, Attachment)
                or not isinstance(attachment.attachment_id, str)
                or not attachment.attachment_id
                or attachment.attachment_id in names
                or attachment.data is not None
                and not isinstance(attachment.data, bytes)
                or not isinstance(attachment.metadata, dict)
            ):
                raise AdapterError("source_attachment_invalid")
            names.add(attachment.attachment_id)


class SourceAdapter(Protocol):
    def probe(self) -> dict: ...

    def read(self, mode: str, cursor: dict | None = None, limit: int = 50) -> Page: ...

    def health(self) -> dict: ...

    def fetch_attachment(self, ref: dict) -> Attachment: ...
