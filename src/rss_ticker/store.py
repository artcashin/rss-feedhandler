from __future__ import annotations

import base64
import functools
import sqlite3
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

MIN_SQLITE = (3, 35, 0)

# insert_articles binds 2 + len(guids) parameters per last_seen_at UPDATE.
# SQLite's SQLITE_MAX_VARIABLE_NUMBER is 32766 on modern builds but only 999
# on older ones; chunk the guid list so a single huge feed can never exceed
# either limit. Kept well under the conservative 999 floor, leaving room for
# the 2 non-guid params (feed_id, now).
LAST_SEEN_UPDATE_CHUNK = 900


class CursorError(Exception):
    pass


def encode_cursor(sort_at: int, article_id: int) -> str:
    return base64.urlsafe_b64encode(f"{sort_at}:{article_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[int, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        sort_at, article_id = raw.split(":")
        return int(sort_at), int(article_id)
    except Exception as exc:
        raise CursorError("cursor is not valid") from exc


def canonical_url(raw: str) -> str:
    """The pool's identity for a feed: scheme and host lowercased, one
    trailing slash stripped, nothing cleverer (design decision 2)."""
    parts = urlsplit(raw.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc
    host = parts.hostname or ""
    if host:
        # Rebuild netloc with a lowercased host, keeping userinfo and port.
        # Deliberately minimal: an IPv6 literal keeps its netloc as-is.
        userinfo, _, hostport = netloc.rpartition("@")
        port = ""
        if hostport.count(":") == 1 and not hostport.startswith("["):
            port = hostport.rpartition(":")[2]
        rebuilt = host + (f":{port}" if port else "")
        netloc = f"{userinfo}@{rebuilt}" if userinfo else rebuilt
    path = parts.path.rstrip("/") if parts.path not in ("", "/") else ""
    return urlunsplit((scheme, netloc, path, parts.query, parts.fragment))


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    name TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    favicon TEXT
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    feed_id INTEGER NOT NULL REFERENCES feeds(id),
    guid TEXT NOT NULL,
    title TEXT NOT NULL,
    link TEXT,
    summary TEXT,
    author TEXT,
    published_at INTEGER,
    fetched_at INTEGER NOT NULL,
    sort_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE (feed_id, guid)
);

CREATE TABLE IF NOT EXISTS feed_state (
    feed_id INTEGER PRIMARY KEY REFERENCES feeds(id),
    etag TEXT,
    last_modified TEXT,
    last_polled_at INTEGER,
    last_success_at INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_poll_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_articles_sort ON articles (sort_at DESC, id DESC);
"""

# Indexes that touch columns added by a migration must be created only after
# that migration has run, or opening a pre-migration database fails. The two
# DROP INDEX lines retire indexes from earlier schemas (the fetched_at
# retention index, and the user-era subscriptions index whose table is gone).
POST_MIGRATION_SCHEMA = """
DROP INDEX IF EXISTS idx_articles_fetched;
DROP INDEX IF EXISTS idx_subs_user;
CREATE INDEX IF NOT EXISTS idx_articles_last_seen ON articles (last_seen_at);
"""


# The feed columns the current schema declares; the migration drops anything
# else it finds on the table.
FEED_COLUMNS = frozenset({"id", "url", "name", "enabled", "favicon"})


@dataclass(frozen=True)
class Feed:
    id: int
    url: str
    name: str | None
    enabled: bool
    favicon: str | None = None


@dataclass(frozen=True)
class FeedState:
    feed_id: int
    etag: str | None
    last_modified: str | None
    last_polled_at: int | None
    last_success_at: int | None
    consecutive_failures: int
    last_error: str | None
    next_poll_at: int


@dataclass(frozen=True)
class Article:
    id: int
    feed_id: int
    guid: str
    title: str
    link: str | None
    summary: str | None
    author: str | None
    published_at: int | None
    fetched_at: int
    sort_at: int
    last_seen_at: int


@dataclass(frozen=True)
class NewArticle:
    guid: str
    title: str
    link: str | None
    summary: str | None
    published_at: int | None
    author: str | None = None


def _feed(row: sqlite3.Row) -> Feed:
    return Feed(
        id=row["id"],
        url=row["url"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        favicon=row["favicon"],
    )


def _synchronized(method):
    """Serialize a Store method body under the instance's RLock.

    The one shared sqlite3 connection (`check_same_thread=False`) makes a
    transaction connection-level, not statement-level: any thread's commit
    or implicit rollback can tear another thread's in-flight transaction.
    Wrapping every public method's *entire* body in the same lock makes
    each method atomic with respect to every other thread, writers and
    readers alike. RLock (not Lock) so that a public method calling another
    public method internally cannot self-deadlock.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _article(row: sqlite3.Row) -> Article:
    return Article(
        id=row["id"],
        feed_id=row["feed_id"],
        guid=row["guid"],
        title=row["title"],
        link=row["link"],
        summary=row["summary"],
        author=row["author"],
        published_at=row["published_at"],
        fetched_at=row["fetched_at"],
        sort_at=row["sort_at"],
        last_seen_at=row["last_seen_at"],
    )


class Store:
    def __init__(self, path: str) -> None:
        # Created first, before anything else in construction, so that no
        # other thread could ever observe self with a decorated method
        # runnable but no lock to acquire.
        self._lock = threading.RLock()
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise RuntimeError(
                f"sqlite {'.'.join(map(str, MIN_SQLITE))}+ required for row-value paging "
                f"and the RETURNING clause, found {sqlite3.sqlite_version}"
            )
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.executescript(POST_MIGRATION_SCHEMA)
        self.db.commit()

    def _migrate(self) -> None:
        """Bring a database created by an older version up to the current schema.

        The user era's tables go; the feed row loses the per-user knobs;
        articles gain a byline; every feed URL is canonicalised. A row whose
        canonical form collides with another row's is left as it is -- it is
        unreachable by lookup, so it sits at zero subscribers and the sweep
        drops it.
        """
        self.db.execute("DROP TABLE IF EXISTS filter_rules")
        self.db.execute("DROP TABLE IF EXISTS subscriptions")
        self.db.execute("DROP TABLE IF EXISTS users")

        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(articles)").fetchall()}
        if "last_seen_at" not in columns:
            self.db.execute(
                "ALTER TABLE articles ADD COLUMN last_seen_at INTEGER NOT NULL DEFAULT 0"
            )
        # Rows predating the column (and any left at the default) are treated as
        # last seen when they were fetched, which is what retention used before.
        self.db.execute("UPDATE articles SET last_seen_at = fetched_at WHERE last_seen_at = 0")
        if "author" not in columns:
            self.db.execute("ALTER TABLE articles ADD COLUMN author TEXT")

        feed_columns = {r["name"] for r in self.db.execute("PRAGMA table_info(feeds)").fetchall()}
        if "favicon" not in feed_columns:
            self.db.execute("ALTER TABLE feeds ADD COLUMN favicon TEXT")
        # Any feed column the current schema does not declare is a knob from
        # the config-feeds/user era (decision E). Derived from FEED_COLUMNS
        # rather than a hardcoded list, so the dead names live in exactly one
        # place -- the schema itself.
        for dead in sorted(feed_columns - FEED_COLUMNS):
            self.db.execute(f'ALTER TABLE feeds DROP COLUMN "{dead}"')

        for row in self.db.execute("SELECT id, url FROM feeds").fetchall():
            canon = canonical_url(row["url"])
            if canon == row["url"]:
                continue
            taken = self.db.execute("SELECT 1 FROM feeds WHERE url = ?", (canon,)).fetchone()
            if taken is None:
                self.db.execute("UPDATE feeds SET url = ? WHERE id = ?", (canon, row["id"]))
        self.db.commit()

    @_synchronized
    def close(self) -> None:
        self.db.close()

    @_synchronized
    def upsert_feed(self, url: str, name: str | None = None, now: int = 0) -> int:
        """Add `url` to the pool (or find it), returning its feed id.

        The COALESCE order is deliberate: the *stored* name wins, so a
        client's `name` only lands on a feed new to the pool (addendum,
        reply frame).
        """
        url = canonical_url(url)
        self.db.execute(
            "INSERT INTO feeds (url, name) VALUES (?, ?) "
            "ON CONFLICT(url) DO UPDATE SET name = COALESCE(feeds.name, excluded.name)",
            (url, name),
        )
        feed_id = self.db.execute("SELECT id FROM feeds WHERE url = ?", (url,)).fetchone()["id"]
        self.db.execute(
            "INSERT INTO feed_state (feed_id, next_poll_at) VALUES (?, ?) "
            "ON CONFLICT(feed_id) DO NOTHING",
            (feed_id, now),
        )
        self.db.commit()
        return feed_id

    @_synchronized
    def feed_by_url(self, url: str) -> Feed | None:
        row = self.db.execute(
            "SELECT * FROM feeds WHERE url = ?", (canonical_url(url),)
        ).fetchone()
        return _feed(row) if row else None

    @_synchronized
    def set_enabled(self, feed_id: int, enabled: bool) -> None:
        self.db.execute(
            "UPDATE feeds SET enabled = ? WHERE id = ?", (1 if enabled else 0, feed_id)
        )
        self.db.commit()

    @_synchronized
    def disable_all_feeds(self) -> None:
        """Boot state: nobody is connected, so nothing is polled (decision B)."""
        self.db.execute("UPDATE feeds SET enabled = 0")
        self.db.commit()

    @_synchronized
    def drop_disabled_feeds(self) -> int:
        """Remove every feed at zero subscribers, with its state and articles."""
        with self.db:
            ids = [r["id"] for r in self.db.execute("SELECT id FROM feeds WHERE enabled = 0")]
            for feed_id in ids:
                self.db.execute("DELETE FROM articles WHERE feed_id = ?", (feed_id,))
                self.db.execute("DELETE FROM feed_state WHERE feed_id = ?", (feed_id,))
                self.db.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
        return len(ids)

    @_synchronized
    def get_feed(self, feed_id: int) -> Feed | None:
        row = self.db.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        return _feed(row) if row else None

    @_synchronized
    def set_feed_favicon(self, feed_id: int, favicon: str | None) -> None:
        self.db.execute(
            "UPDATE feeds SET favicon = ? WHERE id = ?", (favicon, feed_id)
        )
        self.db.commit()

    @_synchronized
    def all_feeds(self) -> list[Feed]:
        rows = self.db.execute("SELECT * FROM feeds ORDER BY id").fetchall()
        return [_feed(r) for r in rows]

    @_synchronized
    def get_feed_state(self, feed_id: int) -> FeedState | None:
        row = self.db.execute(
            "SELECT * FROM feed_state WHERE feed_id = ?", (feed_id,)
        ).fetchone()
        if not row:
            return None
        return FeedState(
            feed_id=row["feed_id"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            last_polled_at=row["last_polled_at"],
            last_success_at=row["last_success_at"],
            consecutive_failures=row["consecutive_failures"],
            last_error=row["last_error"],
            next_poll_at=row["next_poll_at"],
        )

    @_synchronized
    def insert_articles(
        self, feed_id: int, entries: list[NewArticle], now: int
    ) -> list[Article]:
        if not entries:
            return []
        seen: set[str] = set()
        rows = []
        for e in entries:
            if e.guid in seen:
                continue
            seen.add(e.guid)
            rows.append(
                (
                    feed_id,
                    e.guid,
                    e.title,
                    e.link,
                    e.summary,
                    e.author,
                    e.published_at,
                    now,
                    # sort_at is the widget's ordering/cursor key (see
                    # encode_cursor / page_news), not the displayed date.
                    # Clamp it to now: an unclamped future-dated feed entry
                    # would pin itself above every real article and poison
                    # the widget's after-cursor gap-fill (a reconnect asks
                    # for articles "after" a cursor that is stuck in the
                    # future and gets none, hiding everything missed during
                    # the disconnect). published_at itself is left alone.
                    min(e.published_at, now) if e.published_at is not None else now,
                    now,
                )
            )
        inserted: list[Article] = []
        with self.db:
            for row in rows:
                # DO NOTHING, never DO UPDATE: RETURNING must yield only rows
                # that did not already exist, because that is what makes an
                # article "new" and therefore broadcastable.
                cur = self.db.execute(
                    "INSERT INTO articles "
                    "(feed_id, guid, title, link, summary, author, published_at, fetched_at, "
                    " sort_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (feed_id, guid) DO NOTHING "
                    "RETURNING *",
                    row,
                )
                got = cur.fetchone()
                if got is not None:
                    inserted.append(_article(got))

            # Separate statement so that items still present in the feed keep
            # their retention clock alive without being reported as new.
            # Only ?-placeholder count is interpolated here; values are parameterized.
            # Chunked (see LAST_SEEN_UPDATE_CHUNK) so a large batch cannot bind
            # more parameters than SQLite allows; every chunk runs inside the
            # same `with self.db:` transaction, so the batch stays atomic.
            guids = list(seen)
            for i in range(0, len(guids), LAST_SEEN_UPDATE_CHUNK):
                chunk = guids[i : i + LAST_SEEN_UPDATE_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                self.db.execute(
                    "UPDATE articles SET last_seen_at = ? "
                    f"WHERE feed_id = ? AND guid IN ({placeholders})",
                    [now, feed_id, *chunk],
                )
        return inserted

    @_synchronized
    def page_news(
        self, limit: int, before: str | None = None, after: str | None = None
    ) -> tuple[list[Article], str | None]:
        """Page the whole pool.

        `before` and the no-cursor default page NEWEST-FIRST, walking backward
        in time (each next_cursor moves further into the past).

        `after` is a forward walk-forward gap fill (e.g. for WebSocket
        reconnects retrieving articles missed while disconnected) and returns
        results OLDEST-FIRST instead: it orders ascending and the returned
        next_cursor is the newest row of the page, so that chaining `after`
        calls advances forward through time without skipping or repeating
        rows. This ordering asymmetry between `before`/default and `after` is
        intentional but easy to miss -- see the base design.
        """
        where = ["1 = 1"]
        params: list[object] = []
        if before:
            sort_at, article_id = decode_cursor(before)
            where.append("(sort_at, id) < (?, ?)")
            params += [sort_at, article_id]
        if after:
            sort_at, article_id = decode_cursor(after)
            where.append("(sort_at, id) > (?, ?)")
            params += [sort_at, article_id]
        order = "ASC" if after else "DESC"
        sql = (
            f"SELECT * FROM articles WHERE {' AND '.join(where)} "
            f"ORDER BY sort_at {order}, id {order} LIMIT ?"
        )
        params.append(limit + 1)
        rows = self.db.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        articles = [_article(r) for r in rows]
        next_cursor = (
            encode_cursor(articles[-1].sort_at, articles[-1].id) if has_more and articles else None
        )
        return articles, next_cursor

    @_synchronized
    def sweep(self, now: int, retention_days: int) -> int:
        cutoff = now - retention_days * 86400
        cur = self.db.execute("DELETE FROM articles WHERE last_seen_at < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount

    @_synchronized
    def due_feeds(self, now: int) -> list[Feed]:
        rows = self.db.execute(
            "SELECT f.* FROM feeds f "
            "JOIN feed_state st ON st.feed_id = f.id "
            "WHERE f.enabled = 1 AND st.next_poll_at <= ? ORDER BY st.next_poll_at",
            (now,),
        ).fetchall()
        return [_feed(r) for r in rows]

    @_synchronized
    def record_success(
        self,
        feed_id: int,
        *,
        etag: str | None,
        last_modified: str | None,
        now: int,
        next_poll_at: int,
    ) -> None:
        self.db.execute(
            "UPDATE feed_state SET etag = ?, last_modified = ?, last_polled_at = ?, "
            "last_success_at = ?, consecutive_failures = 0, last_error = NULL, "
            "next_poll_at = ? WHERE feed_id = ?",
            (etag, last_modified, now, now, next_poll_at, feed_id),
        )
        self.db.commit()

    @_synchronized
    def record_failure(
        self, feed_id: int, *, error: str, now: int, next_poll_at: int
    ) -> None:
        self.db.execute(
            "UPDATE feed_state SET last_polled_at = ?, "
            "consecutive_failures = consecutive_failures + 1, last_error = ?, "
            "next_poll_at = ? WHERE feed_id = ?",
            (now, error, next_poll_at, feed_id),
        )
        self.db.commit()

    @_synchronized
    def all_feed_status(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT f.id, f.url, f.name, f.enabled, st.last_polled_at, st.last_success_at, "
            "st.consecutive_failures, st.last_error, st.next_poll_at "
            "FROM feeds f JOIN feed_state st ON st.feed_id = f.id ORDER BY f.id"
        ).fetchall()
        return [dict(r) for r in rows]
