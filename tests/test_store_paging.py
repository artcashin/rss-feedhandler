import pytest
from rss_ticker.store import CursorError, NewArticle, Store, decode_cursor


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def seed(store, n, feed_url="https://x.example/rss", start=1000, step=10):
    fid = store.upsert_feed(feed_url, now=0)
    entries = [
        NewArticle(guid=f"g{i}", title=f"headline {i}", link=None, summary=None,
                   published_at=start + i * step)
        for i in range(n)
    ]
    store.insert_articles(fid, entries, now=start)
    return fid


def test_first_page_is_newest_first(store):
    seed(store, 5)
    rows, _ = store.page_news(limit=3)
    assert [r.title for r in rows] == ["headline 4", "headline 3", "headline 2"]


def test_paging_walks_backwards_without_gaps_or_repeats(store):
    seed(store, 7)
    seen = []
    cursor = None
    while True:
        rows, cursor = store.page_news(limit=3, before=cursor)
        seen += [r.guid for r in rows]
        if cursor is None:
            break
    assert seen == [f"g{i}" for i in reversed(range(7))]


def test_cursor_is_stable_when_new_articles_arrive_mid_scroll(store):
    fid = seed(store, 5)
    page1, cursor = store.page_news(limit=2)
    store.insert_articles(
        fid,
        [NewArticle(guid="brand-new", title="new", link=None, summary=None,
                    published_at=99999)],
        now=99999,
    )
    page2, _ = store.page_news(limit=2, before=cursor)
    assert not ({r.guid for r in page1} & {r.guid for r in page2})
    assert "brand-new" not in {r.guid for r in page2}


def test_the_whole_pool_is_returned_not_one_feeds_slice(store):
    seed(store, 3, feed_url="https://mine.example/rss")
    other = store.upsert_feed("https://theirs.example/rss", now=0)
    store.insert_articles(
        other,
        [NewArticle(guid="x", title="theirs too", link=None, summary=None, published_at=99999)],
        now=99999,
    )
    rows, _ = store.page_news(limit=10)
    assert "theirs too" in [r.title for r in rows]


def test_after_cursor_returns_only_newer_items(store):
    from rss_ticker.store import encode_cursor

    seed(store, 5)
    rows, _ = store.page_news(limit=2)
    boundary = encode_cursor(rows[-1].sort_at, rows[-1].id)
    newer, _ = store.page_news(limit=10, after=boundary)
    assert [r.guid for r in newer] == ["g4"]


def test_next_cursor_is_none_on_last_page(store):
    seed(store, 2)
    _, cursor = store.page_news(limit=10)
    assert cursor is None


def test_bad_cursor_raises(store):
    with pytest.raises(CursorError):
        decode_cursor("not-base64!!")


def test_after_paging_walks_forward_without_gaps_or_repeats(store):
    from rss_ticker.store import encode_cursor

    seed(store, 10)
    all_rows, _ = store.page_news(limit=10)  # newest-first: g9..g0
    g2 = next(r for r in all_rows if r.guid == "g2")
    boundary = encode_cursor(g2.sort_at, g2.id)

    seen = []
    cursor = boundary
    while True:
        rows, cursor = store.page_news(limit=3, after=cursor)
        seen += [r.guid for r in rows]
        if cursor is None:
            break
    assert seen == [f"g{i}" for i in range(3, 10)]


def test_after_cursor_page_is_oldest_first(store):
    from rss_ticker.store import encode_cursor

    seed(store, 5)
    all_rows, _ = store.page_news(limit=10)  # newest-first: g4..g0
    g0 = all_rows[-1]
    boundary = encode_cursor(g0.sort_at, g0.id)

    newer, _ = store.page_news(limit=3, after=boundary)
    assert [r.guid for r in newer] == ["g1", "g2", "g3"]


def test_next_cursor_is_none_on_last_after_page(store):
    from rss_ticker.store import encode_cursor

    seed(store, 2)
    boundary = encode_cursor(0, 0)  # older than every seeded article
    _, cursor = store.page_news(limit=10, after=boundary)
    assert cursor is None
