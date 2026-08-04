from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .filters import evaluate
from .store import Article, Store, encode_cursor

log = logging.getLogger(__name__)

MAX_QUEUE = 1000


@dataclass(eq=False)
class Subscription:
    user_id: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUE))
    dropped: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)


def article_payload(article: Article, feed_name: str | None, highlighted: bool) -> dict:
    return {
        "id": article.id,
        # The stable key the widget maps to a favicon: the icon lives once per
        # feed in /api/feeds, so an article carries only its feed_id rather than
        # repeating a data URI on every headline.
        "feed_id": article.feed_id,
        "cursor": encode_cursor(article.sort_at, article.id),
        "title": article.title,
        "link": article.link,
        "summary": article.summary,
        "source": feed_name,
        "published_at": article.published_at,
        "sort_at": article.sort_at,
        "highlighted": highlighted,
    }


class Broadcaster:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._subs: dict[str, set[Subscription]] = {}

    def subscribe(self, user_id: str) -> Subscription:
        sub = Subscription(user_id=user_id)
        self._subs.setdefault(user_id, set()).add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        peers = self._subs.get(sub.user_id)
        if peers:
            peers.discard(sub)
            if not peers:
                self._subs.pop(sub.user_id, None)

    def subscriber_count(self, user_id: str) -> int:
        return len(self._subs.get(user_id, ()))

    async def publish(self, articles: list[Article]) -> None:
        if not articles or not self._subs:
            return

        feed_names: dict[int, str | None] = {}
        feed_users: dict[int, list[str]] = {}
        for article in articles:
            if article.feed_id not in feed_names:
                feed = self.store.get_feed(article.feed_id)
                feed_names[article.feed_id] = feed.name if feed else None
                feed_users[article.feed_id] = self.store.subscribers_of(article.feed_id)

        rules_cache: dict[str, list] = {}

        for article in articles:
            for user_id in feed_users[article.feed_id]:
                targets = self._subs.get(user_id)
                if not targets:
                    continue
                if user_id not in rules_cache:
                    rules_cache[user_id] = self.store.filters_for(user_id)
                included, highlighted = evaluate(
                    rules_cache[user_id], article.title, article.summary
                )
                if not included:
                    continue
                payload = article_payload(
                    article, feed_names[article.feed_id], highlighted
                )
                for sub in list(targets):
                    try:
                        sub.queue.put_nowait(payload)
                    except asyncio.QueueFull:
                        sub.dropped = True
                        sub.closed.set()
                        self.unsubscribe(sub)
                        log.warning("Dropped slow subscriber for user %s", user_id)
