"""Stable, typed repository locators. No filesystem paths are accepted here."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re


_PATTERN = re.compile(
    r"(?P<alias>[cusra][1-9][0-9]*)(?:(?::(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?)|"
    r"(?:@(?P<t0>[0-9]+(?:\.[0-9]+)?)-(?P<t1>[0-9]+(?:\.[0-9]+)?)))?\Z"
)


def _number(value: Decimal) -> str:
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class Ref:
    alias: str
    start: int | None = None
    end: int | None = None
    start_seconds: Decimal | None = None
    end_seconds: Decimal | None = None

    @property
    def kind(self) -> str:
        return self.alias[0]

    @classmethod
    def parse(cls, value: str | Ref) -> Ref:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str) or not (match := _PATTERN.fullmatch(value)):
            raise ValueError(f"Invalid locator: {value!r}")
        alias = match["alias"]
        start = int(match["start"]) if match["start"] else None
        end = int(match["end"]) if match["end"] else start
        try:
            t0 = Decimal(match["t0"]) if match["t0"] else None
            t1 = Decimal(match["t1"]) if match["t1"] else None
        except InvalidOperation as error:
            raise ValueError("Invalid media time") from error
        if end is not None and end < start:
            raise ValueError("Locator line range is reversed")
        if t0 is not None and t1 <= t0:
            raise ValueError("Media range must have positive duration")
        kind = alias[0]
        if kind == "r" and (start is not None or t0 is not None):
            raise ValueError("View locators may reference only the file entrance")
        if kind == "u" and (start is None or match["end"] or t0 is not None):
            raise ValueError("Unit locators require exactly one starting line")
        if kind in {"c", "s"} and (start is None or t0 is not None):
            raise ValueError("Text and detail locators require a line range")
        if kind == "a" and start is not None:
            raise ValueError("Media locators do not have line numbers")
        return cls(alias, start, end, t0, t1)

    @property
    def canonical(self) -> str:
        if self.start is not None:
            suffix = f":{self.start}"
            if self.end != self.start:
                suffix += f"-{self.end}"
            return self.alias + suffix
        if self.start_seconds is not None:
            return f"{self.alias}@{_number(self.start_seconds)}-{_number(self.end_seconds)}"
        return self.alias

    def __str__(self) -> str:
        return self.canonical

    def contains(self, other: str | Ref) -> bool:
        other = Ref.parse(other)
        if self.alias != other.alias:
            return False
        if self.kind in {"c", "s"}:
            return self.start <= other.start <= other.end <= self.end
        if self.kind == "u":
            return self.start == other.start
        if self.kind == "a":
            if self.start_seconds is None:
                return True
            return (other.start_seconds is not None and
                    self.start_seconds <= other.start_seconds < other.end_seconds <= self.end_seconds)
        return True


def merge_refs(values: list[str]) -> list[str]:
    """Union overlapping/adjacent *selected* text ranges, never bridge gaps."""
    parsed = [Ref.parse(value) for value in values]
    by_alias: dict[str, list[Ref]] = {}
    for ref in parsed:
        by_alias.setdefault(ref.alias, []).append(ref)
    result: list[str] = []
    for group in by_alias.values():
        if group[0].kind not in {"c", "s"}:
            result.extend(dict.fromkeys(ref.canonical for ref in group))
            continue
        current = None
        for ref in sorted(group, key=lambda item: (item.start, item.end)):
            if current is not None and ref.start <= current.end + 1:
                current = Ref(current.alias, current.start, max(current.end, ref.end))
            else:
                if current is not None:
                    result.append(current.canonical)
                current = ref
        if current is not None:
            result.append(current.canonical)
    return result
