from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FilterRule:
    pattern: str
    action: str


def _haystack(title: str, summary: str | None) -> str:
    return f"{title} {summary or ''}".lower()


def include_patterns(rules: list[FilterRule]) -> list[str]:
    return [r.pattern.lower() for r in rules if r.action == "include"]


def highlights(rules: list[FilterRule], title: str, summary: str | None) -> bool:
    hay = _haystack(title, summary)
    return any(r.pattern.lower() in hay for r in rules if r.action == "highlight")


def evaluate(rules: list[FilterRule], title: str, summary: str | None) -> tuple[bool, bool]:
    hay = _haystack(title, summary)
    includes = include_patterns(rules)
    included = True if not includes else any(p in hay for p in includes)
    return included, highlights(rules, title, summary)
