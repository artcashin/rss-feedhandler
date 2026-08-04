from __future__ import annotations

import calendar
import hashlib
import re

import feedparser

from .store import NewArticle

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")

# feedparser folds well-known namespaced tags into its own field names, so a
# template naming the tag as it appears in the raw XML needs a bridge.
_TAG_ALIASES = {
    "dc:creator": "author",
    "creator": "author",
    "description": "summary",
}


def _entry_field(entry, name: str) -> str | None:
    """A non-empty string field of the entry, or None.

    Tried as written, then through the raw-XML-tag aliases, then with the
    namespace colon folded to feedparser's underscore convention.
    """
    for key in (name, _TAG_ALIASES.get(name), name.replace(":", "_")):
        if not key:
            continue
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _format_title(template: str, entry, title: str) -> str:
    """Render template against the entry, or fall back to the plain title.

    All-or-nothing on purpose: a missing or blank field would leave a dangling
    separator ("Headline - "), so any unresolvable placeholder abandons the
    template entirely rather than rendering half of it.
    """
    parts: list[str] = []
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(template):
        value = _entry_field(entry, m.group(1))
        if value is None:
            return title
        parts.append(template[pos : m.start()])
        parts.append(value)
        pos = m.end()
    parts.append(template[pos:])
    formatted = "".join(parts).strip()
    return formatted or title


def _published(entry) -> int | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return calendar.timegm(value)
    return None


def _hash_guid(title: str, published_at: int | None) -> str:
    payload = f"{title}\x00{published_at if published_at is not None else ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# now is accepted for API symmetry with parse_feed but unused; published_at
# is None when the entry has no date rather than falling back to now.
def normalize_entry(entry, now: int, title_format: str | None = None) -> NewArticle | None:
    title = (entry.get("title") or "").strip()
    if not title:
        return None
    link = entry.get("link") or None
    summary = entry.get("summary") or None
    published_at = _published(entry)
    # The fallback guid hashes the raw title, before any title_format is
    # applied: retuning a feed's template must not mint new guids and
    # resurrect its whole cached back catalogue as broadcastable news.
    guid = entry.get("id") or link or _hash_guid(title, published_at)
    if title_format:
        title = _format_title(title_format, entry, title)
    return NewArticle(
        guid=guid,
        title=title,
        link=link,
        summary=summary,
        published_at=published_at,
    )


def parse_feed(
    body: bytes, now: int, title_format: str | None = None
) -> tuple[list[NewArticle], int]:
    parsed = feedparser.parse(body)
    entries: list[NewArticle] = []
    dropped = 0
    for raw in parsed.entries:
        item = normalize_entry(raw, now, title_format)
        if item is None:
            dropped += 1
        else:
            entries.append(item)
    return entries, dropped
