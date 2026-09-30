from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from .config import Channel
from .fetch import TIMEOUT_S

log = logging.getLogger(__name__)

# YouTube embeds the player state as a JSON object assigned to
# `ytInitialPlayerResponse` in an inline <script>. We read the current video id
# from videoDetails.videoId and only trust it when the page says the stream is
# live *now* -- a channel's /@handle/live URL also serves upcoming and ended
# streams (CNBC's did on 2026-09-29), and framing one of those would show a
# countdown or a dead player instead of live TV.
_MARKER = "ytInitialPlayerResponse"
# An id is exactly 11 URL-safe base64 characters. Anything else is a parser
# artifact, not a real id, so it is rejected rather than framed.
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")

# Cache windows. A fresh entry is served without refetching for 5 minutes
# (the rate ceiling: <=1 outbound request per channel per 5 minutes). On a
# fetch/parse ERROR the last confirmed-live id is served for up to 30 minutes
# after it was obtained, so a brief YouTube blip doesn't blank the card.
LIVE_TTL_S = 300
STALE_TTL_S = 1800


def parse_live_video_id(html: str) -> str | None:
    """The current video id iff `html` is a YouTube page that is live now.

    Returns None for an upcoming stream, an ended/offline stream, a page with
    no player data (e.g. a consent interstitial), or any parse failure. Never
    raises: hostile or truncated input is a None, not an exception.
    """
    idx = html.find(_MARKER)
    if idx == -1:
        return None
    brace = html.find("{", idx)
    if brace == -1:
        return None
    try:
        # raw_decode parses one JSON object starting at `brace` and ignores the
        # trailing `;</script>...`, so we never have to guess where the object
        # ends by counting braces ourselves.
        data, _ = json.JSONDecoder().raw_decode(html, brace)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    details = data.get("videoDetails")
    if not isinstance(details, dict):
        return None
    vid = details.get("videoId")
    if not isinstance(vid, str) or not _VIDEO_ID_RE.fullmatch(vid):
        return None
    # Live now is the tight signal: a currently-live watch page carries
    # videoDetails.isLive == true. Upcoming and ended pages don't, and an ended
    # stream additionally reports playabilityStatus.status LIVE_STREAM_OFFLINE.
    # ponytail: heuristic on YouTube's private JSON shape; if YouTube renames a
    # marker the card shows off-air (safe), and the fix is to adjust these keys.
    if details.get("isLive") is not True:
        return None
    if details.get("isUpcoming") is True:
        return None
    status = (data.get("playabilityStatus") or {}).get("status")
    if status == "LIVE_STREAM_OFFLINE":
        return None
    return vid


@dataclass
class _Entry:
    # `cached` is the answer currently served; `cached_at` anchors the 5-minute
    # freshness window. `good_id`/`good_at` remember the last confirmed-live id
    # and when it was obtained, which is what the 30-minute stale-on-error
    # window is measured against (from obtain time, not from the error).
    cached: str | None = None
    cached_at: float = float("-inf")
    good_id: str | None = None
    good_at: float = float("-inf")


class LiveTV:
    """Per-channel live-video-id cache backed by on-demand fetches.

    Lazy by design (see the plan): a channel is fetched only when asked and at
    most once per 5 minutes. A per-channel lock makes concurrent requests share
    a single fetch -- the waiters re-check freshness after acquiring the lock
    and return the just-cached answer without a second outbound request.
    """

    def __init__(
        self,
        channels: tuple[Channel, ...],
        client_getter: Callable[[], httpx.AsyncClient | None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.channels = tuple(channels)
        self._by_key = {c.key: c for c in self.channels}
        self._client_getter = client_getter
        self._clock = clock
        self._entries = {c.key: _Entry() for c in self.channels}
        # Locks are created once at construction (all keys are known upfront),
        # so there is no race to create a lock for a key.
        self._locks = {c.key: asyncio.Lock() for c in self.channels}

    def __contains__(self, key: str) -> bool:
        return key in self._by_key

    async def video_id(self, key: str) -> str | None:
        entry = self._entries[key]
        async with self._locks[key]:
            now = self._clock()
            if now - entry.cached_at < LIVE_TTL_S:
                # Fresh -- also the path a waiter that lost the race takes, so
                # the shared fetch is genuinely shared (one outbound request).
                return entry.cached
            try:
                vid = await self._fetch(self._by_key[key].handle)
            except Exception:
                # Network/HTTP error: keep serving the last good id while it is
                # inside the 30-minute window, otherwise empty. good_id/good_at
                # are left untouched so the window keeps measuring from obtain.
                log.warning("Live lookup for channel %s failed", key)
                if entry.good_id is not None and now - entry.good_at < STALE_TTL_S:
                    entry.cached = entry.good_id
                else:
                    entry.cached = None
                entry.cached_at = now
                return entry.cached
            if vid is not None:
                entry.good_id = vid
                entry.good_at = now
                entry.cached = vid
            else:
                # A clean "not live" page: the stream ended. Serve empty now and
                # forget the good id, so a later error can't resurrect it.
                entry.good_id = None
                entry.cached = None
            entry.cached_at = now
            return entry.cached

    async def _fetch(self, handle: str) -> str | None:
        client = self._client_getter()
        if client is None:
            # No client yet (called outside the app lifespan). Treated as an
            # error by the caller, which is the safe direction.
            raise RuntimeError("no http client available")
        # ponytail: reuses the shared client's rss-ticker User-Agent. If YouTube
        # starts serving a consent interstitial to it, add a browser UA +
        # Accept-Language here (see plan Task 3 decision).
        resp = await client.get(
            f"https://www.youtube.com/@{handle.lstrip('@')}/live",
            timeout=TIMEOUT_S,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return parse_live_video_id(resp.text)
