import asyncio

import pytest

from rss_ticker.broadcast import MAX_QUEUE, Broadcaster
from rss_ticker.store import NewArticle, Store


@pytest.fixture
def store():
    s = Store(":memory:")
    s.upsert_user("art", None)
    s.upsert_user("bob", None)
    yield s
    s.close()


def add(store, user_ids, url="https://x.example/rss", name="X"):
    fid = store.upsert_feed(url, name=name, now=0)
    for u in user_ids:
        store.subscribe(u, fid)
    return fid


def articles(store, fid, specs):
    return store.insert_articles(
        fid,
        [NewArticle(g, t, None, None, ts) for g, t, ts in specs],
        now=1000,
    )


async def drain(sub):
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


async def test_subscriber_receives_articles_for_their_feeds(store):
    fid = add(store, ["art"])
    b = Broadcaster(store)
    sub = b.subscribe("art")
    await b.publish(articles(store, fid, [("a", "Fed holds", 1)]))
    assert [m["title"] for m in await drain(sub)] == ["Fed holds"]


async def test_subscriber_does_not_receive_other_users_feeds(store):
    add(store, ["bob"], url="https://bob.example/rss")
    fid_bob = store.upsert_feed("https://bob.example/rss", now=0)
    b = Broadcaster(store)
    sub = b.subscribe("art")
    await b.publish(articles(store, fid_bob, [("a", "Bob only", 1)]))
    assert await drain(sub) == []


async def test_include_filter_suppresses_non_matching(store):
    fid = add(store, ["art"])
    store.add_filter("art", "fed", "include")
    b = Broadcaster(store)
    sub = b.subscribe("art")
    await b.publish(articles(store, fid, [("a", "Fed holds", 1), ("b", "Oil slips", 2)]))
    assert [m["title"] for m in await drain(sub)] == ["Fed holds"]


async def test_highlight_flag_is_set(store):
    fid = add(store, ["art"])
    store.add_filter("art", "nvidia", "highlight")
    b = Broadcaster(store)
    sub = b.subscribe("art")
    await b.publish(articles(store, fid, [("a", "Nvidia beats", 1), ("b", "Oil", 2)]))
    msgs = {m["title"]: m for m in await drain(sub)}
    assert msgs["Nvidia beats"]["highlighted"] is True
    assert msgs["Oil"]["highlighted"] is False


async def test_payload_carries_feed_name_and_cursor(store):
    fid = add(store, ["art"], name="Reuters")
    b = Broadcaster(store)
    sub = b.subscribe("art")
    await b.publish(articles(store, fid, [("a", "Fed holds", 1)]))
    msg = (await drain(sub))[0]
    assert msg["source"] == "Reuters"
    assert msg["cursor"]
    assert msg["id"]
    # feed_id is the stable key the widget maps to the feed's favicon.
    assert msg["feed_id"] == fid
    assert msg["author"] is None


async def test_payload_carries_author(store):
    fid = add(store, ["art"])
    b = Broadcaster(store)
    sub = b.subscribe("art")
    await b.publish(
        store.insert_articles(fid, [NewArticle("a", "T", None, None, 1, author="Jane")], now=1000)
    )
    assert (await drain(sub))[0]["author"] == "Jane"


async def test_two_subscribers_of_same_user_both_receive(store):
    fid = add(store, ["art"])
    b = Broadcaster(store)
    s1, s2 = b.subscribe("art"), b.subscribe("art")
    await b.publish(articles(store, fid, [("a", "Fed holds", 1)]))
    assert len(await drain(s1)) == 1
    assert len(await drain(s2)) == 1


async def test_slow_client_is_dropped_not_awaited(store):
    fid = add(store, ["art"])
    b = Broadcaster(store)
    sub = b.subscribe("art")
    for i in range(MAX_QUEUE):
        sub.queue.put_nowait({"filler": i})
    await asyncio.wait_for(
        b.publish(articles(store, fid, [("a", "Fed holds", 1)])), timeout=1.0
    )
    assert sub.dropped is True
    assert b.subscriber_count("art") == 0


async def test_dropped_subscriber_signals_closed(store):
    fid = add(store, ["art"])
    b = Broadcaster(store)
    sub = b.subscribe("art")
    for i in range(MAX_QUEUE):
        sub.queue.put_nowait({"filler": i})
    await asyncio.wait_for(
        b.publish(articles(store, fid, [("a", "Fed holds", 1)])), timeout=1.0
    )
    assert sub.closed.is_set() is True
    assert sub.dropped is True
    assert b.subscriber_count("art") == 0


async def test_unsubscribe_stops_delivery(store):
    fid = add(store, ["art"])
    b = Broadcaster(store)
    sub = b.subscribe("art")
    b.unsubscribe(sub)
    await b.publish(articles(store, fid, [("a", "Fed holds", 1)]))
    assert await drain(sub) == []
