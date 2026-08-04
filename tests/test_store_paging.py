import pytest
from rss_ticker.filters import FilterRule, evaluate
from rss_ticker.store import Store, NewArticle, decode_cursor, CursorError


@pytest.fixture
def store():
    s = Store(":memory:")
    s.upsert_user("art", None)
    yield s
    s.close()


def seed(store, n, feed_url="https://x.example/rss", start=1000, step=10):
    fid = store.upsert_feed(feed_url, now=0)
    store.subscribe("art", fid)
    entries = [
        NewArticle(guid=f"g{i}", title=f"headline {i}", link=None, summary=None,
                   published_at=start + i * step)
        for i in range(n)
    ]
    store.insert_articles(fid, entries, now=start)
    return fid


def test_first_page_is_newest_first(store):
    seed(store, 5)
    rows, _ = store.page_news("art", limit=3)
    assert [r.title for r in rows] == ["headline 4", "headline 3", "headline 2"]


def test_paging_walks_backwards_without_gaps_or_repeats(store):
    seed(store, 7)
    seen = []
    cursor = None
    while True:
        rows, cursor = store.page_news("art", limit=3, before=cursor)
        seen += [r.guid for r in rows]
        if cursor is None:
            break
    assert seen == [f"g{i}" for i in reversed(range(7))]


def test_cursor_is_stable_when_new_articles_arrive_mid_scroll(store):
    fid = seed(store, 5)
    page1, cursor = store.page_news("art", limit=2)
    store.insert_articles(
        fid,
        [NewArticle(guid="brand-new", title="new", link=None, summary=None,
                    published_at=99999)],
        now=99999,
    )
    page2, _ = store.page_news("art", limit=2, before=cursor)
    assert not ({r.guid for r in page1} & {r.guid for r in page2})
    assert "brand-new" not in {r.guid for r in page2}


def test_articles_from_unsubscribed_feeds_are_not_returned(store):
    seed(store, 3, feed_url="https://mine.example/rss")
    other = store.upsert_feed("https://theirs.example/rss", now=0)
    store.insert_articles(
        other,
        [NewArticle(guid="x", title="not mine", link=None, summary=None, published_at=99999)],
        now=0,
    )
    rows, _ = store.page_news("art", limit=10)
    assert "not mine" not in [r.title for r in rows]


def test_include_filter_restricts_results(store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    store.insert_articles(
        fid,
        [
            NewArticle("a", "Fed holds rates", None, None, 100),
            NewArticle("b", "Oil slips", None, None, 200),
        ],
        now=0,
    )
    store.add_filter("art", "fed", "include")
    rows, _ = store.page_news("art", limit=10)
    assert [r.title for r in rows] == ["Fed holds rates"]


def test_after_cursor_returns_only_newer_items(store):
    from rss_ticker.store import encode_cursor

    seed(store, 5)
    rows, _ = store.page_news("art", limit=2)
    boundary = encode_cursor(rows[-1].sort_at, rows[-1].id)
    newer, _ = store.page_news("art", limit=10, after=boundary)
    assert [r.guid for r in newer] == ["g4"]


def test_next_cursor_is_none_on_last_page(store):
    seed(store, 2)
    _, cursor = store.page_news("art", limit=10)
    assert cursor is None


def test_bad_cursor_raises(store):
    with pytest.raises(CursorError):
        decode_cursor("not-base64!!")


def test_filters_for_returns_rules(store):
    store.add_filter("art", "nvidia", "highlight")
    rules = store.filters_for("art")
    assert rules[0].pattern == "nvidia"
    assert rules[0].action == "highlight"


def test_multiple_include_rules_are_or_not_and(store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    store.insert_articles(
        fid,
        [
            NewArticle("a", "Fed holds rates", None, None, 100),
            NewArticle("b", "Oil slips", None, None, 200),
            NewArticle("c", "Wheat rallies", None, None, 300),
        ],
        now=0,
    )
    store.add_filter("art", "fed", "include")
    store.add_filter("art", "oil", "include")
    rows, _ = store.page_news("art", limit=10)
    assert {r.guid for r in rows} == {"a", "b"}


def test_after_paging_walks_forward_without_gaps_or_repeats(store):
    from rss_ticker.store import encode_cursor

    seed(store, 10)
    all_rows, _ = store.page_news("art", limit=10)  # newest-first: g9..g0
    g2 = next(r for r in all_rows if r.guid == "g2")
    boundary = encode_cursor(g2.sort_at, g2.id)

    seen = []
    cursor = boundary
    while True:
        rows, cursor = store.page_news("art", limit=3, after=cursor)
        seen += [r.guid for r in rows]
        if cursor is None:
            break
    assert seen == [f"g{i}" for i in range(3, 10)]


def test_after_cursor_page_is_oldest_first(store):
    from rss_ticker.store import encode_cursor

    seed(store, 5)
    all_rows, _ = store.page_news("art", limit=10)  # newest-first: g4..g0
    g0 = all_rows[-1]
    boundary = encode_cursor(g0.sort_at, g0.id)

    newer, _ = store.page_news("art", limit=3, after=boundary)
    assert [r.guid for r in newer] == ["g1", "g2", "g3"]


def test_next_cursor_is_none_on_last_after_page(store):
    from rss_ticker.store import encode_cursor

    seed(store, 2)
    boundary = encode_cursor(0, 0)  # older than every seeded article
    _, cursor = store.page_news("art", limit=10, after=boundary)
    assert cursor is None


def test_include_filter_matches_non_ascii_case_folding_like_live_push(store):
    # SQLite's builtin lower() only folds ASCII, so lower('CAFÉ') == 'cafÉ'
    # (the accented letter is untouched -- confirmed: sqlite3 in-memory
    # `select lower('CAFÉ')` returns 'cafÉ', not 'café'). filters.evaluate
    # (used for the live WebSocket push) uses Python's Unicode-aware
    # str.lower(), which does fold 'É' -> 'é'. The title below deliberately
    # carries an uppercase 'É' that must be case-folded for the pattern to
    # match -- a title that already has a lowercase 'é' wouldn't need any
    # folding and would pass this test whether or not the SQL path is
    # Unicode-aware, pinning nothing. If page_news relies on SQLite's
    # builtin lower(), the two paths disagree here: an article a user sees
    # live would silently vanish on reload.
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    store.insert_articles(
        fid,
        [NewArticle("a", "CAFÉ opens", None, None, 100)],
        now=0,
    )
    store.add_filter("art", "café", "include")

    rows, _ = store.page_news("art", limit=10)
    assert [r.title for r in rows] == ["CAFÉ opens"]

    included, _ = evaluate([FilterRule("café", "include")], "CAFÉ opens", None)
    assert included is True


def test_include_filter_escapes_underscore_metacharacter(store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    store.insert_articles(
        fid,
        [
            NewArticle("a", "50_off deal", None, None, 100),
            NewArticle("b", "500off deal", None, None, 200),
        ],
        now=0,
    )
    store.add_filter("art", "50_off", "include")
    rows, _ = store.page_news("art", limit=10)
    assert [r.title for r in rows] == ["50_off deal"]
