from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable

import httpx

from . import __version__
from .config import Config
from .fetch import fetch_feed, next_interval, redact_feed_url, user_agent
from .normalize import parse_feed
from .store import Article, Feed, Store

log = logging.getLogger(__name__)

OnNewArticles = Callable[[list[Article]], Awaitable[None] | None]

# Grace window for the broadcast-age guard below. Publishers stamp pubDate at
# authoring time, and an item can surface in the feed body one or more poll
# intervals later (CDN-cached feeds guarantee it), so a genuinely new
# article's pubDate routinely lands BEFORE the previous successful poll --
# by minutes, not days. The resurrections the guard exists to stop are, by
# construction, at least retention_days old (they were swept), so an hour of
# grace cleanly separates the two without letting any real resurrection
# through.
BROADCAST_GRACE_S = 3600


def _default_jitter() -> float:
    return random.uniform(0.9, 1.1)


class Poller:
    def __init__(
        self,
        store: Store,
        client: httpx.AsyncClient,
        config: Config,
        on_new_articles: OnNewArticles,
        jitter: Callable[[], float] = _default_jitter,
    ) -> None:
        self.store = store
        self.client = client
        self.config = config
        self.on_new_articles = on_new_articles
        self.jitter = jitter
        self._ua = user_agent(__version__)
        self.client.headers["User-Agent"] = self._ua

    def _base_interval(self, feed: Feed) -> int:
        return feed.poll_interval_s or self.config.default_poll_interval_s

    async def poll_feed(self, feed: Feed, now: int) -> list[Article]:
        state = self.store.get_feed_state(feed.id)
        cold_start = state is None or state.last_success_at is None
        etag = state.etag if state else None
        last_modified = state.last_modified if state else None
        failures = state.consecutive_failures if state else 0

        outcome = await fetch_feed(self.client, feed.url, etag, last_modified)

        if outcome.status == "failed":
            delay = next_interval(self._base_interval(feed), failures + 1, outcome.retry_after)
            # Retry-After is a request, not a suggestion: jittering it down
            # would retry EARLIER than the server asked. Backoff keeps its
            # jitter; a server-supplied delay is used exactly.
            if outcome.retry_after is None:
                delay = int(delay * self.jitter())
            self.store.record_failure(
                feed.id, error=outcome.error or "unknown error", now=now,
                next_poll_at=now + int(delay),
            )
            log.warning("Feed %s failed: %s", redact_feed_url(feed.url), outcome.error)
            return []

        delay = int(self._base_interval(feed) * self.jitter())

        if outcome.status == "not_modified":
            if cold_start:
                # We sent no conditional headers (no etag/last_modified exist
                # yet on a first-ever poll), so a spec-compliant server can't
                # legitimately return 304 here. Treat it as a failure: there
                # is nothing cached and nothing succeeded, so recording a
                # success would clear cold-start suppression and cause the
                # next real response to broadcast the whole back catalogue.
                delay = next_interval(self._base_interval(feed), failures + 1, None)
                self.store.record_failure(
                    feed.id,
                    error="304 not modified on an unconditional first poll",
                    now=now,
                    next_poll_at=now + int(delay * self.jitter()),
                )
                log.warning(
                    "Feed %s returned 304 on an unconditional first poll",
                    redact_feed_url(feed.url),
                )
                return []
            self.store.record_success(
                feed.id, etag=etag, last_modified=last_modified, now=now,
                next_poll_at=now + delay,
            )
            return []

        # parse_feed drives feedparser/sgmllib, pure CPU that can run to
        # multiple seconds on a multi-megabyte body. Run it off the event
        # loop thread so it can't freeze every concurrent WebSocket
        # send/accept while it churns. parse_feed touches no shared state
        # (no store, no self), so no locking is needed here.
        entries, dropped = await asyncio.to_thread(
            parse_feed, outcome.body or b"", now, feed.title_format
        )
        if dropped:
            log.info("Feed %s dropped %d unusable entries", redact_feed_url(feed.url), dropped)

        if not entries:
            delay = next_interval(self._base_interval(feed), failures + 1, None)
            self.store.record_failure(
                feed.id, error="feed produced no usable entries", now=now,
                next_poll_at=now + int(delay * self.jitter()),
            )
            return []

        inserted = self.store.insert_articles(feed.id, entries, now)
        self.store.record_success(
            feed.id,
            etag=outcome.etag,
            last_modified=outcome.last_modified,
            now=now,
            next_poll_at=now + delay,
        )

        if cold_start:
            log.info("Feed %s cold start: cached %d articles, broadcast none",
                     redact_feed_url(feed.url), len(inserted))
            return []

        # Broadcast-age guard: `inserted` rows are "new" only in the sense
        # that no (feed_id, guid) row existed before this poll -- that is
        # also true of an article that left the feed, aged past
        # retention_days, was swept by Store.sweep, and has now reappeared.
        # Re-broadcasting that resurrection would push it live pinned to
        # its original (old) publish date, as if it were breaking news.
        # `state.last_success_at` (captured before this poll, non-None
        # here since cold_start is False) is the feed's previous successful
        # poll time: a genuinely new article carries a current date and
        # is >= that time, while a resurrected/swept-and-returned item
        # carries an old date and predates it. Withheld articles are not
        # lost -- they were already inserted above and remain cached and
        # retrievable via paging; they are simply not pushed live with a
        # stale date.
        #
        # Honest limitation: this is a heuristic, not a tombstone. A
        # dateless article (published_at is None) can't be judged by age
        # at all, so it is always broadcast -- a dateless resurrection
        # would slip through. Likewise a publisher backdating an article
        # into the grace window would still pass.
        cutoff = state.last_success_at - BROADCAST_GRACE_S
        return [
            a for a in inserted
            if a.published_at is None or a.published_at >= cutoff
        ]

    async def run_once(self, now: int) -> int:
        feeds = self.store.due_feeds(now)
        if not feeds:
            return 0
        sem = asyncio.Semaphore(self.config.max_concurrent_polls)

        async def guarded(feed: Feed) -> None:
            async with sem:
                try:
                    batch = await self.poll_feed(feed, now)
                except Exception:
                    log.exception("Unexpected error polling %s", redact_feed_url(feed.url))
                    return
            # Published per feed, as soon as its own poll completes -- NOT
            # batched after the whole tick. One stalled fetch (which can ride
            # the full TOTAL_TIMEOUT_S) must not hold up the live push of
            # articles already fetched from healthy feeds. Outside the
            # semaphore so a slow broadcaster doesn't occupy a poll slot.
            if batch:
                try:
                    maybe = self.on_new_articles(batch)
                    if asyncio.iscoroutine(maybe):
                        await maybe
                except Exception:
                    log.exception("Broadcaster failed handling new articles")

        await asyncio.gather(*(guarded(f) for f in feeds))
        return len(feeds)

    async def run_forever(self, tick_s: float = 1.0) -> None:
        while True:
            try:
                await self.run_once(int(time.time()))
            except Exception:
                log.exception("Poller tick failed")
            await asyncio.sleep(tick_s)
