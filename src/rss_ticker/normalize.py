from __future__ import annotations

import calendar
import hashlib

import feedparser

from .store import NewArticle

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
def normalize_entry(entry, now: int) -> NewArticle | None:
    title = (entry.get("title") or "").strip()
    if not title:
        return None
    link = entry.get("link") or None
    summary = entry.get("summary") or None
    author = _entry_field(entry, "author")
    published_at = _published(entry)
    guid = entry.get("id") or link or _hash_guid(title, published_at)
    return NewArticle(
        guid=guid,
        title=title,
        link=link,
        summary=summary,
        published_at=published_at,
        author=author,
    )


def parse_feed(body: bytes, now: int) -> tuple[list[NewArticle], int]:
    parsed = feedparser.parse(body)
    entries: list[NewArticle] = []
    dropped = 0
    for raw in parsed.entries:
        item = normalize_entry(raw, now)
        if item is None:
            dropped += 1
        else:
            entries.append(item)
    return entries, dropped
