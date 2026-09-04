import asyncio
from pathlib import Path

import httpx
import pytest

from rss_ticker.broadcast import Broadcaster
from rss_ticker.config import Config
from rss_ticker.poller import Poller
from rss_ticker.store import Store

ROUND1 = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Backfilled one</title><guid>urn:1</guid></item>
<item><title>Backfilled two</title><guid>urn:2</guid></item></channel></rss>"""

ROUND2 = ROUND1.replace(
    b"<item><title>Backfilled one</title>",
    b"<item><title>Breaking now</title><guid>urn:3</guid></item>"
    b"<item><title>Backfilled one</title>",
)


@pytest.fixture
def wiring(tmp_path: Path):
    config = Config(retention_days=7, default_poll_interval_s=1)
    store = Store(str(tmp_path / "t.db"))
    live = store.upsert_feed("https://live.example/rss", name="Live", now=0)
    dead = store.upsert_feed("https://dead.example/rss", name="Dead", now=0)
    broadcaster = Broadcaster(store)
    sub = broadcaster.subscribe()
    broadcaster.set_feeds(sub, {live, dead})
    yield config, store, broadcaster, sub, tmp_path
    store.close()


def poller_for(store, config, broadcaster, bodies):
    def handler(request):
        if "dead" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, content=bodies[0])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Poller(store, client, config, on_new_articles=broadcaster.publish, jitter=lambda: 1.0)


async def test_new_article_reaches_a_live_subscriber(wiring):
    config, store, broadcaster, sub, _ = wiring
    bodies = [ROUND1]
    poller = poller_for(store, config, broadcaster, bodies)
    await poller.run_once(now=100)  # cold start: cache, no broadcast
    assert sub.queue.empty()
    bodies[0] = ROUND2
    store.db.execute("UPDATE feed_state SET next_poll_at = 0")
    store.db.commit()
    await poller.run_once(now=200)
    msg = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
    assert msg["title"] == "Breaking now"
    assert sub.queue.empty(), "only genuinely new articles should broadcast"


async def test_cold_start_articles_are_scrollable_but_were_not_broadcast(wiring):
    config, store, broadcaster, sub, _ = wiring
    poller = poller_for(store, config, broadcaster, [ROUND1])
    await poller.run_once(now=100)
    assert sub.queue.empty()
    rows, _ = store.page_news(limit=10)
    assert {r.title for r in rows} == {"Backfilled one", "Backfilled two"}


async def test_failing_feed_does_not_stop_the_healthy_one(wiring):
    config, store, broadcaster, _, _ = wiring
    poller = poller_for(store, config, broadcaster, [ROUND1])
    await poller.run_once(now=100)
    status = {f["name"]: f for f in store.all_feed_status()}
    assert status["Dead"]["consecutive_failures"] == 1
    assert status["Live"]["last_success_at"] == 100


async def test_a_feed_nobody_names_is_not_polled_and_is_dropped_by_the_sweep(wiring):
    config, store, broadcaster, sub, _ = wiring
    live = store.feed_by_url("https://live.example/rss").id
    broadcaster.set_feeds(sub, {live})  # drop Dead
    poller = poller_for(store, config, broadcaster, [ROUND1])
    assert await poller.run_once(now=100) == 1
    assert store.drop_disabled_feeds() == 1
    assert [f.name for f in store.all_feeds()] == ["Live"]


async def test_cache_survives_a_restart(wiring):
    config, store, broadcaster, _, tmp_path = wiring
    poller = poller_for(store, config, broadcaster, [ROUND1])
    await poller.run_once(now=100)
    store.close()
    reopened = Store(str(tmp_path / "t.db"))
    try:
        seen: list[str] = []
        before = None
        while True:
            page, before = reopened.page_news(limit=1, before=before)
            seen += [r.title for r in page]
            if before is None:
                break
        assert sorted(seen) == ["Backfilled one", "Backfilled two"]
    finally:
        reopened.close()
