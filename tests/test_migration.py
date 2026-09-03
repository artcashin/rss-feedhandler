"""What an existing v8 deployment experiences on upgrade: the user tables go,
the feed knobs go, URLs are canonicalised, articles keep their history."""

import sqlite3
from pathlib import Path

from rss_ticker.store import Store


def v8_database(path: str) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT, created_at INTEGER NOT NULL DEFAULT 0, token TEXT);
        CREATE TABLE feeds (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, name TEXT,
            poll_interval_s INTEGER, enabled INTEGER NOT NULL DEFAULT 1, favicon TEXT,
            "group" TEXT, title_format TEXT);
        CREATE TABLE subscriptions (user_id TEXT NOT NULL, feed_id INTEGER NOT NULL, PRIMARY KEY (user_id, feed_id));
        CREATE TABLE articles (id INTEGER PRIMARY KEY, feed_id INTEGER NOT NULL, guid TEXT NOT NULL,
            title TEXT NOT NULL, link TEXT, summary TEXT, published_at INTEGER, fetched_at INTEGER NOT NULL,
            sort_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL DEFAULT 0, UNIQUE (feed_id, guid));
        CREATE TABLE feed_state (feed_id INTEGER PRIMARY KEY, etag TEXT, last_modified TEXT,
            last_polled_at INTEGER, last_success_at INTEGER, consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, next_poll_at INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE filter_rules (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, pattern TEXT NOT NULL,
            action TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
        INSERT INTO users VALUES ('art', 'Art', 0, 'tkn');
        INSERT INTO feeds (id, url, name, poll_interval_s, "group", title_format)
            VALUES (1, 'HTTPS://A.example/feed/', 'A', 90, 'Markets', '{title} - {author}'),
                   (2, 'https://a.example/feed', 'A dup', NULL, NULL, NULL),
                   (3, 'https://b.example/feed', 'B', NULL, NULL, NULL);
        INSERT INTO subscriptions VALUES ('art', 1);
        INSERT INTO articles (feed_id, guid, title, fetched_at, sort_at, last_seen_at)
            VALUES (3, 'g', 'Kept', 100, 100, 100);
        INSERT INTO feed_state (feed_id, next_poll_at) VALUES (1, 0), (2, 0), (3, 0);
        INSERT INTO filter_rules (user_id, pattern, action) VALUES ('art', 'nvidia', 'highlight');
        """
    )
    db.commit()
    db.close()


def tables(path: str) -> set[str]:
    return {
        r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def user_version(path: str) -> int:
    return sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]


def columns(path: str, table: str) -> set[str]:
    return {r[1] for r in sqlite3.connect(path).execute(f"PRAGMA table_info({table})")}


def test_v8_database_migrates_in_place(tmp_path: Path):
    db = str(tmp_path / "t.db")
    v8_database(db)
    store = Store(db)
    try:
        assert not ({"users", "subscriptions", "filter_rules"} & tables(db))
        assert columns(db, "feeds") == {"id", "url", "name", "enabled", "favicon"}
        assert "author" in columns(db, "articles")
        # Feed 3 canonicalises to itself; feed 1's canonical form collides with
        # feed 2, so feed 1 is left as-is and feed 2 remains the reachable one.
        assert store.feed_by_url("https://a.example/feed").id == 2
        assert store.get_feed(1).url == "HTTPS://A.example/feed/"
        assert [a.title for a in store.page_news(limit=10)[0]] == ["Kept"]
    finally:
        store.close()


def test_migration_is_idempotent(tmp_path: Path):
    db = str(tmp_path / "t.db")
    v8_database(db)
    Store(db).close()
    Store(db).close()
    assert columns(db, "feeds") == {"id", "url", "name", "enabled", "favicon"}


def test_canonicalisation_runs_once_and_never_re_strips(tmp_path: Path):
    """One-slash stripping is not a fixed point: `/f//` -> `/f/` -> `/f`.

    Re-running it on every open would walk the URL down a slash at a time,
    and each step splits the feed row -- a new id, no articles. The
    `user_version` gate is what stops it.
    """
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE feeds (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, name TEXT,
            enabled INTEGER NOT NULL DEFAULT 1, favicon TEXT);
        CREATE TABLE articles (id INTEGER PRIMARY KEY, feed_id INTEGER NOT NULL, guid TEXT NOT NULL,
            title TEXT NOT NULL, link TEXT, summary TEXT, published_at INTEGER, fetched_at INTEGER NOT NULL,
            sort_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL DEFAULT 0, UNIQUE (feed_id, guid));
        CREATE TABLE feed_state (feed_id INTEGER PRIMARY KEY, etag TEXT, last_modified TEXT,
            last_polled_at INTEGER, last_success_at INTEGER, consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, next_poll_at INTEGER NOT NULL DEFAULT 0);
        INSERT INTO feeds (id, url, name) VALUES (1, 'https://a.example/f//', 'A');
        INSERT INTO articles (feed_id, guid, title, fetched_at, sort_at, last_seen_at)
            VALUES (1, 'g', 'Kept', 100, 100, 100);
        INSERT INTO feed_state (feed_id, next_poll_at) VALUES (1, 0);
        """
    )
    conn.commit()
    conn.close()

    for open_number in range(3):
        store = Store(db)
        try:
            assert [(f.id, f.url) for f in store.all_feeds()] == [(1, "https://a.example/f/")]
            assert [a.title for a in store.page_news(limit=10)[0]] == ["Kept"]
            assert user_version(db) == 1, f"open {open_number + 1}"
        finally:
            store.close()
