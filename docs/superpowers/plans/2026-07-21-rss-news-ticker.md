# RSS news ticker server implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Dockerized server that polls RSS feeds, pushes new articles to WebSocket clients in real time, caches history in SQLite for cursor-paged scrollback, and serves a Bloomberg-style news window widget to OpenBB Workspace via an iframe.

**Architecture:** One process, one SQLite file, six modules. `poller` fetches and writes; `api` reads and serves; they never reference each other, meeting only at `store` and `broadcast`. Feeds are global and deduplicated by URL; users subscribe to them. The widget is static HTML/JS served from this server's own origin, so its REST and WebSocket calls are same-origin.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, httpx, feedparser, PyYAML, stdlib sqlite3 (no ORM). Tests with pytest + pytest-asyncio. Lint with ruff. Packaged as a multi-arch Docker image.

**Spec:** `docs/superpowers/specs/2026-07-21-rss-news-ticker-design.md`

## Global constraints

Every task's requirements implicitly include these.

- **Python 3.12** exactly. The host has 3.13 and no 3.12; use `uv python install 3.12` and `uv venv --python 3.12`.
- **No ORM.** Raw SQL through stdlib `sqlite3` only.
- **Timestamps are INTEGER epoch seconds, UTC.** Never store ISO strings, never store local time.
- **`now` is always an injected parameter in business logic** — `store`, `poller`, `normalize`, and `filters` never call `time.time()` themselves, so tests control the clock. Reading the clock is the job of the outer boundary only: API request handlers and the background loops in `main.py`. Calling `time.time()` there is correct, not a violation.
- **Cursor paging only.** No `OFFSET` anywhere.
- **Nothing auto-disables on error** except a feed losing its last subscriber.
- **Config reconciliation is additive.** Boot never deletes users, feeds, subscriptions, or filters.
- **Every dependency must ship prebuilt wheels for both `linux/amd64` and `linux/arm64`**, or the multi-arch build breaks. Compiled extensions are fine when both wheels exist — `uvloop` and `httptools` (via `uvicorn[standard]`) publish manylinux wheels for aarch64 and x86_64, verified 2026-07-21. What is forbidden is a dependency that is source-only on one architecture, which is what forced `openbb-docker` to amd64-only. Do not drop `uvicorn[standard]`: Task 18 uses the `websockets` client it brings in.
- **Copy rule:** user-visible strings — log messages and API `detail` values — are sentence case, no trailing period, no exclamation marks. This does **not** cover Python exception messages (`raise ConfigError("public_base_url is required")`), which keep the lowercase, no-period convention used by the stdlib. Do not flag lowercase exception text as a defect.
- SQLite row-value comparison requires **3.15+**; assert at startup.

---

### Task 1: Project scaffold and config loading

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/rss_ticker/__init__.py`
- Create: `src/rss_ticker/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `FeedConfig(url: str, name: str | None, poll_interval_s: int | None)`
  - `FilterConfig(pattern: str, action: str)`
  - `UserConfig(id: str, name: str | None, feeds: tuple[FeedConfig, ...], filters: tuple[FilterConfig, ...])`
  - `Config(public_base_url: str, admin_key: str, retention_days: int, default_poll_interval_s: int, max_concurrent_polls: int, users: tuple[UserConfig, ...])`
  - `load_config(path: Path, env: Mapping[str, str]) -> Config`
  - `ConfigError(Exception)`

- [ ] **Step 1: Create the project files**

`pyproject.toml`:

```toml
[project]
name = "rss-ticker"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "httpx>=0.27",
    "feedparser>=6.0.11",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "ruff>=0.7"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/rss_ticker"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
```

`.gitignore`:

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
/data/
config.yaml
```

`src/rss_ticker/__init__.py`:

```python
__version__ = "0.1.0"
```

- [ ] **Step 2: Create the environment**

```bash
cd ~/Developer/rss-ticker
uv python install 3.12
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Expected: `Installed ... fastapi ... feedparser ...` and no resolution errors.

- [ ] **Step 3: Write the failing test**

`tests/test_config.py`:

```python
import pytest
from pathlib import Path
from rss_ticker.config import load_config, ConfigError


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_loads_full_config(tmp_path):
    p = write(tmp_path, """
public_base_url: http://nas.local:8088
admin_key: ${TICKER_ADMIN_KEY}
retention_days: 3
default_poll_interval_s: 120
max_concurrent_polls: 4
users:
  - id: art
    name: Art
    feeds:
      - {url: "https://a.example/rss", name: A}
      - {url: "https://b.example/rss", poll_interval_s: 600}
    filters:
      - {pattern: nvidia, action: highlight}
""")
    cfg = load_config(p, {"TICKER_ADMIN_KEY": "s3cret"})
    assert cfg.public_base_url == "http://nas.local:8088"
    assert cfg.admin_key == "s3cret"
    assert cfg.retention_days == 3
    assert cfg.max_concurrent_polls == 4
    assert len(cfg.users) == 1
    u = cfg.users[0]
    assert u.id == "art"
    assert u.feeds[0].name == "A"
    assert u.feeds[0].poll_interval_s is None
    assert u.feeds[1].poll_interval_s == 600
    assert u.filters[0].action == "highlight"


def test_defaults_applied(tmp_path):
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: k\n")
    cfg = load_config(p, {})
    assert cfg.retention_days == 7
    assert cfg.default_poll_interval_s == 300
    assert cfg.max_concurrent_polls == 8
    assert cfg.users == ()


def test_missing_public_base_url_is_error(tmp_path):
    p = write(tmp_path, "admin_key: k\n")
    with pytest.raises(ConfigError, match="public_base_url"):
        load_config(p, {})


def test_unset_env_var_is_error(tmp_path):
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: ${NOPE}\n")
    with pytest.raises(ConfigError, match="NOPE"):
        load_config(p, {})


def test_bad_filter_action_is_error(tmp_path):
    p = write(tmp_path, """
public_base_url: http://x
admin_key: k
users:
  - id: art
    filters:
      - {pattern: p, action: banish}
""")
    with pytest.raises(ConfigError, match="banish"):
        load_config(p, {})


def test_duplicate_user_id_is_error(tmp_path):
    p = write(tmp_path, """
public_base_url: http://x
admin_key: k
users:
  - {id: art}
  - {id: art}
""")
    with pytest.raises(ConfigError, match="duplicate user id"):
        load_config(p, {})
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rss_ticker.config'`

- [ ] **Step 5: Write the implementation**

`src/rss_ticker/config.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ACTIONS = ("include", "highlight")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class FeedConfig:
    url: str
    name: str | None = None
    poll_interval_s: int | None = None


@dataclass(frozen=True)
class FilterConfig:
    pattern: str
    action: str


@dataclass(frozen=True)
class UserConfig:
    id: str
    name: str | None = None
    feeds: tuple[FeedConfig, ...] = ()
    filters: tuple[FilterConfig, ...] = ()


@dataclass(frozen=True)
class Config:
    public_base_url: str
    admin_key: str
    retention_days: int = 7
    default_poll_interval_s: int = 300
    max_concurrent_polls: int = 8
    users: tuple[UserConfig, ...] = ()


def _expand(value, env: Mapping[str, str]):
    if not isinstance(value, str):
        return value

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in env:
            raise ConfigError(f"config references unset environment variable {name}")
        return env[name]

    return _ENV_RE.sub(sub, value)


def _walk(node, env: Mapping[str, str]):
    if isinstance(node, dict):
        return {k: _walk(v, env) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, env) for v in node]
    return _expand(node, env)


def _feed(raw: dict) -> FeedConfig:
    url = raw.get("url")
    if not url:
        raise ConfigError("every feed needs a url")
    return FeedConfig(
        url=url,
        name=raw.get("name"),
        poll_interval_s=raw.get("poll_interval_s"),
    )


def _filter(raw: dict) -> FilterConfig:
    pattern = raw.get("pattern")
    action = raw.get("action")
    if not pattern:
        raise ConfigError("every filter needs a pattern")
    if action not in _ACTIONS:
        raise ConfigError(f"filter action {action!r} must be one of {_ACTIONS}")
    return FilterConfig(pattern=pattern, action=action)


def _user(raw: dict) -> UserConfig:
    uid = raw.get("id")
    if not uid:
        raise ConfigError("every user needs an id")
    return UserConfig(
        id=uid,
        name=raw.get("name"),
        feeds=tuple(_feed(f) for f in raw.get("feeds") or []),
        filters=tuple(_filter(f) for f in raw.get("filters") or []),
    )


def load_config(path: Path, env: Mapping[str, str]) -> Config:
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config is not valid yaml: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    raw = _walk(raw, env)

    if not raw.get("public_base_url"):
        raise ConfigError("public_base_url is required")
    if not raw.get("admin_key"):
        raise ConfigError("admin_key is required")

    users = tuple(_user(u) for u in raw.get("users") or [])
    seen: set[str] = set()
    for u in users:
        if u.id in seen:
            raise ConfigError(f"duplicate user id {u.id!r}")
        seen.add(u.id)

    return Config(
        public_base_url=raw["public_base_url"].rstrip("/"),
        admin_key=raw["admin_key"],
        retention_days=int(raw.get("retention_days", 7)),
        default_poll_interval_s=int(raw.get("default_poll_interval_s", 300)),
        max_concurrent_polls=int(raw.get("max_concurrent_polls", 8)),
        users=users,
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS — 6 passed

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore src tests
git commit -m "feat: project scaffold and config loading"
```

---

### Task 2: Store — schema, entities, subscriptions

**Files:**
- Create: `src/rss_ticker/store.py`
- Test: `tests/test_store_entities.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Article(id: int, feed_id: int, guid: str, title: str, link: str | None, summary: str | None, published_at: int | None, fetched_at: int, sort_at: int)`
  - `Feed(id: int, url: str, name: str | None, poll_interval_s: int | None, enabled: bool)`
  - `FeedState(feed_id: int, etag: str | None, last_modified: str | None, last_polled_at: int | None, last_success_at: int | None, consecutive_failures: int, last_error: str | None, next_poll_at: int)`
  - `Store(path: str)` with `close()`, `upsert_user(user_id, name)`, `upsert_feed(url, name=None, poll_interval_s=None, now=0) -> int`, `subscribe(user_id, feed_id)`, `unsubscribe(user_id, feed_id) -> bool`, `list_feeds(user_id) -> list[Feed]`, `get_feed(feed_id) -> Feed | None`, `subscribers_of(feed_id) -> list[str]`, `user_exists(user_id) -> bool`

- [ ] **Step 1: Write the failing test**

`tests/test_store_entities.py`:

```python
import pytest
from rss_ticker.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_upsert_feed_is_idempotent_by_url(store):
    a = store.upsert_feed("https://x.example/rss", name="X", now=100)
    b = store.upsert_feed("https://x.example/rss", name="X renamed", now=200)
    assert a == b
    assert store.get_feed(a).name == "X renamed"


def test_upsert_feed_creates_feed_state_with_next_poll_now(store):
    fid = store.upsert_feed("https://x.example/rss", now=100)
    st = store.get_feed_state(fid)
    assert st.next_poll_at == 100
    assert st.last_success_at is None
    assert st.consecutive_failures == 0


def test_subscribe_is_idempotent(store):
    store.upsert_user("art", "Art")
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    store.subscribe("art", fid)
    assert [f.id for f in store.list_feeds("art")] == [fid]


def test_two_users_share_one_feed_row(store):
    store.upsert_user("art", "Art")
    store.upsert_user("bob", "Bob")
    a = store.upsert_feed("https://x.example/rss", now=0)
    b = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", a)
    store.subscribe("bob", b)
    assert a == b
    assert sorted(store.subscribers_of(a)) == ["art", "bob"]


def test_unsubscribe_keeps_feed_for_other_subscribers(store):
    store.upsert_user("art", None)
    store.upsert_user("bob", None)
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    store.subscribe("bob", fid)
    store.unsubscribe("art", fid)
    assert store.get_feed(fid).enabled is True
    assert store.subscribers_of(fid) == ["bob"]


def test_last_unsubscribe_disables_feed_but_keeps_row(store):
    store.upsert_user("art", None)
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    assert store.unsubscribe("art", fid) is True
    feed = store.get_feed(fid)
    assert feed is not None
    assert feed.enabled is False


def test_resubscribe_reenables_feed(store):
    store.upsert_user("art", None)
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    store.unsubscribe("art", fid)
    store.subscribe("art", fid)
    assert store.get_feed(fid).enabled is True


def test_unsubscribe_returns_false_when_not_subscribed(store):
    store.upsert_user("art", None)
    fid = store.upsert_feed("https://x.example/rss", now=0)
    assert store.unsubscribe("art", fid) is False


def test_user_exists(store):
    assert store.user_exists("art") is False
    store.upsert_user("art", None)
    assert store.user_exists("art") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store_entities.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rss_ticker.store'`

- [ ] **Step 3: Write the implementation**

`src/rss_ticker/store.py`:

```python
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

MIN_SQLITE = (3, 15, 0)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT,
    created_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    name TEXT,
    poll_interval_s INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1
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
CREATE INDEX IF NOT EXISTS idx_articles_fetched ON articles (fetched_at);
CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions (user_id);
"""


@dataclass(frozen=True)
class Feed:
    id: int
    url: str
    name: str | None
    poll_interval_s: int | None
    enabled: bool


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


def _feed(row: sqlite3.Row) -> Feed:
    return Feed(
        id=row["id"],
        url=row["url"],
        name=row["name"],
        poll_interval_s=row["poll_interval_s"],
        enabled=bool(row["enabled"]),
    )


class Store:
    def __init__(self, path: str) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise RuntimeError(
                f"sqlite {'.'.join(map(str, MIN_SQLITE))}+ required for row-value paging, "
                f"found {sqlite3.sqlite_version}"
            )
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def upsert_user(self, user_id: str, name: str | None, now: int = 0) -> None:
        self.db.execute(
            "INSERT INTO users (id, name, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = COALESCE(excluded.name, users.name)",
            (user_id, name, now),
        )
        self.db.commit()

    def user_exists(self, user_id: str) -> bool:
        row = self.db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
        return row is not None

    def upsert_feed(
        self,
        url: str,
        name: str | None = None,
        poll_interval_s: int | None = None,
        now: int = 0,
    ) -> int:
        self.db.execute(
            "INSERT INTO feeds (url, name, poll_interval_s) VALUES (?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET "
            "  name = COALESCE(excluded.name, feeds.name), "
            "  poll_interval_s = COALESCE(excluded.poll_interval_s, feeds.poll_interval_s)",
            (url, name, poll_interval_s),
        )
        feed_id = self.db.execute("SELECT id FROM feeds WHERE url = ?", (url,)).fetchone()["id"]
        self.db.execute(
            "INSERT INTO feed_state (feed_id, next_poll_at) VALUES (?, ?) "
            "ON CONFLICT(feed_id) DO NOTHING",
            (feed_id, now),
        )
        self.db.commit()
        return feed_id

    def get_feed(self, feed_id: int) -> Feed | None:
        row = self.db.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        return _feed(row) if row else None

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

    def subscribe(self, user_id: str, feed_id: int) -> None:
        self.db.execute(
            "INSERT INTO subscriptions (user_id, feed_id) VALUES (?, ?) "
            "ON CONFLICT DO NOTHING",
            (user_id, feed_id),
        )
        self.db.execute("UPDATE feeds SET enabled = 1 WHERE id = ?", (feed_id,))
        self.db.commit()

    def unsubscribe(self, user_id: str, feed_id: int) -> bool:
        cur = self.db.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND feed_id = ?", (user_id, feed_id)
        )
        removed = cur.rowcount > 0
        if removed and not self.subscribers_of(feed_id):
            self.db.execute("UPDATE feeds SET enabled = 0 WHERE id = ?", (feed_id,))
        self.db.commit()
        return removed

    def subscribers_of(self, feed_id: int) -> list[str]:
        rows = self.db.execute(
            "SELECT user_id FROM subscriptions WHERE feed_id = ? ORDER BY user_id", (feed_id,)
        ).fetchall()
        return [r["user_id"] for r in rows]

    def list_feeds(self, user_id: str) -> list[Feed]:
        rows = self.db.execute(
            "SELECT f.* FROM feeds f "
            "JOIN subscriptions s ON s.feed_id = f.id "
            "WHERE s.user_id = ? ORDER BY f.id",
            (user_id,),
        ).fetchall()
        return [_feed(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_entities.py -v`
Expected: PASS — 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/rss_ticker/store.py tests/test_store_entities.py
git commit -m "feat: store schema, feeds, users, subscriptions"
```

---

### Task 3: Store — article insert with dedup

**Files:**
- Modify: `src/rss_ticker/store.py` (append methods to `Store`)
- Test: `tests/test_store_articles.py`

**Interfaces:**
- Consumes: `Store`, `Article` from Task 2
- Produces:
  - `NewArticle(guid: str, title: str, link: str | None, summary: str | None, published_at: int | None)` — the pre-insert shape
  - `Store.insert_articles(feed_id: int, entries: list[NewArticle], now: int) -> list[Article]` returning only rows that were genuinely new

- [ ] **Step 1: Write the failing test**

`tests/test_store_articles.py`:

```python
import pytest
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store_articles.py -v`
Expected: FAIL — `ImportError: cannot import name 'NewArticle'`

- [ ] **Step 3: Add `NewArticle` beside the other dataclasses in `store.py`**

```python
@dataclass(frozen=True)
class NewArticle:
    guid: str
    title: str
    link: str | None
    summary: str | None
    published_at: int | None
```

- [ ] **Step 4: Add the insert method to `Store`**

```python
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
                    e.published_at if e.published_at is not None else now,
                )
            )
        inserted: list[Article] = []
        with self.db:
            for row in rows:
                cur = self.db.execute(
                    "INSERT INTO articles "
                    "(feed_id, guid, title, link, summary, published_at, fetched_at, sort_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (feed_id, guid) DO NOTHING "
                    "RETURNING *",
                    row,
                )
                got = cur.fetchone()
                if got is not None:
                    inserted.append(_article(got))
        return inserted
```

- [ ] **Step 5: Add the `_article` row mapper next to `_feed`**

```python
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
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_articles.py -v`
Expected: PASS — 8 passed

- [ ] **Step 7: Commit**

```bash
git add src/rss_ticker/store.py tests/test_store_articles.py
git commit -m "feat: article insert with guid dedup"
```

---

### Task 4: Filters

**Files:**
- Create: `src/rss_ticker/filters.py`
- Test: `tests/test_filters.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `FilterRule(pattern: str, action: str)`
  - `evaluate(rules: list[FilterRule], title: str, summary: str | None) -> tuple[bool, bool]` returning `(included, highlighted)`
  - `highlights(rules: list[FilterRule], title: str, summary: str | None) -> bool` — the highlight half alone, for callers that already applied inclusion in SQL
  - `include_patterns(rules) -> list[str]` — lowercased patterns of `include` rules, for SQL

- [ ] **Step 1: Write the failing test**

`tests/test_filters.py`:

```python
from rss_ticker.filters import FilterRule, evaluate, include_patterns


def test_no_rules_includes_everything():
    assert evaluate([], "Anything", None) == (True, False)


def test_only_highlight_rules_still_includes_everything():
    rules = [FilterRule("nvidia", "highlight")]
    assert evaluate(rules, "Apple ships", None) == (True, False)
    assert evaluate(rules, "Nvidia beats", None) == (True, True)


def test_include_rule_excludes_non_matching():
    rules = [FilterRule("fed", "include")]
    assert evaluate(rules, "Fed holds rates", None) == (True, False)
    assert evaluate(rules, "Oil slips", None) == (False, False)


def test_any_include_rule_matching_is_enough():
    rules = [FilterRule("fed", "include"), FilterRule("oil", "include")]
    assert evaluate(rules, "Oil slips", None)[0] is True


def test_matching_is_case_insensitive():
    assert evaluate([FilterRule("NVIDIA", "highlight")], "nvidia up", None) == (True, True)


def test_summary_is_searched_too():
    rules = [FilterRule("earnings", "include")]
    assert evaluate(rules, "Chipmaker update", "Quarterly earnings beat")[0] is True


def test_missing_summary_does_not_crash():
    assert evaluate([FilterRule("x", "include")], "no match", None) == (False, False)


def test_include_and_highlight_combine():
    rules = [FilterRule("fed", "include"), FilterRule("rates", "highlight")]
    assert evaluate(rules, "Fed holds rates", None) == (True, True)


def test_include_patterns_lowercases_and_filters_by_action():
    rules = [FilterRule("Fed", "include"), FilterRule("NVDA", "highlight")]
    assert include_patterns(rules) == ["fed"]


def test_highlights_ignores_include_rules():
    rules = [FilterRule("fed", "include")]
    assert highlights(rules, "Fed holds rates", None) is False


def test_highlights_matches_highlight_rules():
    rules = [FilterRule("nvidia", "highlight")]
    assert highlights(rules, "Nvidia beats", None) is True
    assert highlights(rules, "Oil slips", None) is False


def test_highlights_agrees_with_evaluate():
    rules = [FilterRule("fed", "include"), FilterRule("rates", "highlight")]
    assert highlights(rules, "Fed holds rates", None) == evaluate(
        rules, "Fed holds rates", None
    )[1]
```

Update the import line at the top of this file to:

```python
from rss_ticker.filters import FilterRule, evaluate, highlights, include_patterns
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_filters.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rss_ticker.filters'`

- [ ] **Step 3: Write the implementation**

`src/rss_ticker/filters.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FilterRule:
    pattern: str
    action: str


def _haystack(title: str, summary: str | None) -> str:
    return f"{title} {summary or ''}".lower()


def include_patterns(rules: list[FilterRule]) -> list[str]:
    return [r.pattern.lower() for r in rules if r.action == "include"]


def highlights(rules: list[FilterRule], title: str, summary: str | None) -> bool:
    hay = _haystack(title, summary)
    return any(r.pattern.lower() in hay for r in rules if r.action == "highlight")


def evaluate(rules: list[FilterRule], title: str, summary: str | None) -> tuple[bool, bool]:
    hay = _haystack(title, summary)
    includes = include_patterns(rules)
    included = True if not includes else any(p in hay for p in includes)
    return included, highlights(rules, title, summary)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_filters.py -v`
Expected: PASS — 12 passed

- [ ] **Step 5: Commit**

```bash
git add src/rss_ticker/filters.py tests/test_filters.py
git commit -m "feat: substring filter evaluation"
```

---

### Task 5: Store — cursor paging and filter rules

**Files:**
- Modify: `src/rss_ticker/store.py`
- Test: `tests/test_store_paging.py`

**Interfaces:**
- Consumes: `Store` (Task 2/3), `FilterRule` (Task 4)
- Produces:
  - `encode_cursor(sort_at: int, id: int) -> str`, `decode_cursor(s: str) -> tuple[int, int]`, `CursorError(Exception)`
  - `Store.add_filter(user_id, pattern, action)`, `Store.filters_for(user_id) -> list[FilterRule]`
  - `Store.page_news(user_id: str, limit: int, before: str | None = None, after: str | None = None) -> tuple[list[Article], str | None]` returning `(articles, next_cursor)`. **`before` and the no-cursor default return NEWEST-FIRST** (walking backward in time). **`after` returns OLDEST-FIRST** — it is a forward walk-forward gap fill for WebSocket reconnects, so it orders ascending and its `next_cursor` is the newest row of the page. That asymmetry is deliberate: ordering `after` descending and truncating to `limit` returns the newest N rows past the cursor rather than the N immediately following it, which permanently skips articles and re-serves others whenever the backlog exceeds one page. `next_cursor` is `None` when exhausted.
  - `_like_escape(value: str) -> str` — escapes `\`, `%`, `_` so a user's filter pattern cannot act as a SQL wildcard

- [ ] **Step 1: Write the failing test**

`tests/test_store_paging.py`:

```python
import pytest
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store_paging.py -v`
Expected: FAIL — `ImportError: cannot import name 'decode_cursor'`

- [ ] **Step 3: Add cursor helpers to the top of `store.py`, after the imports**

```python
import base64
from .filters import FilterRule, include_patterns


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
```

- [ ] **Step 4: Add paging and filter methods to `Store`**

```python
    def add_filter(self, user_id: str, pattern: str, action: str) -> None:
        self.db.execute(
            "INSERT INTO filter_rules (user_id, pattern, action) VALUES (?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            (user_id, pattern, action),
        )
        self.db.commit()

    def filters_for(self, user_id: str) -> list[FilterRule]:
        rows = self.db.execute(
            "SELECT pattern, action FROM filter_rules WHERE user_id = ? AND enabled = 1",
            (user_id,),
        ).fetchall()
        return [FilterRule(pattern=r["pattern"], action=r["action"]) for r in rows]

    def page_news(
        self,
        user_id: str,
        limit: int,
        before: str | None = None,
        after: str | None = None,
    ) -> tuple[list[Article], str | None]:
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
```

The `include` patterns are OR-joined inside a single parenthesized clause because the
spec requires **any** include rule matching to admit the article. AND-ing them (one
`where.append` per pattern) would require every rule to match, which is the opposite
behavior — the regression test in Step 6 pins this.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_paging.py -v`
Expected: PASS — 9 passed

- [ ] **Step 6: Add a multi-include regression test**

Append to `tests/test_store_paging.py`:

```python
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
```

Run: `uv run pytest tests/test_store_paging.py -v`
Expected: PASS — 10 passed

- [ ] **Step 7: Commit**

```bash
git add src/rss_ticker/store.py tests/test_store_paging.py
git commit -m "feat: cursor paging and per-user filter rules"
```

---

### Task 6: Store — retention sweep and feed scheduling state

**Files:**
- Modify: `src/rss_ticker/store.py`
- Test: `tests/test_store_retention.py`

**Interfaces:**
- Consumes: `Store` (Tasks 2–5)
- Produces:
  - `Store.sweep(now: int, retention_days: int) -> int` (rows deleted)
  - `Store.vacuum()`
  - `Store.due_feeds(now: int) -> list[Feed]`
  - `Store.record_success(feed_id, *, etag, last_modified, now, next_poll_at)`
  - `Store.record_failure(feed_id, *, error, now, next_poll_at)`
  - `Store.all_feed_status() -> list[dict]` for the health endpoint

- [ ] **Step 1: Write the failing test**

`tests/test_store_retention.py`:

```python
import pytest
from rss_ticker.store import Store, NewArticle


@pytest.fixture
def store():
    s = Store(":memory:")
    s.upsert_user("art", None)
    yield s
    s.close()


DAY = 86400


def test_sweep_deletes_by_fetched_at_not_sort_at(store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    store.insert_articles(
        fid,
        [NewArticle("old-date-new-arrival", "t", None, None, published_at=1)],
        now=100 * DAY,
    )
    deleted = store.sweep(now=100 * DAY, retention_days=7)
    assert deleted == 0
    rows, _ = store.page_news("art", limit=10)
    assert len(rows) == 1


def test_sweep_deletes_articles_past_retention(store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.subscribe("art", fid)
    store.insert_articles(fid, [NewArticle("a", "t", None, None, None)], now=1 * DAY)
    assert store.sweep(now=10 * DAY, retention_days=7) == 1
    assert store.page_news("art", limit=10)[0] == []


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store_retention.py -v`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'sweep'`

- [ ] **Step 3: Add the methods to `Store`**

```python
    def sweep(self, now: int, retention_days: int) -> int:
        cutoff = now - retention_days * 86400
        cur = self.db.execute("DELETE FROM articles WHERE fetched_at < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount

    def vacuum(self) -> None:
        self.db.execute("VACUUM")

    def due_feeds(self, now: int) -> list[Feed]:
        rows = self.db.execute(
            "SELECT f.* FROM feeds f "
            "JOIN feed_state st ON st.feed_id = f.id "
            "WHERE f.enabled = 1 AND st.next_poll_at <= ? ORDER BY st.next_poll_at",
            (now,),
        ).fetchall()
        return [_feed(r) for r in rows]

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

    def all_feed_status(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT f.id, f.url, f.name, f.enabled, st.last_polled_at, st.last_success_at, "
            "st.consecutive_failures, st.last_error, st.next_poll_at "
            "FROM feeds f JOIN feed_state st ON st.feed_id = f.id ORDER BY f.id"
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_retention.py -v`
Expected: PASS — 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/rss_ticker/store.py tests/test_store_retention.py
git commit -m "feat: retention sweep and feed scheduling state"
```

---

### Task 7: Config reconciliation on boot

**Files:**
- Create: `src/rss_ticker/reconcile.py`
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: `Config` (Task 1), `Store` (Tasks 2–6)
- Produces: `reconcile(store: Store, config: Config, now: int) -> None`

- [ ] **Step 1: Write the failing test**

`tests/test_reconcile.py`:

```python
import pytest
from rss_ticker.config import Config, UserConfig, FeedConfig, FilterConfig
from rss_ticker.reconcile import reconcile
from rss_ticker.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def cfg(users):
    return Config(public_base_url="http://x", admin_key="k", users=tuple(users))


def test_creates_users_feeds_subscriptions_and_filters(store):
    c = cfg([
        UserConfig(
            id="art",
            name="Art",
            feeds=(FeedConfig(url="https://a.example/rss", name="A"),),
            filters=(FilterConfig(pattern="nvidia", action="highlight"),),
        )
    ])
    reconcile(store, c, now=100)
    feeds = store.list_feeds("art")
    assert [f.url for f in feeds] == ["https://a.example/rss"]
    assert store.filters_for("art")[0].pattern == "nvidia"


def test_is_idempotent(store):
    c = cfg([UserConfig(id="art", feeds=(FeedConfig(url="https://a.example/rss"),))])
    reconcile(store, c, now=100)
    reconcile(store, c, now=200)
    assert len(store.list_feeds("art")) == 1


def test_does_not_delete_feeds_added_outside_config(store):
    reconcile(store, cfg([UserConfig(id="art")]), now=100)
    fid = store.upsert_feed("https://runtime.example/rss", now=100)
    store.subscribe("art", fid)
    reconcile(store, cfg([UserConfig(id="art")]), now=200)
    assert "https://runtime.example/rss" in [f.url for f in store.list_feeds("art")]


def test_does_not_delete_filters_added_outside_config(store):
    reconcile(store, cfg([UserConfig(id="art")]), now=100)
    store.add_filter("art", "runtime", "include")
    reconcile(store, cfg([UserConfig(id="art")]), now=200)
    assert "runtime" in [f.pattern for f in store.filters_for("art")]


def test_updates_feed_name_and_interval(store):
    reconcile(store, cfg([UserConfig(
        id="art", feeds=(FeedConfig(url="https://a.example/rss", name="Old"),))]), now=100)
    reconcile(store, cfg([UserConfig(
        id="art",
        feeds=(FeedConfig(url="https://a.example/rss", name="New", poll_interval_s=600),),
    )]), now=200)
    feed = store.list_feeds("art")[0]
    assert feed.name == "New"
    assert feed.poll_interval_s == 600


def test_shared_feed_is_polled_once(store):
    c = cfg([
        UserConfig(id="art", feeds=(FeedConfig(url="https://same.example/rss"),)),
        UserConfig(id="bob", feeds=(FeedConfig(url="https://same.example/rss"),)),
    ])
    reconcile(store, c, now=100)
    assert len(store.due_feeds(now=100)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rss_ticker.reconcile'`

- [ ] **Step 3: Write the implementation**

`src/rss_ticker/reconcile.py`:

```python
from __future__ import annotations

import logging

from .config import Config
from .store import Store

log = logging.getLogger(__name__)


def reconcile(store: Store, config: Config, now: int) -> None:
    """Additively apply config to the database. Never deletes."""
    for user in config.users:
        store.upsert_user(user.id, user.name, now=now)
        for feed in user.feeds:
            feed_id = store.upsert_feed(
                feed.url, name=feed.name, poll_interval_s=feed.poll_interval_s, now=now
            )
            store.subscribe(user.id, feed_id)
        for rule in user.filters:
            store.add_filter(user.id, rule.pattern, rule.action)
        log.info(
            "reconciled user %s with %d feeds and %d filters",
            user.id,
            len(user.feeds),
            len(user.filters),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: PASS — 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/rss_ticker/reconcile.py tests/test_reconcile.py
git commit -m "feat: additive config reconciliation on boot"
```

---

### Task 8: Feed entry normalization

**Files:**
- Create: `src/rss_ticker/normalize.py`
- Create: `tests/fixtures/simple.xml`
- Create: `tests/fixtures/no_guid.xml`
- Create: `tests/fixtures/malformed_with_entries.xml`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Consumes: `NewArticle` (Task 3)
- Produces: `normalize_entry(entry: dict, now: int) -> NewArticle | None` and `parse_feed(body: bytes, now: int) -> tuple[list[NewArticle], int]` returning `(entries, dropped_count)`

- [ ] **Step 1: Create the fixtures**

`tests/fixtures/simple.xml`:

```xml
<?xml version="1.0"?>
<rss version="2.0"><channel><title>Simple</title>
<item>
  <title>Fed holds rates steady</title>
  <link>https://ex.example/a</link>
  <guid>urn:a</guid>
  <description>The central bank left rates unchanged.</description>
  <pubDate>Mon, 21 Jul 2026 14:02:00 GMT</pubDate>
</item>
<item>
  <title>Oil slips below $70</title>
  <link>https://ex.example/b</link>
  <guid>urn:b</guid>
  <pubDate>Mon, 21 Jul 2026 13:40:00 GMT</pubDate>
</item>
</channel></rss>
```

`tests/fixtures/no_guid.xml`:

```xml
<?xml version="1.0"?>
<rss version="2.0"><channel><title>No guid</title>
<item><title>Headline with link only</title><link>https://ex.example/c</link></item>
<item><title>Headline with nothing else</title></item>
<item><link>https://ex.example/d</link></item>
</channel></rss>
```

`tests/fixtures/malformed_with_entries.xml`:

```xml
<?xml version="1.0"?>
<rss version="2.0"><channel><title>Broken & unescaped</title>
<item><title>Still parseable</title><guid>urn:z</guid></item>
</channel>
```

- [ ] **Step 2: Write the failing test**

`tests/test_normalize.py`:

```python
from pathlib import Path

from rss_ticker.normalize import parse_feed, normalize_entry

FIX = Path(__file__).parent / "fixtures"


def test_parses_titles_links_summaries_and_dates():
    entries, dropped = parse_feed((FIX / "simple.xml").read_bytes(), now=999)
    assert dropped == 0
    assert [e.title for e in entries] == ["Fed holds rates steady", "Oil slips below $70"]
    assert entries[0].guid == "urn:a"
    assert entries[0].link == "https://ex.example/a"
    assert "central bank" in entries[0].summary
    assert entries[0].published_at == 1784642520


def test_missing_publish_date_yields_none_not_now():
    entries, _ = parse_feed((FIX / "no_guid.xml").read_bytes(), now=999)
    assert entries[0].published_at is None


def test_guid_falls_back_to_link():
    entries, _ = parse_feed((FIX / "no_guid.xml").read_bytes(), now=999)
    assert entries[0].guid == "https://ex.example/c"


def test_guid_falls_back_to_hash_when_no_id_or_link():
    entries, _ = parse_feed((FIX / "no_guid.xml").read_bytes(), now=999)
    titles = {e.title: e for e in entries}
    hashed = titles["Headline with nothing else"]
    assert len(hashed.guid) == 64
    assert hashed.guid != "Headline with nothing else"


def test_hash_guid_is_stable_across_calls():
    a, _ = parse_feed((FIX / "no_guid.xml").read_bytes(), now=1)
    b, _ = parse_feed((FIX / "no_guid.xml").read_bytes(), now=2)
    assert [e.guid for e in a] == [e.guid for e in b]


def test_entry_without_title_is_dropped_and_counted():
    entries, dropped = parse_feed((FIX / "no_guid.xml").read_bytes(), now=999)
    assert dropped == 1
    assert all(e.title for e in entries)


def test_malformed_feed_with_entries_is_accepted():
    entries, _ = parse_feed((FIX / "malformed_with_entries.xml").read_bytes(), now=999)
    assert [e.title for e in entries] == ["Still parseable"]


def test_normalize_entry_returns_none_for_titleless_entry():
    assert normalize_entry({"link": "https://x"}, now=1) is None


def test_hash_guid_distinguishes_same_title_different_dates():
    a = normalize_entry({"title": "Same"}, now=1)
    b = normalize_entry({"title": "Same", "published_parsed": (2026, 7, 21, 0, 0, 0, 0, 1, 0)},
                        now=1)
    assert a.guid != b.guid
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_normalize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rss_ticker.normalize'`

- [ ] **Step 4: Write the implementation**

`src/rss_ticker/normalize.py`:

```python
from __future__ import annotations

import calendar
import hashlib

import feedparser

from .store import NewArticle


def _published(entry) -> int | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return calendar.timegm(value)
    return None


def _hash_guid(title: str, published_at: int | None) -> str:
    payload = f"{title}\x00{published_at if published_at is not None else ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_entry(entry, now: int) -> NewArticle | None:
    title = (entry.get("title") or "").strip()
    if not title:
        return None
    link = entry.get("link") or None
    summary = entry.get("summary") or None
    published_at = _published(entry)
    guid = entry.get("id") or link or _hash_guid(title, published_at)
    return NewArticle(
        guid=guid,
        title=title,
        link=link,
        summary=summary,
        published_at=published_at,
    )


def parse_feed(body: bytes, now: int) -> tuple[list[NewArticle], int]:
    parsed = feedparser.parse(body)
    entries: list[NewArticle] = []
    dropped = 0
    for raw in parsed.entries:
        item = normalize_entry(raw, now)
        if item is None:
            dropped += 1
        else:
            entries.append(item)
    return entries, dropped
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_normalize.py -v`
Expected: PASS — 9 passed

If `test_parses_titles_links_summaries_and_dates` fails on the epoch value, print the
actual value and update the assertion — the fixture date is fixed, so the correct
constant is whatever `calendar.timegm` yields for `Mon, 21 Jul 2026 14:02:00 GMT`.

- [ ] **Step 6: Commit**

```bash
git add src/rss_ticker/normalize.py tests/test_normalize.py tests/fixtures
git commit -m "feat: feed entry normalization with guid fallback chain"
```

---

### Task 9: HTTP fetch, conditional GET, and backoff policy

**Files:**
- Create: `src/rss_ticker/fetch.py`
- Test: `tests/test_fetch.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `FetchOutcome(status: str, body: bytes | None, etag: str | None, last_modified: str | None, error: str | None, retry_after: int | None)` where `status` is one of `"ok"`, `"not_modified"`, `"failed"`
  - `async fetch_feed(client: httpx.AsyncClient, url: str, etag: str | None, last_modified: str | None) -> FetchOutcome`
  - `next_interval(base_interval: int, consecutive_failures: int, retry_after: int | None) -> int`
  - `MAX_BACKOFF_S = 3600`, `MAX_BODY_BYTES = 5 * 1024 * 1024`, `TIMEOUT_S = 15.0`
  - `user_agent(version: str, base_url: str) -> str`

- [ ] **Step 1: Write the failing test**

`tests/test_fetch.py`:

```python
import httpx
import pytest

from rss_ticker.fetch import (
    MAX_BACKOFF_S,
    fetch_feed,
    next_interval,
    user_agent,
)


def transport(handler):
    return httpx.MockTransport(handler)


async def call(handler, etag=None, last_modified=None):
    async with httpx.AsyncClient(transport=transport(handler)) as client:
        return await fetch_feed(client, "https://x.example/rss", etag, last_modified)


async def test_200_returns_body_and_validators():
    def handler(request):
        return httpx.Response(
            200, content=b"<rss/>", headers={"ETag": '"v1"', "Last-Modified": "Mon"}
        )

    out = await call(handler)
    assert out.status == "ok"
    assert out.body == b"<rss/>"
    assert out.etag == '"v1"'
    assert out.last_modified == "Mon"


async def test_conditional_headers_are_sent_when_known():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(304)

    await call(handler, etag='"v1"', last_modified="Mon")
    assert seen["if-none-match"] == '"v1"'
    assert seen["if-modified-since"] == "Mon"


async def test_304_is_not_modified_not_failure():
    out = await call(lambda r: httpx.Response(304))
    assert out.status == "not_modified"
    assert out.error is None


async def test_500_is_failure():
    out = await call(lambda r: httpx.Response(500))
    assert out.status == "failed"
    assert "500" in out.error


async def test_429_captures_retry_after():
    out = await call(lambda r: httpx.Response(429, headers={"Retry-After": "120"}))
    assert out.status == "failed"
    assert out.retry_after == 120


async def test_non_numeric_retry_after_is_ignored():
    out = await call(lambda r: httpx.Response(503, headers={"Retry-After": "Wed, 21 Oct"}))
    assert out.retry_after is None


async def test_connection_error_is_failure_not_exception():
    def handler(request):
        raise httpx.ConnectError("refused")

    out = await call(handler)
    assert out.status == "failed"
    assert "refused" in out.error


async def test_oversized_body_is_rejected():
    def handler(request):
        return httpx.Response(200, content=b"x" * (6 * 1024 * 1024))

    out = await call(handler)
    assert out.status == "failed"
    assert "too large" in out.error


def test_next_interval_is_base_on_success():
    assert next_interval(300, consecutive_failures=0, retry_after=None) == 300


def test_next_interval_backs_off_exponentially():
    assert next_interval(300, consecutive_failures=1, retry_after=None) == 600
    assert next_interval(300, consecutive_failures=2, retry_after=None) == 1200


def test_next_interval_is_capped():
    assert next_interval(300, consecutive_failures=99, retry_after=None) == MAX_BACKOFF_S


def test_retry_after_overrides_backoff():
    assert next_interval(300, consecutive_failures=5, retry_after=45) == 45


def test_user_agent_includes_version_and_url():
    ua = user_agent("0.1.0", "http://nas.local:8088")
    assert "rss-ticker/0.1.0" in ua
    assert "nas.local" in ua
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fetch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rss_ticker.fetch'`

- [ ] **Step 3: Write the implementation**

`src/rss_ticker/fetch.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

import httpx

MAX_BACKOFF_S = 3600
MAX_BODY_BYTES = 5 * 1024 * 1024
TIMEOUT_S = 15.0


def user_agent(version: str, base_url: str) -> str:
    return f"rss-ticker/{version} (+{base_url})"


@dataclass(frozen=True)
class FetchOutcome:
    status: str
    body: bytes | None = None
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    retry_after: int | None = None


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def fetch_feed(
    client: httpx.AsyncClient,
    url: str,
    etag: str | None,
    last_modified: str | None,
) -> FetchOutcome:
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        async with client.stream(
            "GET", url, headers=headers, timeout=TIMEOUT_S, follow_redirects=True
        ) as response:
            if response.status_code == 304:
                return FetchOutcome(status="not_modified")

            if response.status_code >= 400:
                return FetchOutcome(
                    status="failed",
                    error=f"http {response.status_code}",
                    retry_after=_retry_after(response),
                )

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_BODY_BYTES:
                    return FetchOutcome(status="failed", error="response body too large")
                chunks.append(chunk)

            return FetchOutcome(
                status="ok",
                body=b"".join(chunks),
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
            )
    except Exception as exc:
        return FetchOutcome(status="failed", error=f"{type(exc).__name__}: {exc}")


def next_interval(
    base_interval: int, consecutive_failures: int, retry_after: int | None
) -> int:
    if retry_after is not None:
        return max(0, min(retry_after, MAX_BACKOFF_S))
    if consecutive_failures <= 0:
        return base_interval
    return min(base_interval * (2**consecutive_failures), MAX_BACKOFF_S)
```

Three things here are deliberate and must not be "cleaned up":

**`except Exception`, not `except httpx.HTTPError`.** `httpx.InvalidURL` and plain `ValueError`
(raised by httpx's URL parser on input like `not a url` or `http://[::1`) do **not** subclass
`HTTPError`. Config validation only checks that a feed URL is non-empty, so a YAML typo reaches
this function. `fetch_feed`'s contract is that it never raises, because the poller runs feeds
concurrently and an escaping exception disrupts unrelated feeds. A total boundary function
warrants a broad catch.

**`client.stream` with an incremental byte count, not `client.get` plus a length check.** With
a non-streaming `get`, the whole body is already in memory before any size check runs, so
`MAX_BODY_BYTES` would reject the result without preventing the allocation.

**`Retry-After` is clamped.** It overrides our backoff, but still cannot exceed `MAX_BACKOFF_S`
or go negative — otherwise `Retry-After: 999999` would park a feed for eleven days.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fetch.py -v`
Expected: PASS — 17 passed

- [ ] **Step 5: Commit**

```bash
git add src/rss_ticker/fetch.py tests/test_fetch.py
git commit -m "feat: conditional GET fetch and backoff policy"
```

---

### Task 10: Poller scheduler with cold-start suppression

**Files:**
- Create: `src/rss_ticker/poller.py`
- Test: `tests/test_poller.py`

**Interfaces:**
- Consumes: `Store` (Tasks 2–6), `parse_feed` (Task 8), `fetch_feed`/`next_interval` (Task 9)
- Produces:
  - `Poller(store, client, config, on_new_articles: Callable[[list[Article]], Awaitable[None]], jitter: Callable[[], float] = ...)`
  - `async Poller.poll_feed(feed: Feed, now: int) -> list[Article]`
  - `async Poller.run_once(now: int) -> int` (feeds polled)
  - `async Poller.run_forever()`

- [ ] **Step 1: Write the failing test**

`tests/test_poller.py`:

```python
import httpx
import pytest

from rss_ticker.config import Config
from rss_ticker.poller import Poller
from rss_ticker.store import Store

FEED_A = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>First</title><guid>urn:1</guid></item></channel></rss>"""

FEED_AB = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Second</title><guid>urn:2</guid></item>
<item><title>First</title><guid>urn:1</guid></item></channel></rss>"""


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
    sent = []
    fid = store.upsert_feed("https://a.example/rss", now=0)
    store.subscribe("art", fid)
    p = make_poller(store, lambda r: httpx.Response(304), sent.extend)
    await p.run_once(now=100)
    assert sent == []
    assert store.get_feed_state(fid).consecutive_failures == 0
    assert store.get_feed_state(fid).last_success_at == 100


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_poller.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rss_ticker.poller'`

- [ ] **Step 3: Write the implementation**

`src/rss_ticker/poller.py`:

```python
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable

import httpx

from . import __version__
from .config import Config
from .fetch import fetch_feed, next_interval, user_agent
from .normalize import parse_feed
from .store import Article, Feed, Store

log = logging.getLogger(__name__)

OnNewArticles = Callable[[list[Article]], Awaitable[None] | None]


def _default_jitter() -> float:
    return random.uniform(0.9, 1.1)


class Poller:
    def __init__(
        self,
        store: Store,
        client: httpx.AsyncClient,
        config: Config,
        on_new_articles: OnNewArticles,
        jitter: Callable[[], float] = _default_jitter,
    ) -> None:
        self.store = store
        self.client = client
        self.config = config
        self.on_new_articles = on_new_articles
        self.jitter = jitter
        self._ua = user_agent(__version__, config.public_base_url)

    def _base_interval(self, feed: Feed) -> int:
        return feed.poll_interval_s or self.config.default_poll_interval_s

    async def poll_feed(self, feed: Feed, now: int) -> list[Article]:
        state = self.store.get_feed_state(feed.id)
        cold_start = state is None or state.last_success_at is None
        etag = state.etag if state else None
        last_modified = state.last_modified if state else None
        failures = state.consecutive_failures if state else 0

        self.client.headers["User-Agent"] = self._ua
        outcome = await fetch_feed(self.client, feed.url, etag, last_modified)

        if outcome.status == "failed":
            delay = next_interval(self._base_interval(feed), failures + 1, outcome.retry_after)
            self.store.record_failure(
                feed.id, error=outcome.error or "unknown error", now=now,
                next_poll_at=now + int(delay * self.jitter()),
            )
            log.warning("feed %s failed: %s", feed.url, outcome.error)
            return []

        delay = int(self._base_interval(feed) * self.jitter())

        if outcome.status == "not_modified":
            self.store.record_success(
                feed.id, etag=etag, last_modified=last_modified, now=now,
                next_poll_at=now + delay,
            )
            return []

        entries, dropped = parse_feed(outcome.body or b"", now)
        if dropped:
            log.info("feed %s dropped %d unusable entries", feed.url, dropped)

        if not entries:
            self.store.record_failure(
                feed.id, error="feed produced no usable entries", now=now,
                next_poll_at=now + next_interval(
                    self._base_interval(feed), failures + 1, None
                ),
            )
            return []

        inserted = self.store.insert_articles(feed.id, entries, now)
        self.store.record_success(
            feed.id,
            etag=outcome.etag,
            last_modified=outcome.last_modified,
            now=now,
            next_poll_at=now + delay,
        )

        if cold_start:
            log.info("feed %s cold start: cached %d articles, broadcast none",
                     feed.url, len(inserted))
            return []
        return inserted

    async def run_once(self, now: int) -> int:
        feeds = self.store.due_feeds(now)
        if not feeds:
            return 0
        sem = asyncio.Semaphore(self.config.max_concurrent_polls)

        async def guarded(feed: Feed) -> list[Article]:
            async with sem:
                try:
                    return await self.poll_feed(feed, now)
                except Exception:
                    log.exception("unexpected error polling %s", feed.url)
                    return []

        results = await asyncio.gather(*(guarded(f) for f in feeds))
        new_articles = [a for batch in results for a in batch]
        if new_articles:
            maybe = self.on_new_articles(new_articles)
            if asyncio.iscoroutine(maybe):
                await maybe
        return len(feeds)

    async def run_forever(self, tick_s: float = 1.0) -> None:
        while True:
            try:
                await self.run_once(int(time.time()))
            except Exception:
                log.exception("poller tick failed")
            await asyncio.sleep(tick_s)
```

**Amendments applied during execution (commit `a85f5bd`) — the Step 3 code above predates
these and must be corrected as you transcribe it:**

1. **`on_new_articles` must be wrapped in its own `try/except Exception`** inside
   `run_once`, logging via `log.exception`. As written above it sits outside `guarded()`,
   so a broadcaster failure escapes `run_once` and breaks the containment guarantee.
   `run_once` must still return the feed count afterwards.
2. **Apply `self.jitter()` on the "no usable entries" reschedule path**, matching the
   `int(... * self.jitter())` form used by the fetch-failure branch. Without it, feeds
   that fetch fine but parse to zero entries retry in lockstep.
3. **A 304 on a feed's first-ever poll is a FAILURE, not a success.** On that first poll
   no conditional headers are sent (etag and last_modified are still NULL), so a
   compliant server cannot legitimately 304. If one does and we record success,
   `last_success_at` is set with zero articles stored, and the next poll broadcasts the
   feed's whole back catalogue. When `cold_start` is true, call `record_failure` with
   `"304 not modified on an unconditional first poll"`, apply the same backoff-with-jitter
   arithmetic, log a warning, return `[]`. The non-cold-start 304 path stays a success.
4. **Capitalise every log message** in this module (copy rule), and move
   `self.client.headers["User-Agent"] = self._ua` from `poll_feed` into `__init__`.

Three tests cover these: `test_broadcaster_error_does_not_escape_run_once`,
`test_no_usable_entries_applies_jitter`, `test_304_on_first_poll_is_treated_as_failure`.
Note that `test_304_does_not_broadcast_or_fail` must establish a prior successful poll in
its setup, since a brand-new feed receiving a 304 is now the failure case.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_poller.py -v`
Expected: PASS — 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/rss_ticker/poller.py tests/test_poller.py
git commit -m "feat: poller scheduler with cold-start suppression"
```

---

### Task 11: Broadcaster with per-user filtering and backpressure

**Files:**
- Create: `src/rss_ticker/broadcast.py`
- Test: `tests/test_broadcast.py`

**Interfaces:**
- Consumes: `Store` (Tasks 2–6), `evaluate` (Task 4), `Article` (Task 2)
- Produces:
  - `MAX_QUEUE = 1000`
  - `Subscription` with `user_id: str`, `queue: asyncio.Queue`, `dropped: bool`
  - `Broadcaster(store)` with `subscribe(user_id) -> Subscription`, `unsubscribe(sub)`, `async publish(articles: list[Article]) -> None`, `subscriber_count(user_id) -> int`
  - `article_payload(article: Article, feed_name: str | None, highlighted: bool) -> dict`

- [ ] **Step 1: Write the failing test**

`tests/test_broadcast.py`:

```python
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


async def test_unsubscribe_stops_delivery(store):
    fid = add(store, ["art"])
    b = Broadcaster(store)
    sub = b.subscribe("art")
    b.unsubscribe(sub)
    await b.publish(articles(store, fid, [("a", "Fed holds", 1)]))
    assert await drain(sub) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_broadcast.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rss_ticker.broadcast'`

- [ ] **Step 3: Write the implementation**

`src/rss_ticker/broadcast.py`:

```python
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .filters import evaluate
from .store import Article, Store, encode_cursor

log = logging.getLogger(__name__)

MAX_QUEUE = 1000


# eq=False is required, not stylistic: a plain @dataclass sets __hash__ = None, and
# Subscription instances are stored in a set. Identity semantics are also what we want —
# two subscriptions for the same user must stay distinct, which value-equality would break.
@dataclass(eq=False)
class Subscription:
    user_id: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUE))
    dropped: bool = False


def article_payload(article: Article, feed_name: str | None, highlighted: bool) -> dict:
    return {
        "id": article.id,
        "cursor": encode_cursor(article.sort_at, article.id),
        "title": article.title,
        "link": article.link,
        "summary": article.summary,
        "source": feed_name,
        "published_at": article.published_at,
        "sort_at": article.sort_at,
        "highlighted": highlighted,
    }


class Broadcaster:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._subs: dict[str, set[Subscription]] = {}

    def subscribe(self, user_id: str) -> Subscription:
        sub = Subscription(user_id=user_id)
        self._subs.setdefault(user_id, set()).add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        peers = self._subs.get(sub.user_id)
        if peers:
            peers.discard(sub)
            if not peers:
                self._subs.pop(sub.user_id, None)

    def subscriber_count(self, user_id: str) -> int:
        return len(self._subs.get(user_id, ()))

    async def publish(self, articles: list[Article]) -> None:
        if not articles or not self._subs:
            return

        feed_names: dict[int, str | None] = {}
        feed_users: dict[int, list[str]] = {}
        for article in articles:
            if article.feed_id not in feed_names:
                feed = self.store.get_feed(article.feed_id)
                feed_names[article.feed_id] = feed.name if feed else None
                feed_users[article.feed_id] = self.store.subscribers_of(article.feed_id)

        rules_cache: dict[str, list] = {}

        for article in articles:
            for user_id in feed_users[article.feed_id]:
                targets = self._subs.get(user_id)
                if not targets:
                    continue
                if user_id not in rules_cache:
                    rules_cache[user_id] = self.store.filters_for(user_id)
                included, highlighted = evaluate(
                    rules_cache[user_id], article.title, article.summary
                )
                if not included:
                    continue
                payload = article_payload(
                    article, feed_names[article.feed_id], highlighted
                )
                for sub in list(targets):
                    try:
                        sub.queue.put_nowait(payload)
                    except asyncio.QueueFull:
                        sub.dropped = True
                        self.unsubscribe(sub)
                        log.warning("Dropped slow subscriber for user %s", user_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_broadcast.py -v`
Expected: PASS — 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/rss_ticker/broadcast.py tests/test_broadcast.py
git commit -m "feat: broadcaster with per-user filtering and backpressure"
```

---

### Task 12: REST API — news, feeds, health, root

**Files:**
- Create: `src/rss_ticker/api.py`
- Test: `tests/test_api_rest.py`

**Interfaces:**
- Consumes: everything from Tasks 1–11
- Produces:
  - `create_app(config: Config, store: Store, broadcaster: Broadcaster) -> FastAPI`
  - `ALLOWED_ORIGINS: list[str]`
  - Response shape for `GET /api/news`: `{"articles": [<article_payload>], "next_cursor": str | None}`
  - **Ordering:** the handler returns `store.page_news` results in store order. That means `before` and the no-cursor default are newest-first, while `after` is oldest-first (see Task 5). Do not re-sort in the handler — the widget's prepend path sorts by `sort_at` itself, and re-sorting here would break the forward-walk cursor chain.

- [ ] **Step 1: Write the failing test**

`tests/test_api_rest.py`:

```python
import pytest
from fastapi.testclient import TestClient

from rss_ticker.api import create_app
from rss_ticker.broadcast import Broadcaster
from rss_ticker.config import Config
from rss_ticker.store import NewArticle, Store

CFG = Config(public_base_url="http://nas.local:8088", admin_key="s3cret")


@pytest.fixture
def store():
    s = Store(":memory:")
    s.upsert_user("art", "Art")
    yield s
    s.close()


@pytest.fixture
def client(store):
    return TestClient(create_app(CFG, store, Broadcaster(store)))


def seed(store, n=3):
    fid = store.upsert_feed("https://x.example/rss", name="X", now=0)
    store.subscribe("art", fid)
    store.insert_articles(
        fid,
        [NewArticle(f"g{i}", f"headline {i}", "https://l", None, 1000 + i) for i in range(n)],
        now=1000,
    )
    return fid


def test_root_returns_service_info(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "rss-ticker" in r.json()["service"]


def test_news_returns_newest_first(client, store):
    seed(store, 3)
    body = client.get("/api/news", params={"user": "art"}).json()
    assert [a["title"] for a in body["articles"]] == ["headline 2", "headline 1", "headline 0"]


def test_news_paging_uses_cursor(client, store):
    seed(store, 5)
    first = client.get("/api/news", params={"user": "art", "limit": 2}).json()
    assert first["next_cursor"]
    second = client.get(
        "/api/news", params={"user": "art", "limit": 2, "before": first["next_cursor"]}
    ).json()
    assert not ({a["id"] for a in first["articles"]} & {a["id"] for a in second["articles"]})


def test_news_requires_user(client):
    assert client.get("/api/news").status_code == 422


def test_news_unknown_user_is_400(client):
    r = client.get("/api/news", params={"user": "nobody"})
    assert r.status_code == 400
    assert "unknown user" in r.json()["detail"]


def test_news_limit_is_capped(client, store):
    seed(store, 3)
    r = client.get("/api/news", params={"user": "art", "limit": 5000})
    assert r.status_code == 422


def test_news_bad_cursor_is_400(client, store):
    seed(store, 1)
    r = client.get("/api/news", params={"user": "art", "before": "!!!"})
    assert r.status_code == 400


def test_news_marks_highlighted(client, store):
    seed(store, 1)
    store.add_filter("art", "headline", "highlight")
    body = client.get("/api/news", params={"user": "art"}).json()
    assert body["articles"][0]["highlighted"] is True


def test_list_feeds(client, store):
    seed(store)
    body = client.get("/api/feeds", params={"user": "art"}).json()
    assert body["feeds"][0]["url"] == "https://x.example/rss"


def test_post_feed_requires_admin_key(client):
    r = client.post("/api/feeds", json={"user": "art", "url": "https://n.example/rss"})
    assert r.status_code == 401


def test_post_feed_subscribes_user(client, store):
    r = client.post(
        "/api/feeds",
        json={"user": "art", "url": "https://n.example/rss", "name": "N"},
        headers={"X-Admin-Key": "s3cret"},
    )
    assert r.status_code == 201
    assert "https://n.example/rss" in [f.url for f in store.list_feeds("art")]


def test_post_feed_twice_is_idempotent(client, store):
    payload = {"user": "art", "url": "https://n.example/rss"}
    h = {"X-Admin-Key": "s3cret"}
    client.post("/api/feeds", json=payload, headers=h)
    client.post("/api/feeds", json=payload, headers=h)
    assert len(store.list_feeds("art")) == 1


def test_delete_feed_unsubscribes_only(client, store):
    fid = seed(store)
    store.upsert_user("bob", None)
    store.subscribe("bob", fid)
    r = client.delete(
        f"/api/feeds/{fid}", params={"user": "art"}, headers={"X-Admin-Key": "s3cret"}
    )
    assert r.status_code == 204
    assert store.list_feeds("art") == []
    assert [f.id for f in store.list_feeds("bob")] == [fid]


def test_delete_feed_requires_admin_key(client, store):
    fid = seed(store)
    assert client.delete(f"/api/feeds/{fid}", params={"user": "art"}).status_code == 401


def test_delete_unsubscribed_feed_is_404(client, store):
    fid = seed(store)
    store.unsubscribe("art", fid)
    r = client.delete(
        f"/api/feeds/{fid}", params={"user": "art"}, headers={"X-Admin-Key": "s3cret"}
    )
    assert r.status_code == 404


def test_health_reports_feed_status_and_published_url(client, store):
    seed(store)
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["public_base_url"] == "http://nas.local:8088"
    assert body["feeds"][0]["url"] == "https://x.example/rss"


def test_cors_allows_openbb_origin(client):
    r = client.get("/api/health", headers={"Origin": "https://pro.openbb.co"})
    assert r.headers["access-control-allow-origin"] == "https://pro.openbb.co"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_rest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rss_ticker.api'`

- [ ] **Step 3: Write the implementation**

`src/rss_ticker/api.py`:

```python
from __future__ import annotations

import time

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import __version__
from .broadcast import Broadcaster, article_payload
from .config import Config
from .filters import highlights
from .store import CursorError, Store

ALLOWED_ORIGINS = [
    "https://pro.openbb.co",
    "https://pro.openbb.dev",
    "https://excel.openbb.co",
    "http://localhost:1420",
]


class FeedCreate(BaseModel):
    user: str
    url: str
    name: str | None = None
    poll_interval_s: int | None = None


def create_app(
    config: Config,
    store: Store,
    broadcaster: Broadcaster,
    lifespan=None,
) -> FastAPI:
    app = FastAPI(title="rss-ticker", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.config = config
    app.state.store = store
    app.state.broadcaster = broadcaster

    def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
        if x_admin_key != config.admin_key:
            raise HTTPException(status_code=401, detail="admin key required")

    def require_user(user: str) -> str:
        if not store.user_exists(user):
            raise HTTPException(status_code=400, detail=f"unknown user {user}")
        return user

    @app.get("/")
    def root() -> dict:
        return {
            "service": "rss-ticker",
            "version": __version__,
            "widgets": f"{config.public_base_url}/widgets.json",
        }

    @app.get("/api/news")
    def news(
        user: str = Query(...),
        limit: int = Query(50, ge=1, le=200),
        before: str | None = Query(None),
        after: str | None = Query(None),
    ) -> dict:
        require_user(user)
        if before and after:
            raise HTTPException(status_code=400, detail="pass before or after, not both")
        try:
            articles, next_cursor = store.page_news(
                user, limit=limit, before=before, after=after
            )
        except CursorError:
            raise HTTPException(status_code=400, detail="cursor is not valid") from None

        rules = store.filters_for(user)
        names: dict[int, str | None] = {}
        payloads = []
        for article in articles:
            if article.feed_id not in names:
                feed = store.get_feed(article.feed_id)
                names[article.feed_id] = feed.name if feed else None
            highlighted = highlights(rules, article.title, article.summary)
            payloads.append(article_payload(article, names[article.feed_id], highlighted))
        return {"articles": payloads, "next_cursor": next_cursor}

    @app.get("/api/feeds")
    def list_feeds(user: str = Query(...)) -> dict:
        require_user(user)
        return {
            "feeds": [
                {
                    "id": f.id,
                    "url": f.url,
                    "name": f.name,
                    "poll_interval_s": f.poll_interval_s,
                    "enabled": f.enabled,
                }
                for f in store.list_feeds(user)
            ]
        }

    @app.post("/api/feeds", status_code=201, dependencies=[Depends(require_admin)])
    def add_feed(body: FeedCreate) -> dict:
        require_user(body.user)
        feed_id = store.upsert_feed(
            body.url,
            name=body.name,
            poll_interval_s=body.poll_interval_s,
            now=int(time.time()),
        )
        store.subscribe(body.user, feed_id)
        return {"id": feed_id, "url": body.url}

    @app.delete("/api/feeds/{feed_id}", status_code=204,
                dependencies=[Depends(require_admin)])
    def remove_feed(feed_id: int, user: str = Query(...)) -> Response:
        require_user(user)
        if not store.unsubscribe(user, feed_id):
            raise HTTPException(status_code=404, detail="user is not subscribed to that feed")
        return Response(status_code=204)

    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "public_base_url": config.public_base_url,
            "feeds": store.all_feed_status(),
        }

    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_rest.py -v`
Expected: PASS — 17 passed

- [ ] **Step 5: Commit**

```bash
git add src/rss_ticker/api.py tests/test_api_rest.py
git commit -m "feat: rest api for news, feeds, and health"
```

---

### Task 13: WebSocket endpoint

**Files:**
- Modify: `src/rss_ticker/api.py`
- Test: `tests/test_api_ws.py`

**Interfaces:**
- Consumes: `create_app` (Task 12), `Broadcaster` (Task 11)
- Produces: `WS /ws/news?user=` — server sends one JSON object per article, identical in shape to `article_payload`. Server closes with code 4400 on unknown user.

**Testing note:** `TestClient` drives the ASGI app from a worker thread, so publishing
into a subscriber's queue from the *test* thread is not reliably visible to the app's
event loop. This task therefore tests only what is deterministic through `TestClient` —
connection acceptance, rejection, and subscriber registration/deregistration lifecycle.
Actual message delivery over a real socket is covered in Task 18 against a live uvicorn
server. Do not paper over this with sleeps or cross-thread queue pokes.

- [ ] **Step 1: Write the failing test**

`tests/test_api_ws.py`:

```python
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rss_ticker.api import create_app
from rss_ticker.broadcast import Broadcaster
from rss_ticker.config import Config
from rss_ticker.store import Store

CFG = Config(public_base_url="http://x", admin_key="k")


@pytest.fixture
def store():
    s = Store(":memory:")
    s.upsert_user("art", None)
    yield s
    s.close()


@pytest.fixture
def broadcaster(store):
    return Broadcaster(store)


@pytest.fixture
def client(store, broadcaster):
    return TestClient(create_app(CFG, store, broadcaster))


def test_ws_accepts_known_user_and_registers_subscriber(client, broadcaster):
    with client.websocket_connect("/ws/news?user=art"):
        assert broadcaster.subscriber_count("art") == 1


def test_ws_disconnect_removes_subscriber(client, broadcaster):
    with client.websocket_connect("/ws/news?user=art"):
        pass
    assert broadcaster.subscriber_count("art") == 0


def test_ws_rejects_unknown_user(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/news?user=nobody") as ws:
            ws.receive_json()
    assert exc.value.code == 4400


def test_ws_rejects_missing_user(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/news") as ws:
            ws.receive_json()
    assert exc.value.code == 4400


def test_ws_unknown_user_is_never_registered(client, broadcaster):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/news?user=nobody") as ws:
            ws.receive_json()
    assert broadcaster.subscriber_count("nobody") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_ws.py -v`
Expected: FAIL — WebSocket route `/ws/news` does not exist (404 / `WebSocketDisconnect`)

- [ ] **Step 3: Add imports to `api.py`**

```python
import asyncio
from fastapi import WebSocket, WebSocketDisconnect
```

- [ ] **Step 4: Add the WebSocket route inside `create_app`, before `return app`**

```python
    @app.websocket("/ws/news")
    async def ws_news(websocket: WebSocket) -> None:
        user = websocket.query_params.get("user")
        await websocket.accept()
        if not user or not store.user_exists(user):
            await websocket.close(code=4400, reason="unknown user")
            return

        sub = broadcaster.subscribe(user)
        try:
            while True:
                payload = await sub.queue.get()
                await websocket.send_json(payload)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            pass
        finally:
            broadcaster.unsubscribe(sub)
```

**Amendments applied during execution (commits `3299b0e`, `0dc851c`):**

1. **The admin-key check moved to a module-level `admin_key_ok(provided, expected)` helper
   comparing `bytes`, not `str`.** `hmac.compare_digest` raises `TypeError` on `str`
   operands containing non-ASCII, so a raw `X-Admin-Key: \xe9` header turned a 401 into a
   500 — an auth check crashing instead of failing closed. `TestClient` cannot send such a
   header, which is why it must be tested by calling the helper directly:

   ```python
   def admin_key_ok(provided: str | None, expected: str) -> bool:
       """Constant-time admin key check that never raises on caller-controlled input."""
       if provided is None:
           return False
       return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
   ```

2. **The WebSocket handler's except clauses split three ways**, so cancellation propagates
   and unexpected errors are logged rather than silently swallowed:

   ```python
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Websocket handler failed for user %s", user)
        finally:
            broadcaster.unsubscribe(sub)
   ```

   `asyncio.CancelledError` inherits from `BaseException` in Python 3.8+; swallowing it
   hides cancellation from task groups and `wait_for`. The `finally` still runs on the
   re-raise path, so the subscriber is deregistered either way.

3. Close reason capitalised to `"Unknown user"`. The close CODE stays 4400.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_ws.py -v`
Expected: PASS — 5 passed

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest -v`
Expected: PASS — all tests from Tasks 1–13

- [ ] **Step 7: Commit**

```bash
git add src/rss_ticker/api.py tests/test_api_ws.py
git commit -m "feat: websocket endpoint for live article push"
```

---

### Task 14: widgets.json manifest

**Files:**
- Create: `src/rss_ticker/widgets.py`
- Modify: `src/rss_ticker/api.py`
- Test: `tests/test_widgets.py`

**Interfaces:**
- Consumes: `Config` (Task 1)
- Produces: `render_widgets(config: Config) -> dict` and route `GET /widgets.json`

- [ ] **Step 1: Write the failing test**

`tests/test_widgets.py`:

```python
import pytest
from fastapi.testclient import TestClient

from rss_ticker.api import create_app
from rss_ticker.broadcast import Broadcaster
from rss_ticker.config import Config, UserConfig
from rss_ticker.store import Store
from rss_ticker.widgets import render_widgets

CFG = Config(
    public_base_url="http://nas.local:8088",
    admin_key="k",
    users=(UserConfig(id="art", name="Art"),),
)


@pytest.fixture
def client():
    store = Store(":memory:")
    store.upsert_user("art", "Art")
    yield TestClient(create_app(CFG, store, Broadcaster(store)))
    store.close()


def test_manifest_is_keyed_by_widget_id_not_a_list():
    manifest = render_widgets(CFG)
    assert isinstance(manifest, dict)
    assert not isinstance(manifest, list)
    assert all(isinstance(v, dict) for v in manifest.values())


def test_every_widget_has_required_fields():
    for widget in render_widgets(CFG).values():
        assert widget["name"]
        assert widget["description"]
        assert widget["endpoint"]


def test_endpoint_is_absolute_url_for_iframe():
    for widget in render_widgets(CFG).values():
        assert widget["type"] == "iframe"
        assert widget["endpoint"].startswith("http://nas.local:8088/widget")


def test_one_widget_per_user_per_size():
    manifest = render_widgets(CFG)
    assert "news_window_art" in manifest
    assert "news_rail_art" in manifest
    assert manifest["news_window_art"]["gridData"]["h"] == 8
    assert manifest["news_rail_art"]["gridData"]["h"] == 2


def test_endpoint_carries_the_user_param():
    manifest = render_widgets(CFG)
    assert "user=art" in manifest["news_window_art"]["endpoint"]


def test_widgets_json_is_served(client):
    r = client.get("/widgets.json")
    assert r.status_code == 200
    assert "news_window_art" in r.json()


def test_no_users_yields_empty_manifest():
    empty = Config(public_base_url="http://x", admin_key="k")
    assert render_widgets(empty) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_widgets.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rss_ticker.widgets'`

- [ ] **Step 3: Write the implementation**

`src/rss_ticker/widgets.py`:

```python
from __future__ import annotations

from .config import Config

SIZES = (
    ("news_window", "News window", 8, "Live RSS headlines, newest first, with scrollback"),
    ("news_rail", "News rail", 2, "Live RSS headlines in a compact bottom rail"),
)


def render_widgets(config: Config) -> dict:
    manifest: dict[str, dict] = {}
    for user in config.users:
        for prefix, label, height, description in SIZES:
            manifest[f"{prefix}_{user.id}"] = {
                "name": f"{label} ({user.name or user.id})",
                "description": description,
                "category": "News",
                "type": "iframe",
                "endpoint": f"{config.public_base_url}/widget?user={user.id}",
                "gridData": {"w": 40, "h": height},
                "source": "RSS",
            }
    return manifest
```

- [ ] **Step 4: Add the route to `api.py`, next to `root()`**

```python
    @app.get("/widgets.json")
    def widgets_manifest() -> dict:
        from .widgets import render_widgets

        return render_widgets(config)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_widgets.py -v`
Expected: PASS — 7 passed

- [ ] **Step 6: Commit**

```bash
git add src/rss_ticker/widgets.py src/rss_ticker/api.py tests/test_widgets.py
git commit -m "feat: openbb widgets.json manifest"
```

---

### Task 15: Widget frontend

**Files:**
- Create: `src/rss_ticker/static/widget.html`
- Modify: `src/rss_ticker/api.py`
- Test: `tests/test_widget_route.py`

**Interfaces:**
- Consumes: `GET /api/news`, `WS /ws/news` (Tasks 12–13)
- Produces: route `GET /widget?user=` serving the HTML; the page is fully self-contained (no external assets, since OpenBB loads it in an iframe)

- [ ] **Step 1: Write the failing test**

`tests/test_widget_route.py`:

```python
import pytest
from fastapi.testclient import TestClient

from rss_ticker.api import create_app
from rss_ticker.broadcast import Broadcaster
from rss_ticker.config import Config
from rss_ticker.store import Store

CFG = Config(public_base_url="http://nas.local:8088", admin_key="k")


@pytest.fixture
def client():
    store = Store(":memory:")
    store.upsert_user("art", None)
    yield TestClient(create_app(CFG, store, Broadcaster(store)))
    store.close()


def test_widget_is_served_as_html(client):
    r = client.get("/widget", params={"user": "art"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_widget_has_no_external_asset_references(client):
    body = client.get("/widget", params={"user": "art"}).text
    for marker in ("http://cdn", "https://cdn", "unpkg", "jsdelivr", "googleapis"):
        assert marker not in body


def test_widget_opens_links_in_a_new_tab(client):
    body = client.get("/widget", params={"user": "art"}).text
    assert 'target="_blank"' in body
    assert "noopener" in body


def test_widget_requires_user(client):
    assert client.get("/widget").status_code == 422


def test_widget_unknown_user_is_400(client):
    assert client.get("/widget", params={"user": "nobody"}).status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_widget_route.py -v`
Expected: FAIL — 404, no `/widget` route

- [ ] **Step 3: Create the widget page**

`src/rss_ticker/static/widget.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>News ticker</title>
<style>
  :root {
    --bg: #0d0d0d; --fg: #e6e6e6; --dim: #8a8a8a; --line: #262626;
    --hl: #ffb020; --ok: #3ddc84; --bad: #e2484a;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#fff; --fg:#111; --dim:#6b6b6b; --line:#e3e3e3;
            --hl:#a35a00; --ok:#127a3d; --bad:#a32d2d; }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--bg); color: var(--fg); display: flex; flex-direction: column;
    font: 13px/1.35 ui-sans-serif, -apple-system, "Segoe UI", sans-serif;
  }
  header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 4px 8px; border-bottom: 1px solid var(--line);
    font: 11px ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--dim);
    flex: none;
  }
  #dot { display:inline-block; width:6px; height:6px; border-radius:50%;
         background: var(--bad); margin-right:5px; }
  #dot.live { background: var(--ok); }
  #list { flex: 1 1 auto; overflow-y: scroll; overflow-x: hidden; }
  a.row {
    display: flex; gap: 8px; align-items: baseline; padding: 5px 8px;
    border-bottom: 1px solid var(--line); text-decoration: none; color: inherit;
  }
  a.row:hover { background: rgba(127,127,127,.12); }
  .t { font: 11px ui-monospace, Menlo, monospace; color: var(--dim); flex: none;
       min-width: 38px; }
  .s { font: 11px ui-monospace, Menlo, monospace; color: var(--dim); flex: none;
       min-width: 40px; text-transform: uppercase; }
  .h { flex: 1 1 auto; }
  .h.hl { color: var(--hl); }
  #pill {
    position: sticky; top: 0; z-index: 2; display: none; text-align: center;
    padding: 3px; font-size: 11px; cursor: pointer;
    background: var(--hl); color: var(--bg);
  }
  #empty { padding: 10px 8px; color: var(--dim); }
</style>
</head>
<body>
<header>
  <span><span id="dot"></span><span id="state">connecting</span></span>
  <span id="count"></span>
</header>
<div id="list">
  <div id="pill"></div>
  <div id="empty">loading headlines</div>
</div>
<script>
(function () {
  var params = new URLSearchParams(location.search);
  var user = params.get("user") || "";
  var list = document.getElementById("list");
  var pill = document.getElementById("pill");
  var empty = document.getElementById("empty");
  var dot = document.getElementById("dot");
  var state = document.getElementById("state");
  var count = document.getElementById("count");

  var newestCursor = null, newestKey = null, oldestCursor = null, pending = 0;
  var loading = false, exhausted = false, ws = null, backoff = 1000;
  var seen = new Set();

  function noteNewest(a) {
    var key = [a.sort_at, a.id];
    if (!newestKey || key[0] > newestKey[0] ||
        (key[0] === newestKey[0] && key[1] > newestKey[1])) {
      newestKey = key;
      newestCursor = a.cursor;
    }
  }

  function fmt(ts) {
    var d = new Date(ts * 1000);
    return String(d.getHours()).padStart(2, "0") + ":" +
           String(d.getMinutes()).padStart(2, "0");
  }

  function row(a) {
    var el = document.createElement("a");
    el.className = "row";
    el.href = a.link || "#";
    el.target = "_blank";
    el.rel = "noopener noreferrer";
    el.title = new Date(a.sort_at * 1000).toString();
    var t = document.createElement("span"); t.className = "t";
    t.textContent = fmt(a.sort_at);
    var s = document.createElement("span"); s.className = "s";
    s.textContent = a.source || "";
    var h = document.createElement("span");
    h.className = "h" + (a.highlighted ? " hl" : "");
    h.textContent = a.title;
    el.appendChild(t); el.appendChild(s); el.appendChild(h);
    return el;
  }

  function atTop() { return list.scrollTop <= 2; }

  function prepend(items) {
    var fresh = items.filter(function (a) { return !seen.has(a.id); });
    if (!fresh.length) return;
    fresh.forEach(function (a) { seen.add(a.id); });
    empty.style.display = "none";
    var wasAtTop = atTop();
    var before = list.scrollHeight;
    var frag = document.createDocumentFragment();
    fresh.sort(function (a, b) { return a.sort_at - b.sort_at; });
    fresh.forEach(function (a) {
      noteNewest(a);
      frag.insertBefore(row(a), frag.firstChild);
    });
    pill.parentNode.insertBefore(frag, pill.nextSibling);
    if (wasAtTop) {
      list.scrollTop = 0;
    } else {
      list.scrollTop += list.scrollHeight - before;
      pending += fresh.length;
      pill.textContent = pending + " new " +
        (pending === 1 ? "headline" : "headlines") + " ↑";
      pill.style.display = "block";
    }
  }

  function append(items) {
    if (!items.length) return;
    empty.style.display = "none";
    items.forEach(function (a) {
      if (seen.has(a.id)) return;
      seen.add(a.id);
      noteNewest(a);
      list.appendChild(row(a));
    });
  }

  pill.addEventListener("click", function () {
    list.scrollTop = 0; pending = 0; pill.style.display = "none";
  });
  list.addEventListener("scroll", function () {
    if (atTop()) { pending = 0; pill.style.display = "none"; }
    if (list.scrollTop + list.clientHeight >= list.scrollHeight - 40) loadOlder();
  });

  function api(qs) { return fetch("/api/news?" + qs).then(function (r) {
    if (!r.ok) throw new Error("http " + r.status); return r.json(); }); }

  function loadOlder() {
    if (loading || exhausted) return;
    loading = true;
    var qs = "user=" + encodeURIComponent(user) + "&limit=50" +
             (oldestCursor ? "&before=" + encodeURIComponent(oldestCursor) : "");
    api(qs).then(function (body) {
      append(body.articles);
      if (body.articles.length) {
        oldestCursor = body.articles[body.articles.length - 1].cursor;
      }
      if (!body.next_cursor) exhausted = true;
      if (!seen.size) empty.textContent = "no headlines yet";
      loading = false;
    }).catch(function () { loading = false; });
  }

  function fillGap() {
    if (!newestCursor) return;
    api("user=" + encodeURIComponent(user) + "&limit=200&after=" +
        encodeURIComponent(newestCursor)).then(function (body) {
      prepend(body.articles);
    }).catch(function () {});
  }

  function setLive(on, label) {
    dot.className = on ? "live" : "";
    state.textContent = label;
  }

  function connect() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws/news?user=" +
                       encodeURIComponent(user));
    ws.onopen = function () { backoff = 1000; setLive(true, "live"); fillGap(); };
    ws.onmessage = function (ev) { prepend([JSON.parse(ev.data)]); };
    ws.onclose = function () {
      setLive(false, "reconnecting");
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 30000);
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  fetch("/api/feeds?user=" + encodeURIComponent(user))
    .then(function (r) { return r.json(); })
    .then(function (b) { count.textContent = b.feeds.length + " feeds"; })
    .catch(function () {});

  loadOlder();
  connect();
})();
</script>
</body>
</html>
```

- [ ] **Step 4: Add the route to `api.py`**

Add near the top of the file:

```python
from pathlib import Path
from fastapi.responses import HTMLResponse

STATIC = Path(__file__).parent / "static"
```

Add inside `create_app`, before `return app`:

```python
    @app.get("/widget", response_class=HTMLResponse)
    def widget(user: str = Query(...)) -> HTMLResponse:
        require_user(user)
        return HTMLResponse((STATIC / "widget.html").read_text())
```

- [ ] **Step 5: Confirm the static directory ships with the wheel**

**Do NOT add a `force-include` entry.** An earlier draft of this plan did, and applied
verbatim it breaks `uv build --wheel` with "second file added at same path": hatchling's
existing `packages = ["src/rss_ticker"]` already ships the whole tree, non-`.py` files
included, so force-include duplicates a path hatchling covers.

Verify instead:

```bash
uv build --wheel
unzip -l dist/*.whl | grep widget.html
```

Expected: `rss_ticker/static/widget.html` is listed. If it is missing — only then — add
the force-include block and re-check.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_widget_route.py -v`
Expected: PASS — 5 passed

- [ ] **Step 7: Commit**

```bash
git add src/rss_ticker/static src/rss_ticker/api.py pyproject.toml tests/test_widget_route.py
git commit -m "feat: bloomberg-style news window widget"
```

---

### Task 16: Application entrypoint

**Files:**
- Create: `src/rss_ticker/main.py`
- Create: `config.example.yaml`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: everything from Tasks 1–15
- Produces: `build(config_path: Path, db_path: str, env: Mapping[str, str] | None = None) -> FastAPI` with a `lifespan` context manager owning the poller and sweeper tasks; `main()` console entry

**Note on the tests below:** entering `TestClient(app)` as a context manager runs the
lifespan, so the poller starts and immediately tries to fetch `https://a.example/rss`.
`.example` is a reserved TLD, so DNS fails fast and the feed is simply recorded as
failed — the assertions do not depend on the fetch succeeding. If these tests are ever
slow, that failing lookup is why; do not "fix" it by disabling the poller in `build`.

- [ ] **Step 1: Write the failing test**

`tests/test_main.py`:

```python
from pathlib import Path

from fastapi.testclient import TestClient

from rss_ticker.main import build

CONFIG = """
public_base_url: http://nas.local:8088
admin_key: test-key
retention_days: 2
users:
  - id: art
    name: Art
    feeds:
      - {url: "https://a.example/rss", name: A}
    filters:
      - {pattern: nvidia, action: highlight}
"""


def test_build_reconciles_config_and_serves(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    app = build(cfg, str(tmp_path / "t.db"), env={})
    with TestClient(app) as client:
        assert client.get("/api/health").json()["feeds"][0]["url"] == "https://a.example/rss"
        assert "news_window_art" in client.get("/widgets.json").json()
        assert client.get("/api/news", params={"user": "art"}).json()["articles"] == []


def test_database_persists_across_builds(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    db = str(tmp_path / "t.db")
    app1 = build(cfg, db, env={})
    with TestClient(app1) as c1:
        c1.post(
            "/api/feeds",
            json={"user": "art", "url": "https://runtime.example/rss"},
            headers={"X-Admin-Key": "test-key"},
        )
    app2 = build(cfg, db, env={})
    with TestClient(app2) as c2:
        urls = [f["url"] for f in c2.get("/api/feeds", params={"user": "art"}).json()["feeds"]]
    assert "https://runtime.example/rss" in urls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rss_ticker.main'`

- [ ] **Step 3: Write the implementation**

`src/rss_ticker/main.py`:

```python
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path
from typing import Mapping

import httpx
import uvicorn
from fastapi import FastAPI

from .api import create_app
from .broadcast import Broadcaster
from .config import load_config
from .fetch import TIMEOUT_S
from .poller import Poller
from .reconcile import reconcile
from .store import Store

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 3600
VACUUM_EVERY_SWEEPS = 168


def build(config_path: Path, db_path: str, env: Mapping[str, str] | None = None) -> FastAPI:
    config = load_config(config_path, os.environ if env is None else env)
    store = Store(db_path)
    reconcile(store, config, now=int(time.time()))
    broadcaster = Broadcaster(store)

    async def sweeper() -> None:
        sweeps = 0
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            deleted = store.sweep(int(time.time()), config.retention_days)
            sweeps += 1
            log.info("retention sweep removed %d articles", deleted)
            if sweeps % VACUUM_EVERY_SWEEPS == 0:
                store.vacuum()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        client = httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True)
        poller = Poller(store, client, config, on_new_articles=broadcaster.publish)
        tasks = [
            asyncio.create_task(poller.run_forever()),
            asyncio.create_task(sweeper()),
        ]
        log.info("serving widgets at %s/widgets.json", config.public_base_url)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await client.aclose()
            store.close()

    return create_app(config, store, broadcaster, lifespan=lifespan)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = build(
        Path(os.environ.get("CONFIG_PATH", "/config/config.yaml")),
        os.environ.get("DB_PATH", "/data/ticker.db"),
    )
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8088")))


if __name__ == "__main__":
    main()
```

**Amendments applied during execution (commit `f4db2d6`) — correct these as you transcribe:**

1. **`sweeper` needs the same `try/except Exception` guard per iteration that
   `run_forever` has**, logging via `log.exception`. Without it the task dies silently on
   any sqlite error.
2. **The lifespan `finally` must cancel every task first, then
   `await asyncio.gather(*tasks, return_exceptions=True)`** — not cancel-and-await one at
   a time under `suppress(CancelledError)`. A task that already died with a *different*
   exception re-raises it on `await`, which `suppress` does not catch, aborting the loop
   so `client.aclose()` and `store.close()` never run. `gather(return_exceptions=True)`
   never raises, so cleanup is unconditional.
3. **`store.sweep` and `store.vacuum` must run via `asyncio.to_thread`.** They are
   synchronous SQLite calls on the single event loop shared with the poller and every
   WebSocket send; `VACUUM` can block for seconds. Safe because `Store` opens its
   connection with `check_same_thread=False`.
4. **Wrap `reconcile` so a startup failure closes the store**:
   `store = Store(db_path)` then `try: reconcile(...) except Exception: store.close(); raise`.
5. Capitalise both log messages ("Retention sweep removed...", "Serving widgets at...").

- [ ] **Step 4: Add the console script to `pyproject.toml`**

```toml
[project.scripts]
rss-ticker = "rss_ticker.main:main"
```

- [ ] **Step 5: Create `config.example.yaml`**

```yaml
public_base_url: http://nas.local:8088
admin_key: ${TICKER_ADMIN_KEY}
retention_days: 7
default_poll_interval_s: 300
max_concurrent_polls: 8

users:
  - id: art
    name: Art
    feeds:
      - {url: "https://feeds.reuters.com/reuters/businessNews", name: Reuters Business}
      - {url: "https://www.ft.com/rss/home", name: FT, poll_interval_s: 600}
    filters:
      - {pattern: nvidia, action: highlight}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_main.py -v`
Expected: PASS — 2 passed

- [ ] **Step 7: Commit**

```bash
git add src/rss_ticker/main.py config.example.yaml pyproject.toml tests/test_main.py
git commit -m "feat: application entrypoint with poller and sweeper tasks"
```

---

### Task 17: Docker image and multi-arch build

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.dockerignore`
- Create: `Makefile`
- Modify: `README.md`

**Interfaces:**
- Consumes: `rss-ticker` console script (Task 16)
- Produces: image `ghcr.io/artcashin/rss-ticker`, ports `8088`, volume `/data`, config mount `/config/config.yaml`

- [ ] **Step 1: Create `.dockerignore`**

```
.venv/
__pycache__/
.pytest_cache/
.git/
data/
config.yaml
docs/
tests/
```

**Known build-time dependency:** `feedparser` pulls `sgmllib3k`, which publishes an
sdist only — no wheel for any architecture. It is pure Python, so buildx compiles it
without a toolchain on both platforms, but the image build does need PyPI reachable.
If a no-network build is ever required, vendor a wheel for it rather than dropping
`feedparser`.

- [ ] **Step 2: Create the `Dockerfile`**

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CONFIG_PATH=/config/config.yaml \
    DB_PATH=/data/ticker.db \
    PORT=8088

RUN useradd --create-home --uid 10001 ticker \
    && mkdir -p /data /config \
    && chown -R ticker:ticker /data /config

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

USER ticker
EXPOSE 8088
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8088/api/health', timeout=4).status==200 else 1)"

CMD ["rss-ticker"]
```

- [ ] **Step 3: Create `docker-compose.yml`**

```yaml
services:
  ticker:
    image: ghcr.io/artcashin/rss-ticker:latest
    build: .
    restart: unless-stopped
    ports:
      - "8088:8088"
    environment:
      TICKER_ADMIN_KEY: ${TICKER_ADMIN_KEY:?set TICKER_ADMIN_KEY}
      LOG_LEVEL: INFO
    volumes:
      - ticker-data:/data
      - ./config.yaml:/config/config.yaml:ro

volumes:
  ticker-data:
```

- [ ] **Step 4: Create the `Makefile`**

```makefile
IMAGE ?= ghcr.io/artcashin/rss-ticker
TAG   ?= 0.1.0

.PHONY: test lint build buildx run

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

build:
	docker build -t $(IMAGE):$(TAG) -t $(IMAGE):latest .

buildx:
	docker buildx build --platform linux/amd64,linux/arm64 \
	  -t $(IMAGE):$(TAG) -t $(IMAGE):latest --push .

run:
	docker compose up -d --build
```

- [ ] **Step 5: Build and verify the image runs**

```bash
cd ~/Developer/rss-ticker
cp config.example.yaml config.yaml
sed -i '' 's|http://nas.local:8088|http://localhost:8088|' config.yaml
TICKER_ADMIN_KEY=devkey docker compose up -d --build
sleep 5
curl -s localhost:8088/api/health | head -c 300
curl -s localhost:8088/widgets.json | head -c 300
```

Expected: health JSON with `"status": "ok"` and `"public_base_url": "http://localhost:8088"`; manifest containing `news_window_art`.

- [ ] **Step 6: Verify the container is not running as root**

```bash
docker compose exec ticker id -u
```

Expected: `10001`

- [ ] **Step 7: Verify the multi-arch build**

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t rss-ticker:multiarch-test .
```

Expected: build succeeds for both platforms. (Omit `--push`; this only proves it builds.)

- [ ] **Step 8: Tear down and update the README**

```bash
docker compose down
```

Replace `README.md` with:

```markdown
# rss-ticker

Real-time RSS news ticker server with an OpenBB Workspace widget.

Polls RSS feeds, pushes new articles over WebSocket, caches history in SQLite for
cursor-paged scrollback, and serves a Bloomberg-style news window that OpenBB
Workspace embeds as an iframe widget.

## Quick start

    cp config.example.yaml config.yaml   # edit feeds and public_base_url
    export TICKER_ADMIN_KEY=$(openssl rand -hex 16)
    docker compose up -d

`public_base_url` must be the URL OpenBB Workspace can reach this server at. It is
baked into `widgets.json` at startup; if it is wrong the widget renders a blank frame.
`GET /api/health` echoes the value actually in use.

## OpenBB setup

Add a custom backend in OpenBB Workspace pointing at `<public_base_url>`, then drop the
"News window" or "News rail" widget onto a dashboard.

## Endpoints

| Path | Purpose |
|---|---|
| `GET /widgets.json` | OpenBB manifest |
| `GET /widget?user=` | Ticker UI (iframe target) |
| `GET /api/news?user=&limit=&before=&after=` | Cursor-paged headlines |
| `WS /ws/news?user=` | Live push |
| `GET/POST/DELETE /api/feeds` | Subscriptions (writes need `X-Admin-Key`) |
| `GET /api/health` | Liveness and per-feed poll status |

## Development

    uv venv --python 3.12 && uv pip install -e ".[dev]"
    make test
    make lint

Docs: [design spec](docs/superpowers/specs/2026-07-21-rss-news-ticker-design.md) ·
[implementation plan](docs/superpowers/plans/2026-07-21-rss-news-ticker.md)
```

- [ ] **Step 9: Commit**

```bash
git add Dockerfile docker-compose.yml .dockerignore Makefile README.md
git commit -m "feat: multi-arch docker image and compose setup"
```

---

### Task 18: End-to-end integration test

**Files:**
- Create: `tests/test_integration.py`
- Test: same file

**Interfaces:**
- Consumes: `build` (Task 16), all prior modules
- Produces: nothing consumed downstream

This task proves acceptance criteria 1, 2, 3, and 4 from the spec against the real wiring
rather than against mocks of it.

- [ ] **Step 1: Write the failing test**

`tests/test_integration.py`:

```python
import asyncio
import time
from pathlib import Path

import httpx
import pytest

from rss_ticker.broadcast import Broadcaster
from rss_ticker.config import load_config
from rss_ticker.poller import Poller
from rss_ticker.reconcile import reconcile
from rss_ticker.store import Store

CONFIG = """
public_base_url: http://localhost:8088
admin_key: k
retention_days: 7
default_poll_interval_s: 1
users:
  - id: art
    feeds:
      - {url: "https://live.example/rss", name: Live}
      - {url: "https://dead.example/rss", name: Dead}
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
    rows, _ = reopened.page_news("art", limit=10)
    assert len(rows) == 2
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
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `uv run pytest tests/test_integration.py -v`
Expected: PASS if Tasks 1–16 are correct. Any failure here is a real integration defect —
debug the module at fault rather than weakening the assertion.

- [ ] **Step 3: Write the live-server WebSocket test**

This is the only test that exercises a real socket over a real server, and it is what
actually proves acceptance criterion 1. `websockets` is already installed as a
dependency of `uvicorn[standard]` — no new package is needed.

`tests/test_ws_live.py`:

```python
import asyncio
import json
import socket
import threading
from pathlib import Path

import pytest
import uvicorn
import websockets
from fastapi.responses import Response

from rss_ticker.main import build

ROUND1 = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Backfilled</title><guid>urn:1</guid></item></channel></rss>"""

ROUND2 = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Breaking now</title><guid>urn:2</guid></item>
<item><title>Backfilled</title><guid>urn:1</guid></item></channel></rss>"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path: Path):
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
public_base_url: {base}
admin_key: k
default_poll_interval_s: 1
users:
  - id: art
    feeds:
      - {{url: "{base}/fixture.xml", name: Fixture}}
"""
    )
    app = build(cfg, str(tmp_path / "t.db"), env={})
    served = {"n": 0}

    @app.get("/fixture.xml")
    def fixture() -> Response:
        served["n"] += 1
        body = ROUND1 if served["n"] == 1 else ROUND2
        return Response(content=body, media_type="application/rss+xml")

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        threading.Event().wait(0.1)
    assert server.started, "uvicorn did not start"
    yield base, port
    server.should_exit = True
    thread.join(timeout=10)


async def test_new_article_reaches_a_real_websocket_client(live_server):
    base, port = live_server
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/news?user=art") as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
    msg = json.loads(raw)
    assert msg["title"] == "Breaking now"
    assert msg["source"] == "Fixture"
    assert msg["highlighted"] is False


async def test_backfilled_article_is_pageable_but_was_not_pushed(live_server):
    base, port = live_server
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/news?user=art") as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
    assert json.loads(raw)["title"] == "Breaking now"

    import httpx

    async with httpx.AsyncClient(base_url=base) as client:
        body = (await client.get("/api/news", params={"user": "art"})).json()
    titles = [a["title"] for a in body["articles"]]
    assert "Backfilled" in titles
    assert titles[0] == "Breaking now"
```

- [ ] **Step 4: Run the live-server test**

Run: `uv run pytest tests/test_ws_live.py -v`
Expected: PASS — 2 passed. The first poll cold-starts (caches `Backfilled`, pushes
nothing); roughly a second later the second poll finds `Breaking now` and pushes it.

If this hangs to the 15s timeout, the fault is real: check that the poller task actually
started (`/api/health` shows a `last_success_at`), and that `Broadcaster.publish` is
wired as the poller's `on_new_articles`.

- [ ] **Step 5: Run the full suite and the linter**

```bash
uv run pytest -q
uv run ruff check src tests
```

Expected: all tests pass; ruff reports no errors.

- [ ] **Step 6: Commit**

```bash
git add tests/test_integration.py tests/test_ws_live.py
git commit -m "test: end-to-end integration covering acceptance criteria"
```

---

## Manual verification (acceptance criterion 5)

Not automatable — requires OpenBB Workspace.

- [ ] Start the container with `public_base_url` set to a URL Workspace can reach.
- [ ] In OpenBB Workspace, add a custom backend pointing at that URL.
- [ ] Confirm "News window" and "News rail" appear in the widget list.
- [ ] Drop the news window on a dashboard; confirm headlines render and the connection
      dot is green.
- [ ] Resize the widget; confirm the row count changes and the list still scrolls.
- [ ] Confirm clicking a headline opens the article in a new tab, not inside the widget.
- [ ] Record findings against the four "Unverified" items in the spec and update that
      section with what was observed.
