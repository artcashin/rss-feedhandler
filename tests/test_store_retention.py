import sqlite3
from pathlib import Path

import pytest
from rss_ticker.store import MIN_SQLITE, Store, NewArticle


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


DAY = 86400


def test_min_sqlite_covers_returning_clause():
    # insert_articles uses RETURNING, which requires SQLite 3.35+, not just
    # the 3.15+ row-value paging used elsewhere. MIN_SQLITE must not regress
    # below the stricter requirement.
    assert MIN_SQLITE >= (3, 35, 0)


def test_sweep_ignores_sort_at(store):
    # A January-dated item that only arrived today must survive: retention is
    # measured on when we last saw the item, never on its publication date.
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.insert_articles(
        fid,
        [NewArticle("old-date-new-arrival", "t", None, None, published_at=1)],
        now=100 * DAY,
    )
    deleted = store.sweep(now=100 * DAY, retention_days=7)
    assert deleted == 0
    rows, _ = store.page_news(limit=10)
    assert len(rows) == 1


def test_sweep_deletes_articles_past_retention(store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.insert_articles(fid, [NewArticle("a", "t", None, None, None)], now=1 * DAY)
    assert store.sweep(now=10 * DAY, retention_days=7) == 1
    assert store.page_news(limit=10)[0] == []


def test_item_still_in_the_feed_is_never_swept_or_rebroadcast(store):
    # A low-turnover feed (company IR, SEC filings) keeps the same items in its
    # XML for months. Sweeping on fetched_at deletes them once retention
    # elapses, the next poll re-inserts them, RETURNING calls them new, and the
    # poller broadcasts January press releases as breaking news.
    fid = store.upsert_feed("https://ir.example/rss", now=0)
    entries = [
        NewArticle("pr-1", "Q4 results", None, None, published_at=1 * DAY),
        NewArticle("pr-2", "Board appointment", None, None, published_at=1 * DAY),
    ]

    assert len(store.insert_articles(fid, entries, now=1 * DAY)) == 2

    for day in range(2, 30):
        again = store.insert_articles(fid, entries, now=day * DAY)
        assert again == [], f"day {day}: an item still in the feed re-broadcast"
        store.sweep(now=day * DAY, retention_days=7)

    rows, _ = store.page_news(limit=10)
    assert len(rows) == 2, "items still present in the feed were swept"


def test_item_dropped_from_the_feed_is_swept_after_retention(store):
    fid = store.upsert_feed("https://news.example/rss", now=0)
    stays = NewArticle("keep", "still listed", None, None, None)
    goes = NewArticle("drop", "rolled off the feed", None, None, None)
    store.insert_articles(fid, [stays, goes], now=1 * DAY)

    # From day 2 the feed only carries `stays`; `goes` is never seen again.
    for day in range(2, 12):
        store.insert_articles(fid, [stays], now=day * DAY)

    assert store.sweep(now=11 * DAY, retention_days=7) == 1
    rows, _ = store.page_news(limit=10)
    assert [r.guid for r in rows] == ["keep"]


def test_opening_a_pre_last_seen_database_migrates_and_backfills(tmp_path: Path):
    # A database written before last_seen_at existed must still open, and its
    # rows must not become instantly sweepable.
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT,
                            created_at INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE feeds (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE,
                            name TEXT, poll_interval_s INTEGER,
                            enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE subscriptions (user_id TEXT NOT NULL, feed_id INTEGER NOT NULL,
                                    PRIMARY KEY (user_id, feed_id));
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY, feed_id INTEGER NOT NULL, guid TEXT NOT NULL,
            title TEXT NOT NULL, link TEXT, summary TEXT, published_at INTEGER,
            fetched_at INTEGER NOT NULL, sort_at INTEGER NOT NULL,
            UNIQUE (feed_id, guid));
        CREATE INDEX idx_articles_fetched ON articles (fetched_at);
        INSERT INTO users (id) VALUES ('art');
        INSERT INTO feeds (id, url) VALUES (1, 'https://x.example/rss');
        INSERT INTO subscriptions VALUES ('art', 1);
        INSERT INTO articles
            (feed_id, guid, title, fetched_at, sort_at) VALUES (1, 'g', 't', 864000, 1);
        """
    )
    old.commit()
    old.close()

    store = Store(path)
    try:
        rows, _ = store.page_news(limit=10)
        assert [r.last_seen_at for r in rows] == [864000], "backfill did not run"
        assert store.sweep(now=864000, retention_days=7) == 0
    finally:
        store.close()


def test_due_feeds_respects_next_poll_at_and_enabled(store):
    a = store.upsert_feed("https://a.example/rss", now=100)
    b = store.upsert_feed("https://b.example/rss", now=500)
    store.upsert_feed("https://c.example/rss", now=100)
    store.db.execute("UPDATE feeds SET enabled = 0 WHERE url = 'https://c.example/rss'")
    store.db.commit()
    due = [f.id for f in store.due_feeds(now=200)]
    assert a in due
    assert b not in due
    assert len(due) == 1


def test_record_success_resets_failures_and_stores_validators(store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.record_failure(fid, error="boom", now=10, next_poll_at=20)
    store.record_success(fid, etag='"abc"', last_modified="Mon", now=30, next_poll_at=40)
    st = store.get_feed_state(fid)
    assert st.consecutive_failures == 0
    assert st.etag == '"abc"'
    assert st.last_modified == "Mon"
    assert st.last_success_at == 30
    assert st.next_poll_at == 40
    assert st.last_error is None


def test_record_failure_increments_and_keeps_last_success(store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.record_success(fid, etag=None, last_modified=None, now=5, next_poll_at=10)
    store.record_failure(fid, error="timeout", now=20, next_poll_at=30)
    store.record_failure(fid, error="timeout", now=40, next_poll_at=50)
    st = store.get_feed_state(fid)
    assert st.consecutive_failures == 2
    assert st.last_success_at == 5
    assert st.last_error == "timeout"


def test_all_feed_status_reports_url_and_error(store):
    fid = store.upsert_feed("https://x.example/rss", name="X", now=0)
    store.record_failure(fid, error="http 500", now=10, next_poll_at=20)
    status = store.all_feed_status()
    assert status[0]["url"] == "https://x.example/rss"
    assert status[0]["last_error"] == "http 500"
    assert status[0]["consecutive_failures"] == 1
