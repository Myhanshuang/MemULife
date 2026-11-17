"""Shared strict model outputs and event-time operations."""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .refs import Ref


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _precision(value: str) -> str:
    if re.fullmatch(r"[0-9]{4}", value):
        date(int(value), 1, 1)
        return "year"
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        date.fromisoformat(value + "-01")
        return "month"
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        date.fromisoformat(value)
        return "day"
    if "T" not in value:
        raise ValueError("Time must use an ISO calendar value or timezone-aware instant")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.utcoffset() is None:
        raise ValueError("Instant requires an explicit timezone offset")
    return "instant"


class TimeRange(StrictModel):
    from_: str = Field(alias="from")
    to: str


class Time(StrictModel):
    precision: Literal["instant", "day", "month", "year", "range", "unknown"]
    value: str | TimeRange | None

    @model_validator(mode="after")
    def check_value(self):
        if self.precision == "unknown":
            if self.value is not None:
                raise ValueError("Unknown time must have null value")
        elif self.precision == "range":
            if not isinstance(self.value, TimeRange):
                raise ValueError("Range time requires from/to")
            begin = _precision(self.value.from_)
            finish = _precision(self.value.to)
            if begin != finish:
                raise ValueError("Range endpoints must use the same precision")
            if begin == "instant":
                left = datetime.fromisoformat(self.value.from_.replace("Z", "+00:00"))
                right = datetime.fromisoformat(self.value.to.replace("Z", "+00:00"))
            else:
                left, right = self.value.from_, self.value.to
            if left > right:
                raise ValueError("Time range is reversed")
        elif not isinstance(self.value, str) or _precision(self.value) != self.precision:
            raise ValueError("Time value does not match its precision")
        return self


class Entry(StrictModel):
    time: Time
    body: str = Field(min_length=1)
    facts: list[str] = Field(min_length=1)

    @field_validator("body")
    @classmethod
    def clean_body(cls, value):
        if not value.strip():
            raise ValueError("Entry body must not be blank")
        # Technical conversations can discuss literal locators. Storage escapes
        # body delimiters; only the separate facts/relations fields create links.
        return value

    @field_validator("facts")
    @classmethod
    def valid_facts(cls, values):
        return [Ref.parse(value).canonical for value in values]


class UnitDraft(Entry):
    topics: list[str]

    @field_validator("topics")
    @classmethod
    def safe_topics(cls, topics):
        for topic in topics:
            if not topic.strip() or any(char in topic for char in "\n\r\t,^|;"):
                raise ValueError("Topic must be a nonblank name without structural separators")
        return list(dict.fromkeys(topics))


class Relation(StrictModel):
    rel: str = Field(min_length=1)
    target: str

    @field_validator("target")
    @classmethod
    def unit_target(cls, value):
        ref = Ref.parse(value)
        if ref.kind != "u":
            raise ValueError("Relations must target an existing Unit")
        return ref.canonical


class EntriesOutput(StrictModel):
    entries: list[Entry]


class SummaryMatter(StrictModel):
    matter: str = Field(min_length=1)
    refs: list[str] = Field(min_length=1)


class SummaryOutput(StrictModel):
    # Generate the complete grouping before prose, not after filling entry slots.
    agenda: list[SummaryMatter]
    entries: list[Entry]


class UnitsOutput(StrictModel):
    units: list[UnitDraft]


class TopicProposal(StrictModel):
    key: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    facts: list[str] = Field(min_length=1)

    @field_validator('key')
    @classmethod
    def valid_key(cls, value):
        return UnitDraft.safe_topics([value.strip()])[0]

    @field_validator('facts')
    @classmethod
    def unit_facts(cls, values):
        refs = [Ref.parse(value) for value in values]
        if any(ref.kind != 'u' for ref in refs):
            raise ValueError('Topic evidence must cite Units')
        return list(dict.fromkeys(ref.canonical for ref in refs))


class TopicsOutput(StrictModel):
    topics: list[TopicProposal]


class ContextMemory(StrictModel):
    summary: str = Field(min_length=1)
    facts: list[str] = Field(min_length=1)


class MaterialCoverage(StrictModel):
    item: int = Field(ge=0)
    new: list[int]
    previous: list[str]
    ignored: Literal['operational_context'] | None = None


class CompactEntriesOutput(EntriesOutput):
    memory: ContextMemory
    coverage: list[MaterialCoverage]


class CompactUnitsOutput(UnitsOutput):
    memory: ContextMemory
    coverage: list[MaterialCoverage]


class WorklogEntry(StrictModel):
    body: str = Field(min_length=1)
    facts: list[str] = Field(default_factory=list)
    anchor: str

    @field_validator('body')
    @classmethod
    def clean_body(cls, value):
        return Entry.clean_body(value)

    @field_validator('facts')
    @classmethod
    def valid_facts(cls, values):
        return Entry.valid_facts(values)


class WorklogUnitDraft(WorklogEntry):
    topics: list[str]

    @field_validator('topics')
    @classmethod
    def safe_topics(cls, topics):
        return UnitDraft.safe_topics(topics)


class WorklogEntriesOutput(StrictModel):
    entries: list[WorklogEntry]
    memory: ContextMemory


class WorklogUnitsOutput(StrictModel):
    units: list[WorklogUnitDraft]
    memory: ContextMemory


class RelationsOutput(StrictModel):
    relations: list[Relation]


def _calendar_parts(value: str, timezone: str) -> list[str]:
    precision = _precision(value)
    if precision == "instant":
        value = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo(timezone)).date().isoformat()
    return value.split("-")


def archive_directory(value: Time | dict[str, Any], timezone: str = "Asia/Singapore") -> str:
    event = value if isinstance(value, Time) else Time.model_validate(value)
    if event.precision == "unknown":
        return "_undated"
    if event.precision == "range":
        left = _calendar_parts(event.value.from_, timezone)
        right = _calendar_parts(event.value.to, timezone)
        common = []
        for a, b in zip(left, right):
            if a != b:
                break
            common.append(a)
        return "/".join(common) if common else "_undated"
    parts = _calendar_parts(event.value, timezone)
    if event.precision in {"year", "month"}:
        parts.append("_undated")
    return "/".join(parts)


def time_scopes(value: Time | dict[str, Any], timezone: str = "Asia/Singapore") -> dict[str, list[str]]:
    """Assign a day/month only when the event is wholly inside that period."""
    event = value if isinstance(value, Time) else Time.model_validate(value)
    directory = archive_directory(event, timezone)
    parts = directory.split("/")
    days = ["-".join(parts[:3])] if len(parts) >= 3 and parts[2] != "_undated" else []
    months = ["-".join(parts[:2])] if len(parts) >= 2 and parts[1] != "_undated" else []
    return {"days": days, "months": months}
