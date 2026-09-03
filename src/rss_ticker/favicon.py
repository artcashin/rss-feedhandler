from __future__ import annotations

import asyncio
import base64
import html.parser
import logging
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import httpx

from .fetch import redact_feed_url

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger(__name__)

# A favicon is a cosmetic nicety, not something ingest should ever wait on.
# Short total deadline so a slow/hostile host can't stall a poll cycle.
FAVICON_TIMEOUT_S = 10.0
# Favicons are small; one is stored per feed (as a data URI, in the feeds
# table) and later sent whole in /api/feeds, so 128 KB raw is a safe ceiling
# that keeps that payload bounded without rejecting a real-world icon --
# CNBC's own favicon.ico is a 101 KB multi-resolution bundle (verified
# 2026-08-06), and the previous 50 KB cap silently rejected it.
MAX_FAVICON_BYTES = 128 * 1024

# RSS-syndication hosts that are not real websites and refuse every request
# outright (verified 2026-08-06: 403/404/503, independent of headers). The
# publisher's real public domain resolves a favicon fine.
_KNOWN_PROVIDER_HOSTS: dict[str, str] = {
    "feeds.bloomberg.com": "bloomberg.com",
    "feeds.content.dowjones.io": "wsj.com",
    "search.cnbc.com": "cnbc.com",
}

# Two of the three known-provider domains (wsj.com, cnbc.com) 403 a request
# that doesn't look like a browser; bloomberg.com doesn't care either way.
# Applied only to known-provider hosts -- an ordinary feed's site must not
# start impersonating a browser for no reason. This only ever requests
# /favicon.ico and the bare homepage, never an article or anything behind a
# login, so it can't touch a paywall or auth wall.
_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# The homepage is only ever parsed for a <link rel="icon"> tag, never
# rendered or stored, so it gets its own (larger, but still bounded) cap --
# reusing the favicon-sized cap would reject perfectly normal homepages
# before the <link> in <head> is even reached.
MAX_HTML_BYTES = 512 * 1024

_ICON_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG", "image/png"),
    (b"GIF8", "image/gif"),
    (b"\xff\xd8", "image/jpeg"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
)


def _mime_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    # Strip `; charset=...` etc.
    mime = content_type.split(";", 1)[0].strip().lower()
    return mime or None


def _looks_like_svg(body: bytes) -> bool:
    head = body[:256].lstrip()
    return head.startswith(b"<svg") or (
        (head.startswith(b"<?xml") or head.startswith(b"<")) and b"<svg" in body[:1024]
    )


def _mime_for(content_type: str | None, body: bytes) -> str | None:
    """Decide the MIME type to embed in the data URI, or None if `body`
    doesn't look like an image/icon at all.
    """
    ct = _mime_from_content_type(content_type)
    if ct == "image/svg+xml" or _looks_like_svg(body):
        return "image/svg+xml"
    if ct and ct.startswith("image/"):
        return ct
    for magic, mime in _ICON_MAGIC:
        if body.startswith(magic):
            return mime
    return None


class _IconLinkParser(html.parser.HTMLParser):
    """Collect the href of the first <link rel="...icon..."> tag found."""

    def __init__(self) -> None:
        super().__init__()
        self.href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.href is not None or tag.lower() != "link":
            return
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        rel = attr_map.get("rel", "")
        if "icon" in rel.lower():
            href = attr_map.get("href")
            if href:
                self.href = href


async def _bounded_get(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> httpx.Response | None:
    """GET `url`, following redirects, reading at most `max_bytes` + 1 bytes.

    Returns the response (with `.content` populated) on a 200 whose body did
    not exceed the cap, or None for any non-200 status, an oversized body, or
    any error. Mirrors fetch.py's fetch_feed shape: a streamed read with an
    incremental size counter, bounded per-call by FAVICON_TIMEOUT_S.
    """
    async with client.stream(
        "GET", url, timeout=FAVICON_TIMEOUT_S, follow_redirects=True, headers=headers
    ) as response:
        if response.status_code != 200:
            return None
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
        body = b"".join(chunks)
        # Stash the body somewhere callers can get at it without re-reading
        # the (already-exhausted) stream.
        response._favicon_body = body  # type: ignore[attr-defined]
        return response


def _data_uri(mime: str, body: bytes) -> str:
    encoded = base64.b64encode(body).decode("ascii")
    return f"data:{mime};base64,{encoded}"


async def _try_fetch_icon(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> str | None:
    response = await _bounded_get(client, url, MAX_FAVICON_BYTES, headers=headers)
    if response is None:
        return None
    body: bytes = response._favicon_body  # type: ignore[attr-defined]
    mime = _mime_for(response.headers.get("content-type"), body)
    if mime is None:
        return None
    return _data_uri(mime, body)


async def resolve_favicon(client: httpx.AsyncClient, feed_url: str) -> str | None:
    """Best-effort resolution of a feed's site favicon as a data URI.

    Tries `https://<host>/favicon.ico` first (the feed's own host, unless
    it's a known syndication endpoint -- see `_KNOWN_PROVIDER_HOSTS`), then
    falls back to parsing `<link rel="icon">` (or "shortcut icon" /
    "apple-touch-icon") out of the homepage. Returns None on any failure --
    bad URL, network error, no icon found, an oversized body, or a 200
    response that isn't actually an image (e.g. an HTML error page). Never
    raises.
    """
    try:
        async with asyncio.timeout(FAVICON_TIMEOUT_S * 2):
            parts = urlsplit(feed_url)
            host = parts.hostname
            if not host or parts.scheme not in ("http", "https"):
                return None

            # A feed's own host is sometimes a syndication endpoint rather
            # than the publisher's real site (_KNOWN_PROVIDER_HOSTS); resolve
            # against the real domain in that case, with headers that make
            # the request look like a browser rather than a script.
            resolve_host = _KNOWN_PROVIDER_HOSTS.get(host, host)
            headers = _BROWSER_HEADERS if host in _KNOWN_PROVIDER_HOSTS else None

            favicon_ico_url = f"https://{resolve_host}/favicon.ico"
            direct = await _try_fetch_icon(client, favicon_ico_url, headers=headers)
            if direct is not None:
                return direct

            homepage_url = f"https://{resolve_host}/"
            homepage = await _bounded_get(client, homepage_url, MAX_HTML_BYTES, headers=headers)
            if homepage is None:
                return None
            html_body: bytes = homepage._favicon_body  # type: ignore[attr-defined]
            try:
                text = html_body.decode("utf-8", errors="replace")
            except Exception:
                return None

            parser = _IconLinkParser()
            parser.feed(text)
            if not parser.href:
                return None

            # May land on a third host (e.g. a CDN) -- carry the same
            # headers there too, since that host can gate on them just as
            # easily as the homepage did.
            icon_url = urljoin(str(homepage.url), parser.href)
            return await _try_fetch_icon(client, icon_url, headers=headers)
    except Exception:
        return None


async def resolve_and_store(store: Store, client: httpx.AsyncClient, feed) -> None:
    """Resolve one feed's favicon and store it; never raises, never wipes a
    known icon on failure. Used at startup for every feed and at subscribe
    time for a feed new to the pool (decision D)."""
    try:
        icon = await resolve_favicon(client, feed.url)
        if icon:
            store.set_feed_favicon(feed.id, icon)
    except Exception as exc:
        log.debug("Favicon resolution failed for %s: %s", redact_feed_url(feed.url), exc)


async def refresh_favicons(
    store: Store, client: httpx.AsyncClient, *, concurrency: int = 8
) -> None:
    """One-shot startup pass: re-check every feed's favicon.

    Runs once, at server startup, and never again on the poll path -- polling
    must stay favicon-free. Every feed is re-resolved, not just those missing
    an icon, so a site that changed its favicon since the last restart picks
    up the new one here. A feed whose re-check fails (a hung host, a bad
    response, a network error) or yields nothing simply keeps its last-known
    icon -- a transient failure must never wipe out a good one. There is no
    in-process retry beyond this one pass: the next restart is what
    re-attempts it. A feed that joins the pool later does not wait for that
    restart -- the subscribe path resolves it on its own (decision D) via
    resolve_and_store, since this function itself only runs once, at this
    startup.

    Resolution work is fanned out concurrently across every feed, but bounded
    by a semaphore so a large feed list can't open an unbounded number of
    connections at once.

    Never affects serving or polling: every feed's resolve-and-store is
    individually guarded, so one bad feed (a hung host, a bad response, a
    store error) can never abort the others or propagate into the lifespan
    that started this task. Only the redacted host is ever logged -- never
    the raw feed URL, never an embedded credential.
    """
    try:
        feeds = store.all_feeds()
    except Exception as exc:
        log.debug("Could not list feeds to refresh favicons for: %s", exc)
        return
    if not feeds:
        return

    sem = asyncio.Semaphore(max(1, concurrency))

    async def resolve_one(feed) -> None:
        async with sem:
            await resolve_and_store(store, client, feed)

    await asyncio.gather(*(resolve_one(f) for f in feeds))
