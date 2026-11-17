"""Source selection contract; no queues, persistence, or memory model."""

from datetime import datetime
import re
import json
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scopes: list[str] = Field(min_length=1, max_length=200)
    start: datetime | None = None
    end: datetime | None = None
    revisions: dict[str, str] = Field(default_factory=dict, max_length=200)
    locators: dict[str, str] = Field(default_factory=dict, max_length=200)
    include_attachments: bool = False
    messages: dict[str, dict[str, str]] = Field(default_factory=dict, max_length=200)

    @model_validator(mode="after")
    def validate_range(self):
        if len(set(self.scopes)) != len(self.scopes) or any(
            not s
            or len(s) > 2048
            or any(ord(c) < 32 or ord(c) == 127 or "\ud800" <= c <= "\udfff" for c in s)
            for s in self.scopes
        ):
            raise ValueError("invalid scopes")
        if any(t and t.tzinfo is None for t in (self.start, self.end)):
            raise ValueError("timezone required")
        if self.start and self.end and self.start >= self.end:
            raise ValueError("empty range")
        if any(k not in self.scopes or len(v) > 256 for k, v in self.revisions.items()):
            raise ValueError("invalid revisions")
        if any(
            k not in self.scopes or not v or len(v) > 2048
            for k, v in self.locators.items()
        ):
            raise ValueError("invalid locators")
        if sum(len(v) for v in self.messages.values()) > 500 or any(
            scope not in self.scopes
            or not chosen
            or any(
                not identity
                or len(identity) > 512
                or any(
                    ord(c) < 32 or ord(c) == 127 or "\ud800" <= c <= "\udfff"
                    for c in identity
                )
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                for identity, digest in chosen.items()
            )
            for scope, chosen in self.messages.items()
        ):
            raise ValueError("invalid message selection")
        if (
            len(
                json.dumps(
                    self.messages, ensure_ascii=False, separators=(",", ":")
                ).encode()
            )
            > 256 * 1024
        ):
            raise ValueError("message selection too large")
        return self


def in_time(value, selection):
    if not selection.start and not selection.end:
        return True
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return False
    except (ValueError, TypeError, AttributeError):
        return False
    return (not selection.start or stamp >= selection.start) and (
        not selection.end or stamp < selection.end
    )
