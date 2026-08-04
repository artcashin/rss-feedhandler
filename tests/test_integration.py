import asyncio
from pathlib import Path

import httpx
import pytest

from rss_ticker.broadcast import Broadcaster
from rss_ticker.config import load_config
from rss_ticker.poller import Poller
from rss_ticker.reconcile import reconcile
from rss_ticker.store import Store

TOKEN = "tkn-" + "0123456789abcdef" * 3

CONFIG = f"""
public_base_url: http://localhost:8088
admin_key: k
manifest_key: mk
retention_days: 7
default_poll_interval_s: 1
users:
  - id: art
    token: {TOKEN}
    feeds:
      - {{url: "https://live.example/rss", name: Live}}
      - {{url: "https://dead.example/rss", name: Dead}}
"""

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
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(CONFIG)
    config = load_config(cfg_path, {})
    store = Store(str(tmp_path / "t.db"))
    reconcile(store, config, now=0)
    broadcaster = Broadcaster(store)
    yield config, store, broadcaster, tmp_path
    store.close()


def poller_for(store, config, broadcaster, bodies):
    def handler(request):
        if "dead" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, content=bodies[0])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Poller(store, client, config, on_new_articles=broadcaster.publish,
                  jitter=lambda: 1.0)


async def test_new_article_reaches_a_live_subscriber(wiring):
    config, store, broadcaster, _ = wiring
    bodies = [ROUND1]
    poller = poller_for(store, config, broadcaster, bodies)

    await poller.run_once(now=100)          # cold start: cache, no broadcast
    sub = broadcaster.subscribe("art")
    bodies[0] = ROUND2
    store.db.execute("UPDATE feed_state SET next_poll_at = 0")
    store.db.commit()
    await poller.run_once(now=200)

    msg = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
    assert msg["title"] == "Breaking now"
    assert sub.queue.empty(), "only genuinely new articles should broadcast"


async def test_cold_start_articles_are_scrollable_but_were_not_broadcast(wiring):
    config, store, broadcaster, _ = wiring
    sub = broadcaster.subscribe("art")
    poller = poller_for(store, config, broadcaster, [ROUND1])
    await poller.run_once(now=100)
    assert sub.queue.empty()
    rows, _ = store.page_news("art", limit=10)
    assert {r.title for r in rows} == {"Backfilled one", "Backfilled two"}


async def test_failing_feed_does_not_stop_the_healthy_one(wiring):
    config, store, broadcaster, _ = wiring
    poller = poller_for(store, config, broadcaster, [ROUND1])
    await poller.run_once(now=100)
    status = {f["name"]: f for f in store.all_feed_status()}
    assert status["Dead"]["consecutive_failures"] == 1
    assert status["Live"]["last_success_at"] == 100
    assert status["Dead"]["next_poll_at"] > status["Live"]["next_poll_at"]


async def test_cache_survives_a_restart(wiring):
    config, store, broadcaster, tmp_path = wiring
    poller = poller_for(store, config, broadcaster, [ROUND1])
    await poller.run_once(now=100)
    store.close()

    reopened = Store(str(tmp_path / "t.db"))
    try:
        rows, cursor = reopened.page_news("art", limit=10)
        assert {r.title for r in rows} == {"Backfilled one", "Backfilled two"}
        assert cursor is None

        # Scrollback must still walk the full cached depth after a restart, not
        # merely return a non-empty first page.
        seen: list[str] = []
        before = None
        while True:
            page, before = reopened.page_news("art", limit=1, before=before)
            seen += [r.title for r in page]
            if before is None:
                break
        assert sorted(seen) == ["Backfilled one", "Backfilled two"]
    finally:
        reopened.close()


async def test_runtime_added_feed_broadcasts_nothing_on_first_poll(wiring):
    config, store, broadcaster, _ = wiring
    poller = poller_for(store, config, broadcaster, [ROUND1])
    await poller.run_once(now=100)

    sub = broadcaster.subscribe("art")
    new_id = store.upsert_feed("https://added.example/rss", name="Added", now=200)
    store.subscribe("art", new_id)
    await poller.run_once(now=200)

    assert sub.queue.empty()
    rows, _ = store.page_news("art", limit=50)
    assert any(r.feed_id == new_id for r in rows)
