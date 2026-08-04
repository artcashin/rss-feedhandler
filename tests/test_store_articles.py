import pytest
from rss_ticker import store as store_module
from rss_ticker.store import Store, NewArticle


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def feed(store):
    return store.upsert_feed("https://x.example/rss", now=0)


def na(guid, title="t", published_at=None):
    return NewArticle(guid=guid, title=title, link=None, summary=None,
                      published_at=published_at)


def test_insert_returns_new_rows(store, feed):
    out = store.insert_articles(feed, [na("a"), na("b")], now=1000)
    assert {a.guid for a in out} == {"a", "b"}


def test_reinsert_returns_nothing(store, feed):
    store.insert_articles(feed, [na("a")], now=1000)
    out = store.insert_articles(feed, [na("a")], now=2000)
    assert out == []


def test_partial_overlap_returns_only_new(store, feed):
    store.insert_articles(feed, [na("a")], now=1000)
    out = store.insert_articles(feed, [na("a"), na("b")], now=2000)
    assert [a.guid for a in out] == ["b"]


def test_same_guid_different_feeds_both_stored(store):
    f1 = store.upsert_feed("https://one.example/rss", now=0)
    f2 = store.upsert_feed("https://two.example/rss", now=0)
    assert len(store.insert_articles(f1, [na("shared")], now=1)) == 1
    assert len(store.insert_articles(f2, [na("shared")], now=1)) == 1


def test_sort_at_uses_published_when_present(store, feed):
    out = store.insert_articles(feed, [na("a", published_at=500)], now=1000)
    assert out[0].sort_at == 500
    assert out[0].fetched_at == 1000


def test_sort_at_falls_back_to_fetched_when_published_missing(store, feed):
    out = store.insert_articles(feed, [na("a", published_at=None)], now=1000)
    assert out[0].sort_at == 1000
    assert out[0].published_at is None


def test_duplicate_guids_within_one_batch_insert_once(store, feed):
    out = store.insert_articles(feed, [na("a"), na("a")], now=1000)
    assert len(out) == 1


def test_empty_batch_is_a_noop(store, feed):
    assert store.insert_articles(feed, [], now=1000) == []


def test_sort_at_clamps_future_published_to_now(store, feed):
    # A feed with a bogus future published date must not get a sort_at in
    # the future: that would pin it to the top of the ticker forever and
    # poison the widget's after-cursor gap-fill (every reconnect queries
    # "after the future" and finds nothing). published_at itself (the
    # displayed/stated date) is preserved untouched.
    future = 1000 + 10_000_000
    out = store.insert_articles(feed, [na("a", published_at=future)], now=1000)
    assert out[0].sort_at == 1000
    assert out[0].published_at == future


def test_sort_at_keeps_past_published_unclamped(store, feed):
    out = store.insert_articles(feed, [na("a", published_at=500)], now=1000)
    assert out[0].sort_at == 500
    assert out[0].published_at == 500


def test_last_seen_at_update_is_chunked_across_multiple_batches(store, feed, monkeypatch):
    # Force the guid list to span several chunks of the last_seen_at UPDATE
    # (real feeds would need ~32764 entries to hit SQLite's actual variable
    # limit; monkeypatching the chunk size down lets the test exercise the
    # chunk *loop* quickly, at the cost of not exercising the real limit
    # itself).
    monkeypatch.setattr(store_module, "LAST_SEEN_UPDATE_CHUNK", 5)
    guids = [f"g{i}" for i in range(12)]
    entries = [na(g) for g in guids]

    out = store.insert_articles(feed, entries, now=1000)
    assert {a.guid for a in out} == set(guids)

    # Re-insert the same guids: nothing is new, but every row's last_seen_at
    # must still advance to the later `now` -- across every chunk, not just
    # the first.
    again = store.insert_articles(feed, entries, now=2000)
    assert again == []

    rows = store.db.execute(
        "SELECT guid, last_seen_at FROM articles WHERE feed_id = ?", (feed,)
    ).fetchall()
    stale = [r["guid"] for r in rows if r["last_seen_at"] != 2000]
    assert stale == [], f"guids whose last_seen_at was not updated: {stale}"


def test_future_dated_article_does_not_sort_ahead_in_paging(store):
    store.upsert_user("art", None)
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    future = 1000 + 10_000_000
    store.insert_articles(
        fid,
        [
            na("future", title="future", published_at=future),
            na("normal", title="normal", published_at=None),
        ],
        now=1000,
    )
    rows, _ = store.page_news("art", limit=10)
    # Both were inserted at the same now=1000; with sort_at clamped, the
    # future-dated article cannot jump ahead of the normal one, and it
    # must not be stuck permanently unreachable by an after-cursor either.
    assert {r.title for r in rows} == {"future", "normal"}
    assert rows[0].sort_at == rows[1].sort_at == 1000
