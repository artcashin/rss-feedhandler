from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .store import Article, Store, encode_cursor

log = logging.getLogger(__name__)

MAX_QUEUE = 1000


@dataclass(eq=False)
class Subscription:
    """One open socket: the feed ids it named, and its outbound queue."""

    feed_ids: set[int] = field(default_factory=set)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUE))
    dropped: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)


def article_payload(article: Article, feed_name: str | None) -> dict:
    return {
        "id": article.id,
        "feed_id": article.feed_id,
        "cursor": encode_cursor(article.sort_at, article.id),
        "title": article.title,
        "link": article.link,
        "summary": article.summary,
        "source": feed_name,
        "author": article.author,
        "published_at": article.published_at,
        "sort_at": article.sort_at,
    }


class Broadcaster:
    """Fan-out and the subscriber counts.

    Counts are in memory, derived from open sockets (design decision 3): a
    feed is polled while at least one socket names it. The store's `enabled`
    flag mirrors "count > 0" so the poller's due-feeds query needs no
    knowledge of sockets.
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self._subs: set[Subscription] = set()
        self._counts: dict[int, int] = {}

    def subscribe(self) -> Subscription:
        sub = Subscription()
        self._subs.add(sub)
        return sub

    def set_feeds(self, sub: Subscription, feed_ids: set[int]) -> None:
        """Replace `sub`'s feed set, adjusting counts by the difference."""
        for feed_id in feed_ids - sub.feed_ids:
            self._counts[feed_id] = self._counts.get(feed_id, 0) + 1
            if self._counts[feed_id] == 1:
                self.store.set_enabled(feed_id, True)
        for feed_id in sub.feed_ids - feed_ids:
            remaining = self._counts.get(feed_id, 0) - 1
            if remaining <= 0:
                self._counts.pop(feed_id, None)
                self.store.set_enabled(feed_id, False)
            else:
                self._counts[feed_id] = remaining
        sub.feed_ids = set(feed_ids)

    def unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subs:
            self.set_feeds(sub, set())
            self._subs.discard(sub)

    def subscriber_count(self, feed_id: int) -> int:
        return self._counts.get(feed_id, 0)

    def session_count(self) -> int:
        return len(self._subs)

    async def publish(self, articles: list[Article]) -> None:
        """Every inserted article to every open socket; clients filter."""
        if not articles or not self._subs:
            return
        names: dict[int, str | None] = {}
        for article in articles:
            if article.feed_id not in names:
                feed = self.store.get_feed(article.feed_id)
                names[article.feed_id] = feed.name if feed else None
            payload = article_payload(article, names[article.feed_id])
            for sub in list(self._subs):
                try:
                    sub.queue.put_nowait(payload)
                except asyncio.QueueFull:
                    sub.dropped = True
                    sub.closed.set()
                    self.unsubscribe(sub)
                    log.warning("Dropped a slow subscriber")
