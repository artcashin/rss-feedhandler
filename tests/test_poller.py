import threading

import httpx
import pytest

from rss_ticker import poller as poller_module
from rss_ticker.config import Config
from rss_ticker.fetch import next_interval
from rss_ticker.normalize import parse_feed
from rss_ticker.poller import Poller
from rss_ticker.store import Store

FEED_A = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>First</title><guid>urn:1</guid></item></channel></rss>"""

FEED_AB = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Second</title><guid>urn:2</guid></item>
<item><title>First</title><guid>urn:1</guid></item></channel></rss>"""

FEED_EMPTY = b"""<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>"""


@pytest.fixture
def store():
    s = Store(":memory:")
    s.upsert_user("art", None)
    yield s
    s.close()


def make_poller(store, handler, broadcast_sink, max_concurrent=4):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = Config(
        public_base_url="http://x",
        admin_key="k",
        default_poll_interval_s=300,
        max_concurrent_polls=max_concurrent,
    )
    return Poller(store, client, cfg, on_new_articles=broadcast_sink, jitter=lambda: 1.0)


async def test_first_poll_stores_but_broadcasts_nothing(store):
    sent = []
    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    p = make_poller(store, lambda r: httpx.Response(200, content=FEED_A), sent.extend)
    await p.run_once(now=100)
    assert sent == []
    rows, _ = store.page_news("art", limit=10)
    assert [r.title for r in rows] == ["First"]


async def test_second_poll_broadcasts_only_new_articles(store):
    sent = []
    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    bodies = [FEED_A, FEED_AB]

    def handler(request):
        return httpx.Response(200, content=bodies.pop(0))

    p = make_poller(store, handler, sent.extend)
    await p.run_once(now=100)
    store.db.execute("UPDATE feed_state SET next_poll_at = 0")
    store.db.commit()
    await p.run_once(now=500)
    assert [a.title for a in sent] == ["Second"]


async def test_304_does_not_broadcast_or_fail(store):
    # NOTE: this test previously served the 304 on a feed's first-ever poll,
    # which collided with the cold-start-on-first-poll-304 bug fixed by
    # Finding 3 (see test_304_on_first_poll_is_treated_as_failure below). The
    # setup here now establishes a normal (non-cold-start) success first, so
    # this exercises the ordinary "already-synced feed gets a 304" path,
    # which Finding 3 explicitly leaves unchanged. The assertions themselves
    # are untouched from the original.
    sent = []
    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    responses = [httpx.Response(200, content=FEED_A), httpx.Response(304)]
    p = make_poller(store, lambda r: responses.pop(0), sent.extend)
    await p.run_once(now=0)
    store.db.execute("UPDATE feed_state SET next_poll_at = 0")
    store.db.commit()
    sent.clear()
    await p.run_once(now=100)
    assert sent == []
    assert store.get_feed_state(fid).consecutive_failures == 0
    assert store.get_feed_state(fid).last_success_at == 100


async def test_304_on_first_poll_is_treated_as_failure(store):
    sent = []
    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    p = make_poller(store, lambda r: httpx.Response(304), sent.extend)
    await p.run_once(now=100)
    st = store.get_feed_state(fid)
    assert st.last_success_at is None
    assert st.consecutive_failures == 1
    assert sent == []

    # A subsequent poll that actually returns content must still be treated
    # as cold start (suppressed broadcast, articles stored).
    store.db.execute("UPDATE feed_state SET next_poll_at = 0")
    store.db.commit()
    p2 = make_poller(store, lambda r: httpx.Response(200, content=FEED_A), sent.extend)
    await p2.run_once(now=500)
    assert sent == []
    rows, _ = store.page_news("art", limit=10)
    assert [r.title for r in rows] == ["First"]


async def test_broadcaster_error_does_not_escape_run_once(store):
    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    bodies = [FEED_A, FEED_AB]

    def handler(request):
        return httpx.Response(200, content=bodies.pop(0))

    def boom(articles):
        raise RuntimeError("broadcast failed")

    p = make_poller(store, handler, boom)
    await p.run_once(now=100)
    store.db.execute("UPDATE feed_state SET next_poll_at = 0")
    store.db.commit()
    polled = await p.run_once(now=500)
    assert polled == 1


async def test_no_usable_entries_applies_jitter(store):
    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=FEED_EMPTY))
    )
    cfg = Config(
        public_base_url="http://x",
        admin_key="k",
        default_poll_interval_s=300,
        max_concurrent_polls=4,
    )
    p = Poller(store, client, cfg, on_new_articles=lambda a: None, jitter=lambda: 2.0)
    await p.run_once(now=100)
    st = store.get_feed_state(fid)
    expected = 100 + int(next_interval(300, 1, None) * 2.0)
    assert st.next_poll_at == expected


async def test_failure_records_error_and_backs_off(store):
    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    p = make_poller(store, lambda r: httpx.Response(500), lambda a: None)
    await p.run_once(now=100)
    st = store.get_feed_state(fid)
    assert st.consecutive_failures == 1
    assert "500" in st.last_error
    assert st.next_poll_at == 100 + 600


async def test_one_failing_feed_does_not_block_others(store):
    sent = []
    bad = store.upsert_feed("https://bad.example/rss", now=0)
    good = store.upsert_feed("https://good.example/rss", now=0)
    store.subscribe("art", bad)
    store.subscribe("art", good)

    def handler(request):
        if "bad" in str(request.url):
            raise httpx.ConnectError("refused")
        return httpx.Response(200, content=FEED_A)

    p = make_poller(store, handler, sent.extend)
    polled = await p.run_once(now=100)
    assert polled == 2
    assert store.get_feed_state(good).last_success_at == 100
    assert store.get_feed_state(bad).consecutive_failures == 1


async def test_only_due_feeds_are_polled(store):
    fid = store.upsert_feed("https://a.example/rss", now=1000)
    store.subscribe("art", fid)
    p = make_poller(store, lambda r: httpx.Response(200, content=FEED_A), lambda a: None)
    assert await p.run_once(now=100) == 0


async def test_disabled_feed_is_not_polled(store):
    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    store.unsubscribe("art", fid)
    p = make_poller(store, lambda r: httpx.Response(200, content=FEED_A), lambda a: None)
    assert await p.run_once(now=100) == 0


async def test_feed_specific_interval_overrides_default(store):
    fid = store.upsert_feed("https://a.example/rss", poll_interval_s=60, now=0)
    store.subscribe("art", fid)
    p = make_poller(store, lambda r: httpx.Response(200, content=FEED_A), lambda a: None)
    await p.run_once(now=100)
    assert store.get_feed_state(fid).next_poll_at == 160


SECRET = "SUPERSECRET"
SECRET_URL = f"https://user:{SECRET}@host.example/rss"


def _poller_messages(caplog) -> list[str]:
    # Scoped to poller.py's own logger. httpx's separate "HTTP Request: GET
    # <url>" log line at INFO is a different mechanism (main.py quiets that
    # logger) and would otherwise leak the same secret into this assertion
    # for a reason poller.py's own fix cannot control.
    return [r.getMessage() for r in caplog.records if r.name == "rss_ticker.poller"]


async def test_failure_log_never_contains_a_feed_url_secret(store, caplog):
    # Drives the real poll_feed failure path (poller.py's WARNING call site)
    # rather than asserting on a hand-built string.
    fid = store.upsert_feed(SECRET_URL, now=0)
    store.subscribe("art", fid)
    p = make_poller(store, lambda r: httpx.Response(500), lambda a: None)
    with caplog.at_level("WARNING", logger="rss_ticker.poller"):
        await p.run_once(now=100)
    messages = _poller_messages(caplog)
    assert messages
    assert not any(SECRET in m for m in messages)


async def test_parse_feed_runs_off_the_event_loop_thread(store, monkeypatch):
    # Pins the actual property that matters: parse_feed's CPU work must not
    # execute on the event-loop thread, or a large feed body freezes every
    # concurrent WebSocket send/accept for the duration of the parse. A test
    # that only checks entries/insert side effects would pass against a
    # synchronous call too, so this records the real thread identity the
    # patched parse_feed runs on and compares it to the loop's own thread.
    loop_thread_ident = threading.get_ident()
    parse_thread_idents: list[int] = []

    def recording_parse_feed(body: bytes, now: int, title_format=None):
        parse_thread_idents.append(threading.get_ident())
        return parse_feed(body, now, title_format)

    monkeypatch.setattr(poller_module, "parse_feed", recording_parse_feed)

    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    p = make_poller(store, lambda r: httpx.Response(200, content=FEED_A), lambda a: None)
    await p.run_once(now=100)

    assert parse_thread_idents, "parse_feed was never called"
    assert parse_thread_idents[0] != loop_thread_ident


FEED_OLD_DATED = (
    b"<?xml version=\"1.0\"?><rss version=\"2.0\"><channel>"
    b"<item><title>Resurrected</title><guid>urn:old</guid>"
    b"<pubDate>Mon, 01 Jan 2001 00:00:00 GMT</pubDate></item></channel></rss>"
)


def _feed_with_dated_item(title: str, guid: str, pub_date: str) -> bytes:
    return (
        b"<?xml version=\"1.0\"?><rss version=\"2.0\"><channel>"
        b"<item><title>" + title.encode() + b"</title><guid>" + guid.encode() + b"</guid>"
        b"<pubDate>" + pub_date.encode() + b"</pubDate></item></channel></rss>"
    )


async def test_broadcast_age_guard_withholds_a_resurrection(store):
    # A non-cold-start poll (the feed already has a prior last_success_at)
    # inserts a *new* row (never seen before -- e.g. it aged out, was swept,
    # and the feed is now serving it again) whose published_at is far older
    # than that last_success_at. This is the resurrection case: it must be
    # cached (still retrievable) but NOT broadcast as if it were breaking
    # news with a stale date.
    sent = []
    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    # First poll: cold start, but must actually succeed (contain an entry)
    # so record_success runs and last_success_at is set -- an empty feed
    # body hits the "no usable entries" failure path instead and would
    # leave last_success_at None, making the second poll cold-start too.
    # The item here (urn:1, no pubDate) is unrelated to the resurrection.
    p = make_poller(store, lambda r: httpx.Response(200, content=FEED_A), sent.extend)
    await p.run_once(now=1_700_000_000)
    sent.clear()
    store.db.execute("UPDATE feed_state SET next_poll_at = 0")
    store.db.commit()

    # Second poll: the old-dated item reappears (newly inserted, since it
    # was never present before) alongside the unrelated already-seen item.
    p2 = make_poller(
        store, lambda r: httpx.Response(200, content=FEED_OLD_DATED), sent.extend
    )
    await p2.run_once(now=1_700_000_100)

    assert sent == [], "a resurrected article with a stale date must not be broadcast"
    rows, _ = store.page_news("art", limit=10)
    assert "Resurrected" in {r.title for r in rows}, (
        "the article must still be cached/retrievable even though withheld from the live push"
    )


async def test_genuinely_new_dated_article_is_still_broadcast(store):
    # Same non-cold-start shape as the resurrection test, but the new
    # article's published_at is current (>= last_success_at): it must pass
    # the guard and be broadcast normally.
    sent = []
    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    # See the resurrection test above for why the bootstrap poll must
    # actually succeed (contain an entry) rather than serve an empty body.
    p = make_poller(store, lambda r: httpx.Response(200, content=FEED_A), sent.extend)
    await p.run_once(now=1_700_000_000)
    sent.clear()
    store.db.execute("UPDATE feed_state SET next_poll_at = 0")
    store.db.commit()

    current = _feed_with_dated_item(
        "Fresh", "urn:fresh", "Wed, 14 Nov 2023 22:13:20 GMT"  # == 1_700_000_000
    )
    p2 = make_poller(store, lambda r: httpx.Response(200, content=current), sent.extend)
    await p2.run_once(now=1_700_000_100)

    assert [a.title for a in sent] == ["Fresh"]


async def test_dateless_article_is_still_broadcast(store):
    # published_at is None (no pubDate/updated in the entry): the guard
    # cannot judge age, so a dateless article is always pushed live, even
    # on a non-cold-start poll. This is the documented limitation -- a
    # dateless resurrection would slip through this same path.
    sent = []
    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    bodies = [FEED_A, FEED_AB]

    def handler(request):
        return httpx.Response(200, content=bodies.pop(0))

    p = make_poller(store, handler, sent.extend)
    await p.run_once(now=100)
    store.db.execute("UPDATE feed_state SET next_poll_at = 0")
    store.db.commit()
    await p.run_once(now=500)
    assert [a.title for a in sent] == ["Second"]


async def test_poll_path_never_touches_favicon_resolution(store, monkeypatch):
    # Favicons are resolved once at startup (see resolve_missing_favicons in
    # favicon.py), never on the poll path. poller.py no longer imports
    # resolve_favicon at all, so this patches the name into poller_module's
    # namespace (raising=False, since the attribute doesn't exist there any
    # more) -- harmless today, but if the poller strip were ever reverted
    # (re-adding `from .favicon import resolve_favicon` and a call site),
    # Python resolves that global name from poller_module's __dict__ at call
    # time, so this same patch would intercept it and the assertion below
    # would fail.
    calls = []

    async def spy_resolve_favicon(client, feed_url):
        calls.append(feed_url)
        return "data:image/png;base64,should-not-be-called"

    monkeypatch.setattr(poller_module, "resolve_favicon", spy_resolve_favicon, raising=False)

    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    p = make_poller(store, lambda r: httpx.Response(200, content=FEED_A), lambda a: None)
    await p.run_once(now=100)

    assert calls == []
    assert store.get_feed(fid).favicon is None


async def test_cold_start_log_never_contains_a_feed_url_secret(store, caplog):
    # Drives the real poll_feed cold-start path (poller.py's INFO call site).
    fid = store.upsert_feed(SECRET_URL, now=0)
    store.subscribe("art", fid)
    p = make_poller(store, lambda r: httpx.Response(200, content=FEED_A), lambda a: None)
    with caplog.at_level("INFO", logger="rss_ticker.poller"):
        await p.run_once(now=100)
    messages = _poller_messages(caplog)
    assert messages
    assert not any(SECRET in m for m in messages)


FEED_BYLINE = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<item><title>Scoop</title><guid>urn:s1</guid><dc:creator>Bob Pisani</dc:creator></item>
</channel></rss>"""


async def test_poll_applies_the_feed_title_format(store):
    fid = store.upsert_feed(
        "https://s.example/feed", now=0, title_format="{title} - {author}"
    )
    store.subscribe("art", fid)
    p = make_poller(store, lambda r: httpx.Response(200, content=FEED_BYLINE), lambda a: None)
    await p.run_once(now=100)
    rows, _ = store.page_news("art", limit=10)
    assert [r.title for r in rows] == ["Scoop - Bob Pisani"]


async def test_article_published_just_before_the_last_poll_is_still_broadcast(store):
    # Publish latency: publishers stamp pubDate at authoring time, and the
    # item can surface in the feed body one or more poll intervals later
    # (CDN-cached feeds guarantee this). Its pubDate then lands BEFORE
    # last_success_at even though it is genuinely new. The guard must not
    # eat it -- only resurrections (retention_days old, see the test above)
    # are old enough to withhold.
    sent = []
    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    p = make_poller(store, lambda r: httpx.Response(200, content=FEED_A), sent.extend)
    await p.run_once(now=1_700_000_000)
    sent.clear()
    store.db.execute("UPDATE feed_state SET next_poll_at = 0")
    store.db.commit()

    # pubDate 22:03:20 == 1_699_999_400: ten minutes before last_success_at.
    late_surfacing = _feed_with_dated_item(
        "Late surfacing", "urn:late", "Tue, 14 Nov 2023 22:03:20 GMT"
    )
    p2 = make_poller(
        store, lambda r: httpx.Response(200, content=late_surfacing), sent.extend
    )
    await p2.run_once(now=1_700_000_100)

    assert [a.title for a in sent] == ["Late surfacing"]


async def test_slow_feed_does_not_delay_another_feeds_broadcast(store):
    # One stalled fetch must not hold up the live push of articles already
    # fetched from healthy feeds in the same tick. Feed B's handler blocks
    # on an event; feed A's article must reach the broadcaster while B is
    # still hanging.
    import asyncio as _asyncio

    sent = []
    fa = store.upsert_feed("https://a.example/rss", now=0)
    fb = store.upsert_feed("https://b.example/rss", now=0)
    store.subscribe("art", fa)
    store.subscribe("art", fb)

    release_b = _asyncio.Event()

    async def handler(request):
        if request.url.host == "b.example":
            await release_b.wait()
            return httpx.Response(200, content=FEED_EMPTY)
        return httpx.Response(200, content=FEED_A)

    # Bootstrap A past cold start (B stays cold; its first poll is the slow one).
    p = make_poller(store, handler, sent.extend)
    release_b.set()
    await p.run_once(now=100)
    sent.clear()
    release_b.clear()
    store.db.execute("UPDATE feed_state SET next_poll_at = 0")
    store.db.commit()

    bodies_a = [FEED_AB]

    async def handler2(request):
        if request.url.host == "b.example":
            await release_b.wait()
            return httpx.Response(200, content=FEED_EMPTY)
        return httpx.Response(200, content=bodies_a[0])

    p2 = make_poller(store, handler2, sent.extend)
    task = _asyncio.create_task(p2.run_once(now=500))
    # A's genuinely-new article must be published while B is still blocked.
    for _ in range(200):
        if sent:
            break
        await _asyncio.sleep(0.01)
    assert [a.title for a in sent] == ["Second"], (
        "feed A's article was not broadcast until the slow feed B finished"
    )
    release_b.set()
    await task


async def test_retry_after_is_not_scaled_by_jitter(store):
    # A server's Retry-After is a request, not a suggestion: jittering it
    # down retries EARLIER than the server asked. The backoff path keeps
    # its jitter; the Retry-After path must not.
    sent = []
    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    p = Poller(
        store,
        httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(429, headers={"Retry-After": "100"})
        )),
        Config(public_base_url="http://x", admin_key="k"),
        on_new_articles=sent.extend,
        jitter=lambda: 0.5,
    )
    await p.run_once(now=1000)
    assert store.get_feed_state(fid).next_poll_at == 1100
