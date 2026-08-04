from __future__ import annotations

import base64
import functools
import sqlite3
import threading
from dataclasses import dataclass

from .filters import FilterRule, include_patterns

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


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT,
    created_at INTEGER NOT NULL DEFAULT 0,
    token TEXT
);

CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    name TEXT,
    poll_interval_s INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1,
    favicon TEXT,
    "group" TEXT,
    title_format TEXT
);

CREATE TABLE IF NOT EXISTS subscriptions (
    user_id TEXT NOT NULL REFERENCES users(id),
    feed_id INTEGER NOT NULL REFERENCES feeds(id),
    PRIMARY KEY (user_id, feed_id)
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    feed_id INTEGER NOT NULL REFERENCES feeds(id),
    guid TEXT NOT NULL,
    title TEXT NOT NULL,
    link TEXT,
    summary TEXT,
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

CREATE TABLE IF NOT EXISTS filter_rules (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    pattern TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('include','highlight')),
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE (user_id, pattern, action)
);

CREATE INDEX IF NOT EXISTS idx_articles_sort ON articles (sort_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions (user_id);
"""

# Indexes that touch columns added by a migration must be created only after
# that migration has run, or opening a pre-migration database fails.
POST_MIGRATION_SCHEMA = """
DROP INDEX IF EXISTS idx_articles_fetched;
CREATE INDEX IF NOT EXISTS idx_articles_last_seen ON articles (last_seen_at);
"""


@dataclass(frozen=True)
class Feed:
    id: int
    url: str
    name: str | None
    poll_interval_s: int | None
    enabled: bool
    favicon: str | None = None
    group: str | None = None
    title_format: str | None = None


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


def _feed(row: sqlite3.Row) -> Feed:
    return Feed(
        id=row["id"],
        url=row["url"],
        name=row["name"],
        poll_interval_s=row["poll_interval_s"],
        enabled=bool(row["enabled"]),
        favicon=row["favicon"],
        group=row["group"],
        title_format=row["title_format"],
    )


def _synchronized(method):
    """Serialize a Store method body under the instance's RLock.

    The one shared sqlite3 connection (`check_same_thread=False`) makes a
    transaction connection-level, not statement-level: any thread's commit
    or implicit rollback can tear another thread's in-flight transaction.
    Wrapping every public method's *entire* body in the same lock makes
    each method atomic with respect to every other thread, writers and
    readers alike. RLock (not Lock) because some public methods call
    other public methods internally (see `unsubscribe` -> `subscribers_of`
    and `page_news` -> `filters_for`); a plain Lock would self-deadlock.
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
        # SQLite's builtin lower() only folds ASCII (lower('CAFÉ') == 'cafÉ'),
        # but filters.evaluate() -- used to match the same include/highlight
        # patterns on the live WebSocket push -- uses Python's Unicode-aware
        # str.lower(). Overriding lower() here makes page_news's SQL-side
        # haystack folding agree with the broadcaster's Python-side folding,
        # so a non-ASCII pattern doesn't match live but vanish on reload.
        self.db.create_function(
            "lower", 1, lambda s: s.lower() if s is not None else None, deterministic=True
        )
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.executescript(POST_MIGRATION_SCHEMA)
        self.db.commit()

    def _migrate(self) -> None:
        """Bring a database created by an older version up to the current schema."""
        user_columns = {
            r["name"] for r in self.db.execute("PRAGMA table_info(users)").fetchall()
        }
        if "token" not in user_columns:
            # Existing deployments have users and no tokens. The column arrives
            # NULL, and a NULL token authenticates nothing, so those accounts are
            # closed until boot reconciliation writes the configured value.
            self.db.execute("ALTER TABLE users ADD COLUMN token TEXT")

        columns = {
            r["name"] for r in self.db.execute("PRAGMA table_info(articles)").fetchall()
        }
        if "last_seen_at" not in columns:
            self.db.execute(
                "ALTER TABLE articles ADD COLUMN last_seen_at INTEGER NOT NULL DEFAULT 0"
            )
        # Rows predating the column (and any left at the default) are treated as
        # last seen when they were fetched, which is what retention used before.
        self.db.execute(
            "UPDATE articles SET last_seen_at = fetched_at WHERE last_seen_at = 0"
        )

        feed_columns = {
            r["name"] for r in self.db.execute("PRAGMA table_info(feeds)").fetchall()
        }
        if "favicon" not in feed_columns:
            # Existing feeds predate favicon resolution. The column arrives
            # NULL, meaning "not resolved yet"; a later poll cycle (Task 2)
            # fills it in.
            self.db.execute("ALTER TABLE feeds ADD COLUMN favicon TEXT")
        if "group" not in feed_columns:
            # Existing feeds predate the group column (Task 2 of feed tabs).
            # It arrives NULL -- an ungrouped feed only ever appears under the
            # widget's "All" tab (Task 3), never disappears from the list.
            self.db.execute('ALTER TABLE feeds ADD COLUMN "group" TEXT')
        if "title_format" not in feed_columns:
            # Existing feeds predate per-feed headline templates. NULL means
            # "store the entry title verbatim", which is what they always did.
            self.db.execute("ALTER TABLE feeds ADD COLUMN title_format TEXT")
        self.db.commit()

    @_synchronized
    def close(self) -> None:
        self.db.close()

    @_synchronized
    def upsert_user(
        self,
        user_id: str,
        name: str | None,
        now: int = 0,
        token: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO users (id, name, created_at, token) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  name = COALESCE(excluded.name, users.name), "
            "  token = COALESCE(excluded.token, users.token)",
            (user_id, name, now, token),
        )
        self.db.commit()

    @_synchronized
    def token_for(self, user_id: str) -> str | None:
        row = self.db.execute(
            "SELECT token FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return row["token"] if row else None

    @_synchronized
    def users_without_tokens(self) -> list[str]:
        rows = self.db.execute(
            "SELECT id FROM users WHERE token IS NULL OR token = '' ORDER BY id"
        ).fetchall()
        return [r["id"] for r in rows]

    @_synchronized
    def revoke_tokens_except(self, configured_ids: set[str]) -> list[str]:
        """Clear the token of every user not in `configured_ids`.

        This is how a user removed from config.yaml is closed out: the row,
        their feeds, subscriptions, and articles are left untouched -- only
        the credential is withdrawn, setting token to NULL (never '') so it
        matches the representation `users_without_tokens` and `token_ok`
        already treat as closed.

        Returns only the ids whose token was actually cleared, i.e. it
        excludes users who already had no token, so a caller logging the
        result reports a real transition rather than restating existing
        orphans every reconcile.

        An empty `configured_ids` revokes nobody. A config with no users at
        all reads the same as this call getting an empty set, and treating
        that as "revoke everyone in the database" would turn a misread or
        truncated config file into a mass lockout -- the opposite of
        additive reconciliation. Silence is safer here than a global revoke.
        """
        if not configured_ids:
            return []
        placeholders = ",".join("?" * len(configured_ids))
        ids = tuple(configured_ids)
        rows = self.db.execute(
            f"SELECT id FROM users WHERE id NOT IN ({placeholders}) "
            "AND token IS NOT NULL AND token != '' ORDER BY id",
            ids,
        ).fetchall()
        revoked = [r["id"] for r in rows]
        if revoked:
            self.db.execute(
                f"UPDATE users SET token = NULL WHERE id NOT IN ({placeholders}) "
                "AND token IS NOT NULL AND token != ''",
                ids,
            )
            self.db.commit()
        return revoked

    @_synchronized
    def clear_token(self, user_id: str) -> None:
        """Set a user's token to NULL, unconditionally.

        Used when config is authoritative that a user should have no token
        (tailscale_auth with no `token:` line) -- upsert_user's COALESCE
        would otherwise preserve whatever was stored, keeping a retired
        token alive after the switch to identity auth.
        """
        self.db.execute("UPDATE users SET token = NULL WHERE id = ?", (user_id,))
        self.db.commit()

    @_synchronized
    def user_exists(self, user_id: str) -> bool:
        row = self.db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
        return row is not None

    @_synchronized
    def upsert_feed(
        self,
        url: str,
        name: str | None = None,
        poll_interval_s: int | None = None,
        now: int = 0,
        group: str | None = None,
        title_format: str | None = None,
    ) -> int:
        self.db.execute(
            'INSERT INTO feeds (url, name, poll_interval_s, "group", title_format) '
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET "
            "  name = COALESCE(excluded.name, feeds.name), "
            "  poll_interval_s = COALESCE(excluded.poll_interval_s, feeds.poll_interval_s), "
            '  "group" = COALESCE(excluded."group", feeds."group"), '
            "  title_format = COALESCE(excluded.title_format, feeds.title_format)",
            (url, name, poll_interval_s, group, title_format),
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
    def subscribe(self, user_id: str, feed_id: int) -> None:
        self.db.execute(
            "INSERT INTO subscriptions (user_id, feed_id) VALUES (?, ?) "
            "ON CONFLICT DO NOTHING",
            (user_id, feed_id),
        )
        self.db.execute("UPDATE feeds SET enabled = 1 WHERE id = ?", (feed_id,))
        self.db.commit()

    @_synchronized
    def unsubscribe(self, user_id: str, feed_id: int) -> bool:
        cur = self.db.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND feed_id = ?", (user_id, feed_id)
        )
        removed = cur.rowcount > 0
        if removed and not self.subscribers_of(feed_id):
            self.db.execute("UPDATE feeds SET enabled = 0 WHERE id = ?", (feed_id,))
        self.db.commit()
        return removed

    @_synchronized
    def subscribers_of(self, feed_id: int) -> list[str]:
        rows = self.db.execute(
            "SELECT user_id FROM subscriptions WHERE feed_id = ? ORDER BY user_id", (feed_id,)
        ).fetchall()
        return [r["user_id"] for r in rows]

    @_synchronized
    def list_feeds(self, user_id: str) -> list[Feed]:
        rows = self.db.execute(
            "SELECT f.* FROM feeds f "
            "JOIN subscriptions s ON s.feed_id = f.id "
            "WHERE s.user_id = ? ORDER BY f.id",
            (user_id,),
        ).fetchall()
        return [_feed(r) for r in rows]

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
                    "(feed_id, guid, title, link, summary, published_at, fetched_at, "
                    " sort_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
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
    def add_filter(self, user_id: str, pattern: str, action: str) -> None:
        self.db.execute(
            "INSERT INTO filter_rules (user_id, pattern, action) VALUES (?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            (user_id, pattern, action),
        )
        self.db.commit()

    @_synchronized
    def filters_for(self, user_id: str) -> list[FilterRule]:
        rows = self.db.execute(
            "SELECT pattern, action FROM filter_rules WHERE user_id = ? AND enabled = 1",
            (user_id,),
        ).fetchall()
        return [FilterRule(pattern=r["pattern"], action=r["action"]) for r in rows]

    @_synchronized
    def page_news(
        self,
        user_id: str,
        limit: int,
        before: str | None = None,
        after: str | None = None,
    ) -> tuple[list[Article], str | None]:
        """Page a user's news feed.

        `before` and the no-cursor default page NEWEST-FIRST, walking backward
        in time (each next_cursor moves further into the past).

        `after` is a forward walk-forward gap fill (e.g. for WebSocket
        reconnects retrieving articles missed while disconnected) and returns
        results OLDEST-FIRST instead: it orders ascending and the returned
        next_cursor is the newest row of the page, so that chaining `after`
        calls advances forward through time without skipping or repeating
        rows. This ordering asymmetry between `before`/default and `after` is
        intentional but easy to miss.
        """
        where = ["s.user_id = ?"]
        params: list[object] = [user_id]

        if before:
            sort_at, article_id = decode_cursor(before)
            where.append("(a.sort_at, a.id) < (?, ?)")
            params += [sort_at, article_id]
        if after:
            sort_at, article_id = decode_cursor(after)
            where.append("(a.sort_at, a.id) > (?, ?)")
            params += [sort_at, article_id]

        patterns = include_patterns(self.filters_for(user_id))
        if patterns:
            clause = " OR ".join(
                ["lower(a.title || ' ' || COALESCE(a.summary, '')) LIKE ? ESCAPE '\\'"]
                * len(patterns)
            )
            where.append(f"({clause})")
            params += [f"%{_like_escape(p)}%" for p in patterns]

        order = "ASC" if after else "DESC"
        sql = (
            "SELECT a.* FROM articles a "
            "JOIN subscriptions s ON s.feed_id = a.feed_id "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY a.sort_at {order}, a.id {order} LIMIT ?"
        )
        params.append(limit + 1)
        rows = self.db.execute(sql, params).fetchall()

        has_more = len(rows) > limit
        rows = rows[:limit]
        articles = [_article(r) for r in rows]
        next_cursor = (
            encode_cursor(articles[-1].sort_at, articles[-1].id)
            if has_more and articles
            else None
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
