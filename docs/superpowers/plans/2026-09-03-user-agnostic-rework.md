# User-Agnostic Rework — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn rss-ticker into a user-agnostic shared feed pool: no users, no keys, subscription as the websocket's first frame, in-memory subscriber counts that start and stop polling, feeds dropped at zero by the hourly sweep, one shared wire tagged by `feed_id`, and the exact frames bdobb-v2's News widget already speaks.

**Architecture:** The poller/store/broadcast/api seams stay. `store` loses three tables and gains canonical URLs, `enabled` as a count mirror, an `author` column and feed dropping. `broadcast` keeps a set of feed ids per socket and the per-feed counts, and fans every article out to every socket. `api` shrinks to four routes and one websocket whose first frame subscribes. `config` shrinks to four operational keys and rejects anything else. `widgets`, `static`, `filters` and `reconcile` are deleted.

**Tech Stack:** Python 3.12, FastAPI + uvicorn, httpx, feedparser, stdlib sqlite3; pytest (`asyncio_mode = auto`), ruff (`E4,E7,E9,F`), uv. Docker `python:3.12-slim`, multi-arch via `make buildx`.

**Spec:** `docs/superpowers/specs/2026-09-01-user-agnostic-rework-design.md`, including its 2026-09-03 addendum (the wire contract and decisions A–K). The base design `2026-07-21-rss-news-ticker-design.md` still governs the poller, store internals, broadcast backpressure and packaging.

## Global Constraints

Every task's requirements implicitly include this section.

- **No users, no keys, every endpoint open.** No `admin_key`, `manifest_key`, token, `tailscale_auth`, `X-Admin-Key`, `X-API-KEY`, `?user=`, `?token=`, Bearer handling, decoy comparison, 401/4401/422 auth matrix, or URL redaction on any API response. `grep -rn 'admin_key\|manifest_key\|tailscale\|token' src/` must print nothing after Task 2.
- **Wire contract, verbatim from the addendum:** subscribe frame `{"subscribe": [{"url", "name"?}]}` (http/https only, ≤ 2048 chars, ≤ 200 entries; invalid → close **4400**); reply `{"feeds": [{id, url, title, favicon}]}` in frame order, one per canonical URL; article `{id, feed_id, cursor, title, link, summary, source, author, published_at, sort_at}`; `GET /api/news?limit=&before=&after=`; `GET /api/feeds` → `{feeds: [{id, url, title, favicon, subscribers, enabled}]}`; `GET /api/health` → `{status, version, feeds}`; `GET /` → `{service, version}`.
- **Canonical URL:** scheme and host lowercased, one trailing slash stripped, nothing else. `feeds.url` stores it.
- **Subscriber counts are in-memory in `Broadcaster`;** `feeds.enabled` mirrors count > 0; every feed starts disabled at boot; the hourly sweep drops feeds still disabled (feed + feed_state + articles).
- **Config accepts exactly** `retention_days`, `default_poll_interval_s`, `max_concurrent_polls`, `bind_host`; any other top-level key is a `ConfigError` naming it. `${ENV}` expansion stays but nothing requires it.
- **Broadcast is a plain fan-out** of every inserted article to every open socket; filtering is the client's.
- **Version 8.0.0**, image `ghcr.io/artcashin/rss-feedhandler`, User-Agent `rss-ticker/<version> (+https://github.com/artcashin/rss-feedhandler)`, uvicorn access log on.
- **CORS:** `http://localhost:1420`, `http://localhost:4173`, `tauri://localhost`, `http://tauri.localhost`; `allow_credentials=False`.
- **Scrub gate:** no tailnet names, NAS paths or private hosts in any file (`bash scripts/scrub-check.sh`).
- **Every task ends green:** `uv run pytest -q` passes, `uv run ruff check src tests` is clean, `bash scripts/scrub-check.sh` passes, and the task is committed. Commit messages end with a blank line then `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- **Work in the worktree** `/Users/artcashin/Developer/rss-feedhandler/.worktrees/user-agnostic` (branch `user-agnostic-rework`); its `.venv` is installed (`uv pip install -e ".[dev]"` done). Never touch the main checkout.

## Baselines (measured 2026-09-03 at `4ed524b`)

- `uv run pytest -q` → `424 passed, 1 warning`.
- `uv run ruff check src tests` → `All checks passed!`.
- `bash scripts/scrub-check.sh` → `Scrub check passed.`

The code below was written by reading the tree, not by running it; each task's test run is the verification. Where a block does not compile, fix it in place keeping the behaviour the tests assert, and say so in the report.

## Facts about the tree the tasks rely on

- `src/rss_ticker/store.py`: `SCHEMA` creates `users`, `feeds(id,url UNIQUE,name,poll_interval_s,enabled,favicon,"group",title_format)`, `subscriptions`, `articles(…,last_seen_at)`, `feed_state`, `filter_rules`; `_migrate` adds columns idempotently; every public method is `@_synchronized` under an `RLock`; `insert_articles` uses `INSERT … ON CONFLICT DO NOTHING RETURNING *` and clamps `sort_at`; `page_news(user_id, limit, before, after)` joins `subscriptions` and applies include filters; `sweep(now, retention_days)` deletes by `last_seen_at`; `due_feeds` selects `enabled = 1`.
- `src/rss_ticker/normalize.py`: `normalize_entry(entry, now, title_format)`, `_entry_field(entry, name)` already resolves `author`/`dc:creator`; `parse_feed(body, now, title_format)`.
- `src/rss_ticker/broadcast.py`: `Subscription(user_id, queue(maxsize=MAX_QUEUE), dropped, closed)`, `article_payload(article, feed_name, highlighted)`, `Broadcaster(store)` with `subscribe(user_id)`, `unsubscribe(sub)`, `subscriber_count(user_id)`, `publish(articles)` (per-user filtering, drop on `QueueFull`).
- `src/rss_ticker/api.py`: `create_app(config, store, broadcaster, lifespan=None, health_strict=False)`; the websocket handler races `receiver`/`sender`/`closer` tasks and treats `message["type"] == "websocket.disconnect"` as the end; `DEGRADED_AFTER_FAILURES = 3`; the health route's 503 under `health_strict`.
- `src/rss_ticker/main.py`: `build(config_path, db_path, env=None)` → app with `app.state.bind_host`; `lifespan` creates the `httpx.AsyncClient`, `Poller`, `sweeper()` (every `SWEEP_INTERVAL_S = 3600`), `refresh_favicons`; `main()` runs uvicorn with `access_log=False`.
- `src/rss_ticker/poller.py`: `Poller(store, client, config, on_new_articles, jitter)`; `self._ua = user_agent(__version__, config.public_base_url)`; `parse_feed(outcome.body, now, feed.title_format)`.
- `src/rss_ticker/favicon.py`: `resolve_favicon(client, feed_url) -> str | None` (per-host resolution, three syndication hosts remapped — keep exactly), `refresh_favicons(store, client, *, concurrency)`.
- `src/rss_ticker/fetch.py`: `user_agent(version, base_url)`, `redact_feed_url` (still used by log lines — keep).
- Tests: `tests/test_ws_live.py` boots a real uvicorn on a free port from `build()` with a fixture feed route and drives `websockets.connect`; `tests/test_api_ws.py` uses `TestClient.websocket_connect`; `tests/test_migration.py` builds an old-shape SQLite file by hand and opens it through `build()`; `tests/fixtures/` has `simple.xml`, `no_guid.xml`, `malformed_with_entries.xml`, `substack.xml`; `tests/harness/` and `tests/test_widget_js.py` need Node — both go.
- `.github/workflows/ci.yml` has `scrub`, `test` (with a Node setup step for the widget JS suite) and `docker-build` jobs.

---

### Task 1: `author` on the wire, and the new User-Agent

Additive: nothing existing breaks.

**Files:**
- Modify: `src/rss_ticker/store.py` (`SCHEMA` articles, `_migrate`, `Article`, `NewArticle`, `_article`, `insert_articles`)
- Modify: `src/rss_ticker/normalize.py` (`normalize_entry`)
- Modify: `src/rss_ticker/broadcast.py` (`article_payload`)
- Modify: `src/rss_ticker/fetch.py` (`user_agent`), `src/rss_ticker/poller.py` (its call)
- Test: `tests/test_normalize.py`, `tests/test_store_articles.py`, `tests/test_broadcast.py`, `tests/test_fetch.py`, `tests/test_migration.py`

**Interfaces:**
- Produces: `NewArticle(guid, title, link, summary, published_at, author=None)`, `Article.author: str | None`, `article_payload(...)["author"]`, `user_agent(version) -> str`.

- [ ] **Step 1: Failing tests**

`tests/test_normalize.py`, append:

```python
def test_author_is_captured_from_author_and_dc_creator():
    entries, _ = parse_feed((FIX / "substack.xml").read_bytes(), now=999)
    # substack.xml carries <dc:creator>; feedparser folds it to `author`.
    assert entries[0].author
    entries, _ = parse_feed((FIX / "simple.xml").read_bytes(), now=999)
    assert entries[0].author is None
```

(Open `tests/fixtures/substack.xml` first; if its entries carry no creator, add `<dc:creator>Bob Pisani</dc:creator>` to its first item with the `xmlns:dc="http://purl.org/dc/elements/1.1/"` declaration on the root, and assert `== "Bob Pisani"`.)

`tests/test_store_articles.py`, append (use the file's existing `store` fixture and seeding style):

```python
def test_author_round_trips_and_may_be_null(store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    rows = store.insert_articles(
        fid,
        [
            NewArticle("a", "With byline", None, None, 1, author="Jane Doe"),
            NewArticle("b", "Without", None, None, 2),
        ],
        now=1000,
    )
    by_title = {r.title: r for r in rows}
    assert by_title["With byline"].author == "Jane Doe"
    assert by_title["Without"].author is None
```

`tests/test_broadcast.py`, in `test_payload_carries_feed_name_and_cursor` add `assert msg["author"] is None`, and add:

```python
async def test_payload_carries_author(store):
    fid = add(store, ["art"])
    b = Broadcaster(store)
    sub = b.subscribe("art")
    await b.publish(
        store.insert_articles(fid, [NewArticle("a", "T", None, None, 1, author="Jane")], now=1000)
    )
    assert (await drain(sub))[0]["author"] == "Jane"
```

`tests/test_fetch.py`: change the `user_agent` test(s) to `user_agent("9.9.9") == "rss-ticker/9.9.9 (+https://github.com/artcashin/rss-feedhandler)"`.

`tests/test_migration.py`: in the old-database test, after opening through `build()`, assert `"author" in {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(articles)")}`.

- [ ] **Step 2: Run, expect failure**

`uv run pytest -q tests/test_normalize.py tests/test_store_articles.py tests/test_broadcast.py tests/test_fetch.py` → `author` unexpected keyword / missing key.

- [ ] **Step 3: Implement**

`store.py`: in `SCHEMA` add `author TEXT,` after `summary TEXT,` in `articles`; in `_migrate`, after the `last_seen_at` block:

```python
        if "author" not in columns:
            # Existing articles predate the byline. NULL reads as "no author",
            # which is what the wire sends for an entry without one.
            self.db.execute("ALTER TABLE articles ADD COLUMN author TEXT")
```

Add `author: str | None` to `Article` (after `summary`) and `author: str | None = None` to `NewArticle` (last); `_article` reads `row["author"]`; `insert_articles` binds `e.author` after `e.summary` and the INSERT lists `author` after `summary` with one more `?`.

`normalize.py`: in `normalize_entry`, `author = _entry_field(entry, "author")` and pass `author=author` to `NewArticle`.

`broadcast.py`: `article_payload` gains `"author": article.author,` after `"source"`.

`fetch.py`: `def user_agent(version: str) -> str: return f"rss-ticker/{version} (+https://github.com/artcashin/rss-feedhandler)"`. `poller.py`: `self._ua = user_agent(__version__)`.

- [ ] **Step 4: Run green, lint, commit**

`uv run pytest -q && uv run ruff check src tests` → all green. `git add -A src tests && git commit -m "feat: author on every article, and a repository User-Agent"`.

---

### Task 2: The rework — no users, no keys, first-frame subscribe

One task because the user model runs through every module; the suite cannot be green in between.

**Files:**
- Rewrite: `src/rss_ticker/config.py`, `src/rss_ticker/broadcast.py`, `src/rss_ticker/api.py`, `src/rss_ticker/main.py`
- Modify: `src/rss_ticker/store.py` (schema, migration, feed methods, `page_news`, new methods), `src/rss_ticker/normalize.py` (drop `title_format`), `src/rss_ticker/poller.py` (drop `title_format`, keep everything else)
- Delete: `src/rss_ticker/widgets.py`, `src/rss_ticker/static/`, `src/rss_ticker/filters.py`, `src/rss_ticker/reconcile.py`, `tests/test_widgets.py`, `tests/test_widget_js.py`, `tests/test_widget_route.py`, `tests/test_filters.py`, `tests/test_reconcile.py`, `tests/harness/`
- Rewrite tests: `tests/test_config.py`, `tests/test_broadcast.py`, `tests/test_api_rest.py`, `tests/test_api_ws.py`, `tests/test_ws_live.py`, `tests/test_integration.py`, `tests/test_main.py`, `tests/test_migration.py`
- Adapt tests: `tests/test_store_entities.py`, `tests/test_store_paging.py`, `tests/test_store_retention.py`, `tests/test_store_articles.py`, `tests/test_store_concurrency.py`, `tests/test_poller.py`, `tests/test_favicon.py`, `tests/test_normalize.py`

**Interfaces produced (later tasks and the client rely on these exactly):**

```python
# config.py
class Config: retention_days=7; default_poll_interval_s=300; max_concurrent_polls=8; bind_host="0.0.0.0"
def load_config(path, env) -> Config          # unknown top-level key -> ConfigError naming it

# store.py
def canonical_url(raw: str) -> str
class Feed: id, url, name, enabled, favicon
class Store:
    upsert_feed(url, name=None, now=0) -> int   # canonicalises url; name COALESCE
    feed_by_url(url) -> Feed | None             # canonical lookup
    get_feed(feed_id); all_feeds(); set_feed_favicon(feed_id, favicon)
    set_enabled(feed_id, enabled: bool)
    disable_all_feeds()
    drop_disabled_feeds() -> int                # feeds + feed_state + articles where enabled = 0
    insert_articles(feed_id, entries, now); page_news(limit, before=None, after=None)
    sweep(now, retention_days); due_feeds(now); record_success(...); record_failure(...); all_feed_status()

# broadcast.py
class Subscription: feed_ids: set[int]; queue; dropped; closed
def article_payload(article, feed_name) -> dict
class Broadcaster:
    subscribe() -> Subscription
    set_feeds(sub, feed_ids: set[int])          # adjusts counts; 0->1 enables, 1->0 disables in the store
    unsubscribe(sub)
    subscriber_count(feed_id) -> int
    session_count() -> int
    async publish(articles)                     # fan-out to every subscription

# api.py
MAX_SUBSCRIBE_URLS = 200; MAX_URL_LEN = 2048
def parse_subscribe(frame) -> list[tuple[str, str | None]] | None
def create_app(config, store, broadcaster, lifespan=None, health_strict=False, on_feed_added=None)

# main.py
def build(config_path, db_path, env=None) -> FastAPI
```

- [ ] **Step 1: Delete what goes**

```bash
git rm -q src/rss_ticker/widgets.py src/rss_ticker/filters.py src/rss_ticker/reconcile.py \
  tests/test_widgets.py tests/test_widget_js.py tests/test_widget_route.py tests/test_filters.py tests/test_reconcile.py
git rm -rq src/rss_ticker/static tests/harness
```

- [ ] **Step 2: `config.py` — replace the file**

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# The whole vocabulary. Anything else is an error, so a config from the
# user-and-key era (users:, admin_key:, public_base_url:, ...) fails loudly at
# boot instead of being silently ignored into an empty pool.
KNOWN_KEYS = frozenset(
    {"retention_days", "default_poll_interval_s", "max_concurrent_polls", "bind_host"}
)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    retention_days: int = 7
    default_poll_interval_s: int = 300
    max_concurrent_polls: int = 8
    bind_host: str = "0.0.0.0"


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


def _positive_int(raw: dict, key: str, default: int) -> int:
    value = raw.get(key, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be a whole number, got {value!r}") from None
    if value < 1:
        raise ConfigError(f"{key} must be at least 1, got {value!r}")
    return value


def load_config(path: Path, env: Mapping[str, str]) -> Config:
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        raise ConfigError(
            f"config file not found at {path} -- did you mount it into the "
            f"container? Mount your config.yaml at CONFIG_PATH (default "
            f"/config/config.yaml); see the README deployment section."
        ) from None
    except IsADirectoryError:
        raise ConfigError(
            f"config path {path} is a directory, not a file -- a bind mount to "
            f"a host path that doesn't exist creates a directory there. Create "
            f"the config.yaml file on the host first, then mount it (or its "
            f"folder)."
        ) from None
    except OSError as exc:
        raise ConfigError(f"could not read config at {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config is not valid yaml: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    unknown = sorted(str(k) for k in raw if k not in KNOWN_KEYS)
    if unknown:
        raise ConfigError(
            f"config has keys this version does not use: {', '.join(unknown)}. "
            f"This server has no users, keys or configured feeds -- clients "
            f"subscribe feeds over the websocket. Keep only: "
            f"{', '.join(sorted(KNOWN_KEYS))}."
        )

    raw = _walk(raw, env)

    bind_host = raw.get("bind_host", "0.0.0.0")
    if not isinstance(bind_host, str) or not bind_host:
        raise ConfigError("bind_host must be a non-empty string")

    return Config(
        retention_days=_positive_int(raw, "retention_days", 7),
        default_poll_interval_s=_positive_int(raw, "default_poll_interval_s", 300),
        max_concurrent_polls=_positive_int(raw, "max_concurrent_polls", 8),
        bind_host=bind_host,
    )
```

`tests/test_config.py` — replace the file:

```python
from pathlib import Path

import pytest

from rss_ticker.config import ConfigError, load_config


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_loads_the_four_operational_keys(tmp_path):
    p = write(tmp_path, """
retention_days: 3
default_poll_interval_s: 120
max_concurrent_polls: 4
bind_host: 127.0.0.1
""")
    cfg = load_config(p, {})
    assert (cfg.retention_days, cfg.default_poll_interval_s, cfg.max_concurrent_polls) == (3, 120, 4)
    assert cfg.bind_host == "127.0.0.1"


def test_defaults_apply_to_an_empty_file(tmp_path):
    cfg = load_config(write(tmp_path, ""), {})
    assert (cfg.retention_days, cfg.default_poll_interval_s, cfg.max_concurrent_polls) == (7, 300, 8)
    assert cfg.bind_host == "0.0.0.0"


def test_env_expansion_still_works(tmp_path):
    cfg = load_config(write(tmp_path, "default_poll_interval_s: ${POLL}\n"), {"POLL": "90"})
    assert cfg.default_poll_interval_s == 90


def test_unset_env_variable_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="POLL"):
        load_config(write(tmp_path, "default_poll_interval_s: ${POLL}\n"), {})


def test_a_v8_config_fails_naming_every_stale_key(tmp_path):
    p = write(tmp_path, """
public_base_url: https://t.example
admin_key: k
manifest_key: mk
tailscale_auth: true
retention_days: 7
users:
  - id: art
""")
    with pytest.raises(ConfigError) as exc:
        load_config(p, {})
    message = str(exc.value)
    for key in ("admin_key", "manifest_key", "public_base_url", "tailscale_auth", "users"):
        assert key in message
    assert "retention_days" not in message.split("Keep only")[0]


@pytest.mark.parametrize("key", ["retention_days", "default_poll_interval_s", "max_concurrent_polls"])
def test_non_positive_and_non_numeric_values_are_errors(tmp_path, key):
    with pytest.raises(ConfigError, match=key):
        load_config(write(tmp_path, f"{key}: 0\n"), {})
    with pytest.raises(ConfigError, match=key):
        load_config(write(tmp_path, f"{key}: soon\n"), {})


def test_missing_file_and_directory_are_named_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml", {})
    with pytest.raises(ConfigError, match="directory"):
        load_config(tmp_path, {})


def test_root_must_be_a_mapping(tmp_path):
    with pytest.raises(ConfigError, match="mapping"):
        load_config(write(tmp_path, "- just\n- a list\n"), {})
```

- [ ] **Step 3: `store.py` — the feed pool**

Replace `SCHEMA` and `POST_MIGRATION_SCHEMA`:

```python
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

POST_MIGRATION_SCHEMA = """
DROP INDEX IF EXISTS idx_articles_fetched;
DROP INDEX IF EXISTS idx_subs_user;
CREATE INDEX IF NOT EXISTS idx_articles_last_seen ON articles (last_seen_at);
"""
```

Add, at module level after `_like_escape` (which is then unused — delete `_like_escape` and the `filters` import):

```python
def canonical_url(raw: str) -> str:
    """The pool's identity for a feed: scheme and host lowercased, one
    trailing slash stripped, nothing cleverer (design decision 2)."""
    parts = urlsplit(raw.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc
    host = parts.hostname or ""
    if host:
        # Rebuild netloc with a lowercased host, keeping userinfo and port.
        userinfo, _, hostport = netloc.rpartition("@")
        _, _, port = hostport.rpartition(":") if hostport.count(":") == 1 and not hostport.startswith("[") else ("", "", "")
        rebuilt = host + (f":{port}" if port else "")
        netloc = f"{userinfo}@{rebuilt}" if userinfo else rebuilt
    path = parts.path.rstrip("/") if parts.path not in ("", "/") else ""
    return urlunsplit((scheme, netloc, path, parts.query, parts.fragment))
```

(`from urllib.parse import urlsplit, urlunsplit` at the top. The plan's port handling is deliberately minimal — an IPv6 literal keeps its netloc as-is.)

`Feed` becomes `id, url, name, enabled, favicon` (drop `poll_interval_s`, `group`, `title_format`); `_feed` accordingly.

Replace `_migrate` with:

```python
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
        self.db.execute("UPDATE articles SET last_seen_at = fetched_at WHERE last_seen_at = 0")
        if "author" not in columns:
            self.db.execute("ALTER TABLE articles ADD COLUMN author TEXT")

        feed_columns = {r["name"] for r in self.db.execute("PRAGMA table_info(feeds)").fetchall()}
        if "favicon" not in feed_columns:
            self.db.execute("ALTER TABLE feeds ADD COLUMN favicon TEXT")
        for dead in ("group", "title_format", "poll_interval_s"):
            if dead in feed_columns:
                self.db.execute(f'ALTER TABLE feeds DROP COLUMN "{dead}"')

        for row in self.db.execute("SELECT id, url FROM feeds").fetchall():
            canon = canonical_url(row["url"])
            if canon == row["url"]:
                continue
            taken = self.db.execute("SELECT 1 FROM feeds WHERE url = ?", (canon,)).fetchone()
            if taken is None:
                self.db.execute("UPDATE feeds SET url = ? WHERE id = ?", (canon, row["id"]))
        self.db.commit()
```

Delete: `upsert_user`, `token_for`, `users_without_tokens`, `revoke_tokens_except`, `clear_token`, `user_exists`, `subscribe`, `unsubscribe`, `subscribers_of`, `list_feeds`, `add_filter`, `filters_for`.

Replace `upsert_feed` and add the feed-pool methods:

```python
    @_synchronized
    def upsert_feed(self, url: str, name: str | None = None, now: int = 0) -> int:
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
```

Note the COALESCE order in `upsert_feed`: the *stored* name wins; a client's `name` only lands on a feed new to the pool (addendum, reply frame).

`page_news` loses `user_id` and the filters:

```python
    @_synchronized
    def page_news(
        self, limit: int, before: str | None = None, after: str | None = None
    ) -> tuple[list[Article], str | None]:
        """Page the whole pool. `before` (and no cursor) walks newest-first;
        `after` walks oldest-first for a reconnect gap fill -- see the base
        design for why the asymmetry is deliberate."""
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
```

The `create_function("lower", …)` in `__init__` existed for the filters — remove it. `all_feed_status` also selects `f.favicon IS NOT NULL AS has_favicon`? No — keep it as is (id, url, name, enabled, timings, failures, last_error).

- [ ] **Step 4: `normalize.py` and `poller.py`**

`normalize_entry(entry, now)` and `parse_feed(body, now)`: delete `_PLACEHOLDER_RE`, `_TAG_ALIASES` stays (used by `_entry_field`), delete `_format_title`, the `title_format` branch and parameter. `poller.py`: `parse_feed, outcome.body or b"", now` (no third argument).

- [ ] **Step 5: `broadcast.py` — replace the file**

```python
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .store import Article, Store, encode_cursor

log = logging.getLogger(__name__)

MAX_QUEUE = 1000


@dataclass(eq=False)
class Subscription:
    """One open socket: the feed ids it named, and its outbound queue."""

    feed_ids: set[int] = field(default_factory=set)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUE))
    dropped: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)


def article_payload(article: Article, feed_name: str | None) -> dict:
    return {
        "id": article.id,
        "feed_id": article.feed_id,
        "cursor": encode_cursor(article.sort_at, article.id),
        "title": article.title,
        "link": article.link,
        "summary": article.summary,
        "source": feed_name,
        "author": article.author,
        "published_at": article.published_at,
        "sort_at": article.sort_at,
    }


class Broadcaster:
    """Fan-out and the subscriber counts.

    Counts are in memory, derived from open sockets (design decision 3): a
    feed is polled while at least one socket names it. The store's `enabled`
    flag mirrors "count > 0" so the poller's due-feeds query needs no
    knowledge of sockets.
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self._subs: set[Subscription] = set()
        self._counts: dict[int, int] = {}

    def subscribe(self) -> Subscription:
        sub = Subscription()
        self._subs.add(sub)
        return sub

    def set_feeds(self, sub: Subscription, feed_ids: set[int]) -> None:
        """Replace `sub`'s feed set, adjusting counts by the difference."""
        for feed_id in feed_ids - sub.feed_ids:
            self._counts[feed_id] = self._counts.get(feed_id, 0) + 1
            if self._counts[feed_id] == 1:
                self.store.set_enabled(feed_id, True)
        for feed_id in sub.feed_ids - feed_ids:
            remaining = self._counts.get(feed_id, 0) - 1
            if remaining <= 0:
                self._counts.pop(feed_id, None)
                self.store.set_enabled(feed_id, False)
            else:
                self._counts[feed_id] = remaining
        sub.feed_ids = set(feed_ids)

    def unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subs:
            self.set_feeds(sub, set())
            self._subs.discard(sub)

    def subscriber_count(self, feed_id: int) -> int:
        return self._counts.get(feed_id, 0)

    def session_count(self) -> int:
        return len(self._subs)

    async def publish(self, articles: list[Article]) -> None:
        """Every inserted article to every open socket; clients filter."""
        if not articles or not self._subs:
            return
        names: dict[int, str | None] = {}
        for article in articles:
            if article.feed_id not in names:
                feed = self.store.get_feed(article.feed_id)
                names[article.feed_id] = feed.name if feed else None
            payload = article_payload(article, names[article.feed_id])
            for sub in list(self._subs):
                try:
                    sub.queue.put_nowait(payload)
                except asyncio.QueueFull:
                    sub.dropped = True
                    sub.closed.set()
                    self.unsubscribe(sub)
                    log.warning("Dropped a slow subscriber")
```

`tests/test_broadcast.py` — replace the file:

```python
import asyncio

import pytest

from rss_ticker.broadcast import MAX_QUEUE, Broadcaster
from rss_ticker.store import NewArticle, Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def add(store, url="https://x.example/rss", name="X"):
    return store.upsert_feed(url, name=name, now=0)


def articles(store, fid, specs):
    return store.insert_articles(
        fid, [NewArticle(g, t, None, None, ts) for g, t, ts in specs], now=1000
    )


async def drain(sub):
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


async def test_every_socket_receives_every_article(store):
    a, b = add(store), add(store, url="https://y.example/rss", name="Y")
    bc = Broadcaster(store)
    s1, s2 = bc.subscribe(), bc.subscribe()
    bc.set_feeds(s1, {a})
    bc.set_feeds(s2, set())
    await bc.publish(articles(store, b, [("g", "From Y", 1)]))
    # Filtering is the client's job: s1 named only `a` and still gets Y's article.
    assert [m["feed_id"] for m in await drain(s1)] == [b]
    assert [m["feed_id"] for m in await drain(s2)] == [b]


async def test_payload_shape(store):
    fid = add(store, name="Reuters")
    bc = Broadcaster(store)
    sub = bc.subscribe()
    await bc.publish(
        store.insert_articles(fid, [NewArticle("a", "Fed holds", "https://l", "s", 1, author="Jane")], now=1000)
    )
    msg = (await drain(sub))[0]
    assert set(msg) == {"id", "feed_id", "cursor", "title", "link", "summary", "source", "author", "published_at", "sort_at"}
    assert (msg["source"], msg["author"], msg["feed_id"]) == ("Reuters", "Jane", fid)


def test_counts_follow_the_sockets_and_drive_enabled(store):
    a, b = add(store), add(store, url="https://y.example/rss")
    store.disable_all_feeds()
    bc = Broadcaster(store)
    s1, s2 = bc.subscribe(), bc.subscribe()

    bc.set_feeds(s1, {a, b})
    assert (bc.subscriber_count(a), bc.subscriber_count(b)) == (1, 1)
    assert store.get_feed(a).enabled and store.get_feed(b).enabled

    bc.set_feeds(s2, {a})
    assert bc.subscriber_count(a) == 2

    bc.set_feeds(s1, {b})  # s1 drops a
    assert bc.subscriber_count(a) == 1
    assert store.get_feed(a).enabled

    bc.unsubscribe(s2)
    assert bc.subscriber_count(a) == 0
    assert not store.get_feed(a).enabled
    assert store.get_feed(b).enabled
    assert bc.session_count() == 1


def test_unsubscribe_is_idempotent(store):
    a = add(store)
    bc = Broadcaster(store)
    sub = bc.subscribe()
    bc.set_feeds(sub, {a})
    bc.unsubscribe(sub)
    bc.unsubscribe(sub)
    assert bc.subscriber_count(a) == 0
    assert store.get_feed(a).enabled is False


async def test_slow_client_is_dropped_not_awaited(store):
    fid = add(store)
    bc = Broadcaster(store)
    sub = bc.subscribe()
    bc.set_feeds(sub, {fid})
    for i in range(MAX_QUEUE):
        sub.queue.put_nowait({"filler": i})
    await asyncio.wait_for(bc.publish(articles(store, fid, [("a", "Fed holds", 1)])), timeout=1.0)
    assert sub.dropped is True
    assert sub.closed.is_set()
    assert bc.session_count() == 0
    # Dropping the socket released its subscription.
    assert bc.subscriber_count(fid) == 0
    assert store.get_feed(fid).enabled is False


async def test_unsubscribe_stops_delivery(store):
    fid = add(store)
    bc = Broadcaster(store)
    sub = bc.subscribe()
    bc.unsubscribe(sub)
    await bc.publish(articles(store, fid, [("a", "Fed holds", 1)]))
    assert await drain(sub) == []
```

- [ ] **Step 6: `api.py` — replace the file**

```python
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .broadcast import Broadcaster, article_payload
from .config import Config
from .store import CursorError, Feed, Store

# bdobb-v2's origins: the Vite dev server, its browser-mode e2e server, and
# the Tauri webview on macOS/iOS and on Windows. No credentials -- there are
# no cookies and no keys.
ALLOWED_ORIGINS = [
    "http://localhost:1420",
    "http://localhost:4173",
    "tauri://localhost",
    "http://tauri.localhost",
]

log = logging.getLogger(__name__)

# A single failed poll is a network blip, not an unhealthy deployment.
DEGRADED_AFTER_FAILURES = 3

# Bounds on the one thing a client can make this server do: poll a URL.
MAX_SUBSCRIBE_URLS = 200
MAX_URL_LEN = 2048

# Close code for a malformed subscribe frame. Not 1003/1008 (which some
# clients treat as terminal) and not 4401 (the retired auth code).
INVALID_SUBSCRIBE = 4400

OnFeedAdded = Callable[[Feed], None]


def parse_subscribe(frame: object) -> list[tuple[str, str | None]] | None:
    """The (url, name) pairs of a subscribe frame, or None if it is not one.

    Hostile input: every field is checked by type and size before use.
    """
    if not isinstance(frame, dict) or not isinstance(frame.get("subscribe"), list):
        return None
    entries = frame["subscribe"]
    if len(entries) > MAX_SUBSCRIBE_URLS:
        return None
    out: list[tuple[str, str | None]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
            return None
        url = entry["url"].strip()
        if not url or len(url) > MAX_URL_LEN:
            return None
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
        name = entry.get("name")
        if name is not None and not isinstance(name, str):
            return None
        name = name.strip() if name else None
        out.append((url, name or None))
    return out


def feed_record(feed: Feed) -> dict:
    return {"id": feed.id, "url": feed.url, "title": feed.name, "favicon": feed.favicon}


def create_app(
    config: Config,
    store: Store,
    broadcaster: Broadcaster,
    lifespan=None,
    health_strict: bool = False,
    on_feed_added: OnFeedAdded | None = None,
) -> FastAPI:
    app = FastAPI(title="rss-ticker", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.config = config
    app.state.store = store
    app.state.broadcaster = broadcaster
    app.state.health_strict = health_strict

    @app.get("/")
    def root() -> dict:
        return {"service": "rss-ticker", "version": __version__}

    @app.get("/api/news")
    def news(
        limit: int = Query(50, ge=1, le=200),
        before: str | None = Query(None),
        after: str | None = Query(None),
    ) -> dict:
        if before and after:
            raise HTTPException(status_code=400, detail="Pass before or after, not both")
        try:
            articles, next_cursor = store.page_news(limit=limit, before=before, after=after)
        except CursorError:
            raise HTTPException(status_code=400, detail="Cursor is not valid") from None
        names: dict[int, str | None] = {}
        payloads = []
        for article in articles:
            if article.feed_id not in names:
                feed = store.get_feed(article.feed_id)
                names[article.feed_id] = feed.name if feed else None
            payloads.append(article_payload(article, names[article.feed_id]))
        return {"articles": payloads, "next_cursor": next_cursor}

    @app.get("/api/feeds")
    def list_feeds() -> dict:
        return {
            "feeds": [
                {
                    **feed_record(f),
                    "subscribers": broadcaster.subscriber_count(f.id),
                    "enabled": f.enabled,
                }
                for f in store.all_feeds()
            ]
        }

    @app.get("/api/health")
    def health(response: Response) -> dict:
        feeds = store.all_feed_status()
        degraded = any(
            f["enabled"] and f["consecutive_failures"] >= DEGRADED_AFTER_FAILURES for f in feeds
        )
        if degraded and health_strict:
            response.status_code = 503
        return {
            "status": "degraded" if degraded else "ok",
            "version": __version__,
            "sessions": broadcaster.session_count(),
            "feeds": feeds,
        }

    def resolve_subscription(entries: list[tuple[str, str | None]]) -> tuple[list[dict], set[int]]:
        """Upsert each URL into the pool; report which were new."""
        records: list[dict] = []
        ids: set[int] = set()
        now = int(time.time())
        for url, name in entries:
            existing = store.feed_by_url(url)
            feed_id = store.upsert_feed(url, name=name, now=now)
            if feed_id in ids:
                continue
            ids.add(feed_id)
            feed = store.get_feed(feed_id)
            assert feed is not None
            records.append(feed_record(feed))
            if existing is None and on_feed_added is not None:
                on_feed_added(feed)
        return records, ids

    @app.websocket("/ws/news")
    async def ws_news(websocket: WebSocket) -> None:
        await websocket.accept()
        sub = broadcaster.subscribe()
        receiver = asyncio.create_task(websocket.receive())
        sender = asyncio.create_task(sub.queue.get())
        closer = asyncio.create_task(sub.closed.wait())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {receiver, sender, closer}, return_when=asyncio.FIRST_COMPLETED
                )
                if closer in done:
                    await websocket.close(code=1013, reason="Subscriber queue overflowed")
                    break
                if receiver in done:
                    message = receiver.result()  # raises WebSocketDisconnect on close
                    if message.get("type") == "websocket.disconnect":
                        break
                    text = message.get("text")
                    frame: object = None
                    if isinstance(text, str):
                        try:
                            frame = json.loads(text)
                        except ValueError:
                            frame = None
                    entries = parse_subscribe(frame)
                    if entries is None:
                        await websocket.close(code=INVALID_SUBSCRIBE, reason="Expected a subscribe frame")
                        break
                    records, ids = resolve_subscription(entries)
                    broadcaster.set_feeds(sub, ids)
                    await websocket.send_json({"feeds": records})
                    receiver = asyncio.create_task(websocket.receive())
                if sender in done:
                    await websocket.send_json(sender.result())
                    sender = asyncio.create_task(sub.queue.get())
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Websocket handler failed")
        finally:
            for task in (receiver, sender, closer):
                task.cancel()
            broadcaster.unsubscribe(sub)

    return app
```

`tests/test_api_ws.py` — replace the file:

```python
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rss_ticker.api import INVALID_SUBSCRIBE, MAX_SUBSCRIBE_URLS, create_app, parse_subscribe
from rss_ticker.broadcast import MAX_QUEUE, Broadcaster
from rss_ticker.config import Config
from rss_ticker.store import NewArticle, Store

A = "https://a.example/feed"
B = "https://b.example/feed"


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def broadcaster(store):
    return Broadcaster(store)


@pytest.fixture
def client(store, broadcaster):
    return TestClient(create_app(Config(), store, broadcaster))


def test_first_frame_subscribes_and_is_answered_with_feed_records(client, store, broadcaster):
    with client.websocket_connect("/ws/news") as ws:
        ws.send_json({"subscribe": [{"url": A, "name": "A wire"}, {"url": B}]})
        reply = ws.receive_json()
        assert [r["title"] for r in reply["feeds"]] == ["A wire", None]
        assert [r["url"] for r in reply["feeds"]] == [A, B]
        assert all(r["favicon"] is None for r in reply["feeds"])
        ids = {r["id"] for r in reply["feeds"]}
        assert broadcaster.session_count() == 1
        assert all(broadcaster.subscriber_count(i) == 1 for i in ids)
        assert all(store.get_feed(i).enabled for i in ids)
    assert broadcaster.session_count() == 0
    assert all(broadcaster.subscriber_count(i) == 0 for i in ids)


def test_subscribing_an_existing_url_reuses_the_feed_and_keeps_its_name(client, store):
    fid = store.upsert_feed("https://A.example/feed/", name="Stored", now=0)
    with client.websocket_connect("/ws/news") as ws:
        ws.send_json({"subscribe": [{"url": A, "name": "Client name"}]})
        reply = ws.receive_json()
    assert reply["feeds"] == [{"id": fid, "url": A, "title": "Stored", "favicon": None}]
    assert len(store.all_feeds()) == 1


def test_duplicate_urls_in_one_frame_collapse_to_one_record(client, store):
    with client.websocket_connect("/ws/news") as ws:
        ws.send_json({"subscribe": [{"url": A}, {"url": "HTTPS://a.example/feed/"}]})
        reply = ws.receive_json()
    assert len(reply["feeds"]) == 1
    assert len(store.all_feeds()) == 1


def test_a_later_subscribe_replaces_the_set(client, store, broadcaster):
    with client.websocket_connect("/ws/news") as ws:
        ws.send_json({"subscribe": [{"url": A}]})
        a = ws.receive_json()["feeds"][0]["id"]
        ws.send_json({"subscribe": [{"url": B}]})
        b = ws.receive_json()["feeds"][0]["id"]
        assert (broadcaster.subscriber_count(a), broadcaster.subscriber_count(b)) == (0, 1)
        assert store.get_feed(a).enabled is False


@pytest.mark.parametrize(
    "frame",
    [
        "not json",
        {"subscribe": "no"},
        {"nope": []},
        {"subscribe": [{"url": "ftp://x.example/feed"}]},
        {"subscribe": [{"url": "javascript:alert(1)"}]},
        {"subscribe": [{"url": A, "name": 3}]},
        {"subscribe": [{"url": "x" * 3000}]},
        {"subscribe": [{"url": f"https://h{i}.example/f"} for i in range(MAX_SUBSCRIBE_URLS + 1)]},
    ],
)
def test_an_invalid_subscribe_frame_closes_4400_and_registers_nothing(client, store, broadcaster, frame):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/news") as ws:
            if isinstance(frame, str):
                ws.send_text(frame)
            else:
                ws.send_json(frame)
            ws.receive_json()
    assert exc.value.code == INVALID_SUBSCRIBE
    assert store.all_feeds() == []
    assert broadcaster.session_count() == 0


def test_parse_subscribe_trims_and_normalises_names():
    assert parse_subscribe({"subscribe": [{"url": " https://a.example/f ", "name": "  "}]}) == [
        ("https://a.example/f", None)
    ]
    assert parse_subscribe({"subscribe": []}) == []
    assert parse_subscribe([]) is None


def test_articles_stream_after_the_reply_with_the_feed_id(client, store, broadcaster):
    with client:
        with client.websocket_connect("/ws/news") as ws:
            ws.send_json({"subscribe": [{"url": A, "name": "A"}]})
            fid = ws.receive_json()["feeds"][0]["id"]

            async def publish():
                arts = store.insert_articles(
                    fid, [NewArticle("g", "Fed holds", "https://l", None, 1, author="Jane")], now=1000
                )
                await broadcaster.publish(arts)

            client.portal.call(publish)
            msg = ws.receive_json()
    assert (msg["feed_id"], msg["title"], msg["author"], msg["source"]) == (fid, "Fed holds", "Jane", "A")
    assert "highlighted" not in msg


def test_ws_closes_with_a_reconnect_code_when_the_subscriber_is_dropped(client, store, broadcaster):
    fid = store.upsert_feed(A, name="A", now=0)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client:
            with client.websocket_connect("/ws/news") as ws:
                ws.send_json({"subscribe": [{"url": A}]})
                ws.receive_json()
                sub = next(iter(broadcaster._subs))

                async def overflow_and_publish():
                    for i in range(MAX_QUEUE):
                        sub.queue.put_nowait({"filler": i})
                    arts = store.insert_articles(fid, [NewArticle("a", "Fed holds", None, None, 1)], now=1000)
                    await broadcaster.publish(arts)

                client.portal.call(overflow_and_publish)
                ws.receive_json()
    assert exc.value.code == 1013


def test_a_new_feed_triggers_on_feed_added_once(store, broadcaster):
    added = []
    app = create_app(Config(), store, broadcaster, on_feed_added=added.append)
    client = TestClient(app)
    with client.websocket_connect("/ws/news") as ws:
        ws.send_json({"subscribe": [{"url": A}]})
        ws.receive_json()
        ws.send_json({"subscribe": [{"url": A}, {"url": B}]})
        ws.receive_json()
    assert [f.url for f in added] == [A, B]
```

`tests/test_api_rest.py` — replace the file:

```python
import pytest
from fastapi.testclient import TestClient

from rss_ticker.api import DEGRADED_AFTER_FAILURES, create_app
from rss_ticker.broadcast import Broadcaster
from rss_ticker.config import Config
from rss_ticker.store import NewArticle, Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def broadcaster(store):
    return Broadcaster(store)


@pytest.fixture
def client(store, broadcaster):
    return TestClient(create_app(Config(), store, broadcaster))


def seed(store, n=3, url="https://x.example/rss", name="X"):
    fid = store.upsert_feed(url, name=name, now=0)
    store.insert_articles(
        fid,
        [NewArticle(f"g{i}", f"headline {i}", "https://l", None, 1000 + i) for i in range(n)],
        now=1000,
    )
    return fid


def test_root_returns_service_and_version_only(client):
    body = client.get("/").json()
    assert body["service"] == "rss-ticker"
    assert set(body) == {"service", "version"}


def test_news_returns_the_whole_pool_newest_first_with_no_user_param(client, store):
    seed(store, 2)
    seed(store, 1, url="https://y.example/rss", name="Y")
    body = client.get("/api/news").json()
    assert [a["title"] for a in body["articles"]] == ["headline 1", "headline 0", "headline 0"]
    assert {a["source"] for a in body["articles"]} == {"X", "Y"}
    assert "highlighted" not in body["articles"][0]
    assert body["articles"][0]["author"] is None


def test_news_ignores_a_stray_user_param(client, store):
    seed(store, 1)
    assert client.get("/api/news", params={"user": "art", "token": "x"}).status_code == 200


def test_news_paging_uses_cursor(client, store):
    seed(store, 5)
    first = client.get("/api/news", params={"limit": 2}).json()
    assert first["next_cursor"]
    second = client.get("/api/news", params={"limit": 2, "before": first["next_cursor"]}).json()
    assert not ({a["id"] for a in first["articles"]} & {a["id"] for a in second["articles"]})


def test_news_after_cursor_pages_forward_oldest_first(client, store):
    seed(store, 5)
    backlog = client.get("/api/news").json()["articles"]
    held = backlog[-1]
    first = client.get("/api/news", params={"limit": 2, "after": held["cursor"]}).json()
    assert [a["title"] for a in first["articles"]] == ["headline 1", "headline 2"]


def test_news_rejects_both_cursors_and_a_bad_cursor(client):
    assert client.get("/api/news", params={"before": "a", "after": "b"}).status_code == 400
    assert client.get("/api/news", params={"before": "not-a-cursor"}).status_code == 400


def test_news_limit_bounds(client):
    assert client.get("/api/news", params={"limit": 0}).status_code == 422
    assert client.get("/api/news", params={"limit": 201}).status_code == 422


def test_feeds_lists_the_pool_with_counts_and_full_urls(client, store, broadcaster):
    fid = store.upsert_feed("https://u:apikey@x.example/rss?k=1", name="X", now=0)
    store.set_feed_favicon(fid, "data:image/png;base64,AA==")
    sub = broadcaster.subscribe()
    broadcaster.set_feeds(sub, {fid})
    body = client.get("/api/feeds").json()
    assert body["feeds"] == [
        {
            "id": fid,
            "url": "https://u:apikey@x.example/rss?k=1",
            "title": "X",
            "favicon": "data:image/png;base64,AA==",
            "subscribers": 1,
            "enabled": True,
        }
    ]


def test_health_carries_full_feed_detail_without_any_key(client, store):
    fid = store.upsert_feed("https://x.example/rss", name="X", now=0)
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["sessions"] == 0
    assert body["feeds"][0]["url"] == "https://x.example/rss"
    for _ in range(DEGRADED_AFTER_FAILURES):
        store.record_failure(fid, error="boom", now=1, next_poll_at=2)
    assert client.get("/api/health").json()["status"] == "degraded"


def test_health_strict_returns_503_when_degraded(store, broadcaster):
    client = TestClient(create_app(Config(), store, broadcaster, health_strict=True))
    fid = store.upsert_feed("https://x.example/rss", now=0)
    for _ in range(DEGRADED_AFTER_FAILURES):
        store.record_failure(fid, error="boom", now=1, next_poll_at=2)
    r = client.get("/api/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"


def test_disabled_feed_failures_do_not_degrade(client, store):
    fid = store.upsert_feed("https://x.example/rss", now=0)
    store.set_enabled(fid, False)
    for _ in range(DEGRADED_AFTER_FAILURES):
        store.record_failure(fid, error="boom", now=1, next_poll_at=2)
    assert client.get("/api/health").json()["status"] == "ok"


def test_retired_routes_are_gone(client):
    for path in ("/widgets.json", "/widget"):
        assert client.get(path).status_code == 404
    assert client.post("/api/feeds", json={}).status_code == 405
    assert client.delete("/api/feeds/1").status_code == 405


def test_cors_admits_the_tauri_origin_without_credentials(client):
    r = client.options(
        "/api/news",
        headers={"Origin": "tauri://localhost", "Access-Control-Request-Method": "GET"},
    )
    assert r.headers.get("access-control-allow-origin") == "tauri://localhost"
    assert "access-control-allow-credentials" not in r.headers
    r = client.options(
        "/api/news",
        headers={"Origin": "https://pro.openbb.co", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in r.headers
```

- [ ] **Step 7: `main.py` — replace the file**

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
from .config import ConfigError, load_config
from .favicon import refresh_favicons, resolve_and_store
from .fetch import TIMEOUT_S
from .poller import Poller
from .store import Feed, Store

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 3600

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(env: Mapping[str, str], name: str) -> bool:
    return str(env.get(name, "")).strip().lower() in _TRUTHY


def build(config_path: Path, db_path: str, env: Mapping[str, str] | None = None) -> FastAPI:
    resolved_env = os.environ if env is None else env
    config = load_config(config_path, resolved_env)
    health_strict = _env_flag(resolved_env, "HEALTH_STRICT")
    store = Store(db_path)
    # Nobody is connected at boot, so nothing is polled until a socket asks
    # (decision B). Clients reconnect within seconds and re-enable their feeds.
    store.disable_all_feeds()
    broadcaster = Broadcaster(store)

    # The HTTP client is created in the lifespan; the subscribe path needs it
    # for a new feed's favicon, so it is handed over through this holder.
    holder: dict[str, httpx.AsyncClient] = {}

    def on_feed_added(feed: Feed) -> None:
        client = holder.get("client")
        if client is None:
            return
        asyncio.create_task(resolve_and_store(store, client, feed))

    async def sweeper() -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            try:
                deleted = await asyncio.to_thread(store.sweep, int(time.time()), config.retention_days)
                dropped = await asyncio.to_thread(store.drop_disabled_feeds)
                log.info("Retention sweep removed %d articles and %d idle feeds", deleted, dropped)
            except Exception:
                log.exception("Retention sweep failed")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        client = httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True)
        holder["client"] = client
        poller = Poller(store, client, config, on_new_articles=broadcaster.publish)
        tasks = [
            asyncio.create_task(poller.run_forever()),
            asyncio.create_task(sweeper()),
            asyncio.create_task(
                refresh_favicons(store, client, concurrency=config.max_concurrent_polls)
            ),
        ]
        try:
            yield
        finally:
            holder.pop("client", None)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()
            store.close()

    app = create_app(
        config,
        store,
        broadcaster,
        lifespan=lifespan,
        health_strict=health_strict,
        on_feed_added=on_feed_added,
    )
    app.state.bind_host = config.bind_host
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs every outbound request URL at INFO, and feed URLs can carry
    # credentials; keep it quiet (see the base design's logging notes).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        app = build(
            Path(os.environ.get("CONFIG_PATH", "/config/config.yaml")),
            os.environ.get("DB_PATH", "/data/ticker.db"),
        )
    except ConfigError as exc:
        raise SystemExit(f"rss-ticker: {exc}") from None
    # Access logging is back on: nothing secret rides a request line any more.
    uvicorn.run(app, host=app.state.bind_host, port=int(os.environ.get("PORT", "8088")))


if __name__ == "__main__":
    main()
```

`favicon.py` — add, above `refresh_favicons`, and make `refresh_favicons.resolve_one` call it:

```python
async def resolve_and_store(store: Store, client: httpx.AsyncClient, feed) -> None:
    """Resolve one feed's favicon and store it; never raises, never wipes a
    known icon on failure. Used at startup for every feed and at subscribe
    time for a feed new to the pool (decision D)."""
    try:
        icon = await resolve_favicon(client, feed.url)
        if icon:
            store.set_feed_favicon(feed.id, icon)
    except Exception as exc:
        log.debug("Favicon resolution failed for %s: %s", redact_feed_url(feed.url), exc)
```

- [ ] **Step 8: Rewrite the remaining tests**

`tests/test_main.py` — replace:

```python
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from rss_ticker import main as main_mod
from rss_ticker.main import build

CONFIG = """
retention_days: 2
default_poll_interval_s: 60
"""


def app_for(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    return build(cfg, str(tmp_path / "t.db"), env={})


def test_build_serves_an_empty_pool(tmp_path):
    with TestClient(app_for(tmp_path)) as client:
        assert client.get("/api/health").json()["feeds"] == []
        assert client.get("/api/news").json()["articles"] == []
        assert client.get("/api/feeds").json()["feeds"] == []


def test_every_feed_starts_disabled_at_boot(tmp_path):
    db = str(tmp_path / "t.db")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    with TestClient(build(cfg, db, env={})) as client:
        with client.websocket_connect("/ws/news") as ws:
            ws.send_json({"subscribe": [{"url": "https://a.example/rss"}]})
            fid = ws.receive_json()["feeds"][0]["id"]
            assert client.get("/api/feeds").json()["feeds"][0]["enabled"] is True
    # A second build over the same database: nobody connected, so disabled.
    with TestClient(build(cfg, db, env={})) as client:
        feeds = client.get("/api/feeds").json()["feeds"]
        assert [(f["id"], f["enabled"], f["subscribers"]) for f in feeds] == [(fid, False, 0)]


def test_a_stale_config_exits_with_one_clean_line(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("admin_key: k\nusers: []\n")
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    import pytest

    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert "admin_key" in str(exc.value) and "users" in str(exc.value)


def test_new_feed_resolves_its_favicon_in_the_background(tmp_path, monkeypatch):
    calls = []

    async def fake_resolve(client, url):
        calls.append(url)
        return "data:image/png;base64,AA=="

    monkeypatch.setattr("rss_ticker.favicon.resolve_favicon", fake_resolve)
    with TestClient(app_for(tmp_path)) as client:
        with client.websocket_connect("/ws/news") as ws:
            ws.send_json({"subscribe": [{"url": "https://a.example/rss"}]})
            ws.receive_json()
        import time

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            feeds = client.get("/api/feeds").json()["feeds"]
            if feeds and feeds[0]["favicon"]:
                break
            time.sleep(0.05)
        assert feeds[0]["favicon"] == "data:image/png;base64,AA=="
    assert calls == ["https://a.example/rss"]


def test_database_persists_across_builds(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    db = str(tmp_path / "t.db")
    with TestClient(build(cfg, db, env={})) as client:
        with client.websocket_connect("/ws/news") as ws:
            ws.send_json({"subscribe": [{"url": "https://a.example/rss", "name": "A"}]})
            ws.receive_json()
    rows = sqlite3.connect(db).execute("SELECT url, name FROM feeds").fetchall()
    assert rows == [("https://a.example/rss", "A")]
```

(`monkeypatch.setattr` on `rss_ticker.favicon.resolve_favicon` works because `resolve_and_store` looks the name up in its module at call time.)

`tests/test_migration.py` — replace:

```python
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
    return {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master WHERE type='table'")}


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
```

`tests/test_integration.py` — replace:

```python
import asyncio
from pathlib import Path

import httpx
import pytest

from rss_ticker.broadcast import Broadcaster
from rss_ticker.config import Config
from rss_ticker.poller import Poller
from rss_ticker.store import Store

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
    config = Config(retention_days=7, default_poll_interval_s=1)
    store = Store(str(tmp_path / "t.db"))
    live = store.upsert_feed("https://live.example/rss", name="Live", now=0)
    dead = store.upsert_feed("https://dead.example/rss", name="Dead", now=0)
    broadcaster = Broadcaster(store)
    sub = broadcaster.subscribe()
    broadcaster.set_feeds(sub, {live, dead})
    yield config, store, broadcaster, sub, tmp_path
    store.close()


def poller_for(store, config, broadcaster, bodies):
    def handler(request):
        if "dead" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, content=bodies[0])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Poller(store, client, config, on_new_articles=broadcaster.publish, jitter=lambda: 1.0)


async def test_new_article_reaches_a_live_subscriber(wiring):
    config, store, broadcaster, sub, _ = wiring
    bodies = [ROUND1]
    poller = poller_for(store, config, broadcaster, bodies)
    await poller.run_once(now=100)  # cold start: cache, no broadcast
    assert sub.queue.empty()
    bodies[0] = ROUND2
    store.db.execute("UPDATE feed_state SET next_poll_at = 0")
    store.db.commit()
    await poller.run_once(now=200)
    msg = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
    assert msg["title"] == "Breaking now"
    assert sub.queue.empty(), "only genuinely new articles should broadcast"


async def test_cold_start_articles_are_scrollable_but_were_not_broadcast(wiring):
    config, store, broadcaster, sub, _ = wiring
    poller = poller_for(store, config, broadcaster, [ROUND1])
    await poller.run_once(now=100)
    assert sub.queue.empty()
    rows, _ = store.page_news(limit=10)
    assert {r.title for r in rows} == {"Backfilled one", "Backfilled two"}


async def test_failing_feed_does_not_stop_the_healthy_one(wiring):
    config, store, broadcaster, _, _ = wiring
    poller = poller_for(store, config, broadcaster, [ROUND1])
    await poller.run_once(now=100)
    status = {f["name"]: f for f in store.all_feed_status()}
    assert status["Dead"]["consecutive_failures"] == 1
    assert status["Live"]["last_success_at"] == 100


async def test_a_feed_nobody_names_is_not_polled_and_is_dropped_by_the_sweep(wiring):
    config, store, broadcaster, sub, _ = wiring
    live = store.feed_by_url("https://live.example/rss").id
    broadcaster.set_feeds(sub, {live})  # drop Dead
    poller = poller_for(store, config, broadcaster, [ROUND1])
    assert await poller.run_once(now=100) == 1
    assert store.drop_disabled_feeds() == 1
    assert [f.name for f in store.all_feeds()] == ["Live"]


async def test_cache_survives_a_restart(wiring):
    config, store, broadcaster, _, tmp_path = wiring
    poller = poller_for(store, config, broadcaster, [ROUND1])
    await poller.run_once(now=100)
    store.close()
    reopened = Store(str(tmp_path / "t.db"))
    try:
        seen: list[str] = []
        before = None
        while True:
            page, before = reopened.page_news(limit=1, before=before)
            seen += [r.title for r in page]
            if before is None:
                break
        assert sorted(seen) == ["Backfilled one", "Backfilled two"]
    finally:
        reopened.close()
```

`tests/test_ws_live.py` — replace the config and the connections:

```python
import asyncio
import json
import socket
import threading
import time
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
def live_server(tmp_path: Path, request):
    poll_interval_s = getattr(request, "param", 1)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"default_poll_interval_s: {poll_interval_s}\n")
    app = build(cfg, str(tmp_path / "t.db"), env={})
    served = {"n": 0}

    @app.get("/fixture.xml")
    def fixture() -> Response:
        served["n"] += 1
        return Response(content=ROUND1 if served["n"] == 1 else ROUND2, media_type="application/rss+xml")

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        threading.Event().wait(0.1)
    assert server.started, "uvicorn did not start"
    yield base, port, app
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive(), "uvicorn thread did not shut down"


async def subscribe(ws, base: str) -> dict:
    await ws.send(json.dumps({"subscribe": [{"url": f"{base}/fixture.xml", "name": "Fixture"}]}))
    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    assert list(reply) == ["feeds"]
    return reply["feeds"][0]


async def test_subscribe_reply_then_new_article_reaches_a_real_client(live_server):
    base, port, _ = live_server
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/news") as ws:
        record = await subscribe(ws, base)
        assert record["title"] == "Fixture"
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
    msg = json.loads(raw)
    assert msg["title"] == "Breaking now"
    assert msg["feed_id"] == record["id"]
    assert msg["source"] == "Fixture"
    assert "highlighted" not in msg


async def test_backfilled_article_is_pageable_but_was_not_pushed(live_server):
    base, port, _ = live_server
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/news") as ws:
        await subscribe(ws, base)
        assert json.loads(await asyncio.wait_for(ws.recv(), timeout=15))["title"] == "Breaking now"
    import httpx

    async with httpx.AsyncClient(base_url=base) as client:
        titles = [a["title"] for a in (await client.get("/api/news")).json()["articles"]]
    assert "Backfilled" in titles and titles[0] == "Breaking now"


async def wait_for(fn, want, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn() == want:
            return want
        await asyncio.sleep(0.05)
    return fn()


@pytest.mark.parametrize("live_server", [3600], indirect=True)
async def test_closing_a_real_socket_releases_its_feeds_without_a_publish(live_server):
    base, port, app = live_server
    broadcaster = app.state.broadcaster
    ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws/news")
    record = await subscribe(ws, base)
    assert await wait_for(lambda: broadcaster.subscriber_count(record["id"]), 1, 2.0) == 1
    await ws.close()
    assert await wait_for(lambda: broadcaster.subscriber_count(record["id"]), 0, 2.0) == 0
    assert await wait_for(lambda: app.state.store.get_feed(record["id"]).enabled, False, 2.0) is False


@pytest.mark.parametrize("live_server", [3600], indirect=True)
async def test_repeated_connect_disconnect_cycles_do_not_leak(live_server):
    base, port, app = live_server
    broadcaster = app.state.broadcaster
    for _ in range(5):
        ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws/news")
        record = await subscribe(ws, base)
        assert await wait_for(lambda: broadcaster.subscriber_count(record["id"]), 1, 2.0) == 1
        await ws.close()
        assert await wait_for(lambda: broadcaster.subscriber_count(record["id"]), 0, 2.0) == 0
    assert broadcaster.session_count() == 0
```

**Adapt the store, poller, favicon and normalize tests** with these mechanical rules, file by file, deleting a test only when the behaviour it pinned no longer exists:

- Everywhere: delete `s.upsert_user(...)` lines; replace `store.subscribe("art", fid)` with nothing (feeds are enabled by default at upsert; where a test needs the feed polled, that is already true); replace `store.page_news("art", ...)` with `store.page_news(...)`; `upsert_feed(..., group=..., title_format=..., poll_interval_s=...)` loses those keywords.
- `tests/test_store_entities.py`: delete the user/subscription tests (`test_subscribe_is_idempotent`, `test_two_users_share_one_feed_row`, any `subscribers_of`/`list_feeds`/`unsubscribe` tests, token tests); delete `group`/`title_format` tests; **add**:

```python
def test_upsert_feed_canonicalises_the_url(store):
    a = store.upsert_feed("HTTPS://X.example/rss/", name="X", now=0)
    b = store.upsert_feed("https://x.example/rss", now=0)
    assert a == b
    assert store.get_feed(a).url == "https://x.example/rss"
    assert store.feed_by_url("https://X.EXAMPLE/rss/").id == a
    assert store.feed_by_url("https://y.example/rss") is None


def test_stored_name_wins_over_a_later_subscribers_name(store):
    fid = store.upsert_feed("https://x.example/rss", name="First", now=0)
    store.upsert_feed("https://x.example/rss", name="Second", now=1)
    assert store.get_feed(fid).name == "First"
    unnamed = store.upsert_feed("https://y.example/rss", now=0)
    store.upsert_feed("https://y.example/rss", name="Named later", now=1)
    assert store.get_feed(unnamed).name == "Named later"


def test_canonical_url_rules():
    from rss_ticker.store import canonical_url

    assert canonical_url("HTTPS://Feeds.Bloomberg.com/markets/news.rss/") == "https://feeds.bloomberg.com/markets/news.rss"
    assert canonical_url("https://x.example/Feed?A=1") == "https://x.example/Feed?A=1"
    assert canonical_url("https://u:P@X.example:8443/f/") == "https://u:P@x.example:8443/f"
    assert canonical_url("https://x.example/") == "https://x.example"
    assert canonical_url("https://x.example") == "https://x.example"


def test_enable_disable_and_drop(store):
    a = store.upsert_feed("https://a.example/rss", now=0)
    b = store.upsert_feed("https://b.example/rss", now=0)
    store.insert_articles(a, [NewArticle("g", "t", None, None, 1)], now=1)
    store.disable_all_feeds()
    assert not store.get_feed(a).enabled and not store.get_feed(b).enabled
    assert store.due_feeds(now=10) == []
    store.set_enabled(b, True)
    assert [f.id for f in store.due_feeds(now=10)] == [b]
    assert store.drop_disabled_feeds() == 1
    assert store.get_feed(a) is None
    assert store.get_feed_state(a) is None
    assert store.page_news(limit=10)[0] == []
    assert store.get_feed(b).id == b
```

  (`NewArticle` imported from `rss_ticker.store`.)
- `tests/test_store_paging.py`: remove the `filters` import and every include/highlight test; `page_news(limit=…)`.
- `tests/test_store_retention.py`, `tests/test_store_articles.py`, `tests/test_store_concurrency.py`, `tests/test_poller.py`, `tests/test_favicon.py`, `tests/test_normalize.py`: apply the everywhere rules; in `test_poller.py` delete the `title_format` tests; in `test_normalize.py` delete the `title_format` tests (keep the author test from Task 1).

- [ ] **Step 9: Run everything**

```bash
uv run pytest -q
uv run ruff check src tests
bash scripts/scrub-check.sh
grep -rn 'admin_key\|manifest_key\|tailscale\|token\|public_base_url\|title_format\|filters' src/ ; echo "grep exit $?"
```

Expected: all green; the grep prints nothing (exit 1). Fix what is red; the tests above are the contract.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat!: the user-agnostic pool — no users, no keys, first-frame subscribe, subscriber-counted feeds"
```

---

### Task 3: Packaging, config, docs, CI, version 8.0.0

**Files:**
- Modify: `pyproject.toml` (version), `src/rss_ticker/__init__.py`, `Makefile` (`IMAGE`, `TAG`), `docker-compose.yml`, `docker-compose.nas.yml`, `config.example.yaml`, `config.yaml` (if tracked — check `git ls-files config.yaml`; if untracked, leave it), `README.md`, `.github/workflows/ci.yml`, `docs/superpowers/specs/2026-07-21-rss-news-ticker-design.md` (header note only)
- Test: `tests/test_version.py` (already pins pyproject ↔ `__version__`)

- [ ] **Step 1: Version and image**

`pyproject.toml`: `version = "8.0.0"`. `src/rss_ticker/__init__.py`: `__version__ = "8.0.0"`. `Makefile`: `IMAGE ?= ghcr.io/artcashin/rss-feedhandler`, `TAG ?= 8.0.0`.

- [ ] **Step 2: Compose and config**

`config.example.yaml` — replace with:

```yaml
# rss-ticker: a user-agnostic shared feed pool. Clients subscribe feeds over
# the websocket; nothing here names a feed, a user or a key. Every endpoint
# is open -- run this tailnet-only (or otherwise placed), never funneled.

# Articles a feed no longer lists are kept this long after they were last seen.
retention_days: 7

# Every feed polls at this interval. Be judicious: publishers monitor, and an
# over-eager poller gets throttled or disconnected. 90 suits a pool of wires;
# 300 suits blogs and newsletters.
default_poll_interval_s: 300

max_concurrent_polls: 8

# 0.0.0.0 inside a container whose port is not published; 127.0.0.1 when a
# proxy on the same host (Tailscale Serve) is the only intended way in.
bind_host: 0.0.0.0
```

`docker-compose.yml` — replace with:

```yaml
services:
  ticker:
    image: ghcr.io/artcashin/rss-feedhandler:latest
    build: .
    restart: unless-stopped
    ports:
      # Loopback only. There is no authentication: reachability is the whole
      # access control, so the port must stay behind a proxy or an overlay
      # network that decides who can connect.
      - "127.0.0.1:8088:8088"
    environment:
      LOG_LEVEL: INFO
    volumes:
      - ticker-data:/data
      - ./config.yaml:/config/config.yaml:ro

volumes:
  ticker-data:
```

`docker-compose.nas.yml`: change the image to `ghcr.io/artcashin/rss-feedhandler:8.0.0`; in the header comment replace the identity-header sentences with: "NOTHING is published to the LAN -- Tailscale Serve is the only way in, and reachability is the only access control this server has." Keep `TS_USERSPACE=false` and its comment (a loopback-reachable app around Serve is still the wrong shape).

- [ ] **Step 3: CI**

`.github/workflows/ci.yml`: delete the `actions/setup-node@v4` step and its comment from the `test` job. Nothing else changes.

- [ ] **Step 4: README — replace the file**

```markdown
# rss-ticker

*Companion code for Adventures in OpenBB, Ep. 8: "All the News That Fits, We Print."*

A user-agnostic RSS feed pool with a live wire. Clients tell it which feeds
they want over a websocket; it polls the union, pushes every new article to
every connected client tagged by `feed_id`, and keeps cursor-paged history in
SQLite. The client filters. There are no users, no keys and no configured
feeds — the consumer is bdobb-v2's built-in **News** widget.

## Quick start

    cp config.example.yaml config.yaml   # four operational settings
    docker compose up -d

The image binds `127.0.0.1:8088` and publishes nothing else. **Every endpoint
is open**: whoever can reach the port can make this server poll a URL, read
every article and list every feed. Placement is the access control — keep it
on a private overlay (the NAS compose puts it behind a Tailscale sidecar with
Serve as the only way in) and never expose it to the public internet.

## Protocol

    WS   /ws/news          first frame subscribes; later frames replace the set
    GET  /api/news?limit=&before=&after=
    GET  /api/feeds
    GET  /api/health
    GET  /

**Subscribe** (client → server):

    {"subscribe": [{"url": "https://feeds.example/markets.rss", "name": "Markets"}, ...]}

`url` must be http(s) and at most 2048 characters; at most 200 entries. A
frame that is not that closes the socket with code 4400. `name` is used only
when the feed is new to the pool. Feeds are deduplicated by canonical URL:
scheme and host lowercased, one trailing slash stripped, nothing cleverer.

**Reply** (server → client, once per subscribe frame, in the frame's order):

    {"feeds": [{"id": 3, "url": "https://feeds.example/markets.rss", "title": "Markets", "favicon": "data:image/png;base64,..."}]}

`favicon` is the publisher's icon resolved against the feed's own host (each
Substack newsletter gets its own), or `null` until it resolves.

**Articles** (server → client, every other frame; the same shape on `/api/news`):

    {"id": 91, "feed_id": 3, "cursor": "...", "title": "...", "link": "...", "summary": "...",
     "source": "Markets", "author": null, "published_at": 1756800000, "sort_at": 1756800000}

Timestamps are epoch seconds. `sort_at` is clamped to the poll time so a
future-dated item cannot pin itself to the top or poison a reconnect gap
fill; `published_at` is the feed's own value.

**Paging:** `before` (and no cursor) walks newest-first; `after` walks
oldest-first and exists for a reconnect gap fill. `limit` is 1–200, default
50.

## Feed lifecycle

A feed is polled while at least one open socket names it. The count is in
memory: a socket closing releases its feeds, a feed at zero subscribers stops
polling at once, and the hourly retention sweep deletes feeds still at zero —
with their state and articles. Every feed starts disabled at boot; a client
reconnecting re-enables its own within seconds. The first successful poll of
a feed new to the pool caches its back catalogue without broadcasting it, so
adding a feed never floods the wire.

## Configuration

`config.yaml` holds exactly four keys — `retention_days`,
`default_poll_interval_s`, `max_concurrent_polls`, `bind_host` — and the
server refuses to start on any other key, naming it. A config from the
user-and-key era fails loudly rather than booting an empty pool. `${ENV}`
expansion is still honoured but nothing requires it.

Poll cadence is global: choose `default_poll_interval_s` for the pool as a
whole (90 s suits wires; 300 s suits blogs).

## Health and logging

`GET /api/health` returns `{status, version, sessions, feeds}` with the full
per-feed detail. `status` is `degraded` when an enabled feed has failed three
polls in a row; set `HEALTH_STRICT=1` to make that a 503 so the container's
`HEALTHCHECK` trips. uvicorn's access log is on; the poller's own log lines
reduce feed URLs to scheme and host.

## Upgrading from v8

The database migrates itself: the `users`, `subscriptions` and `filter_rules`
tables are dropped, the per-feed `group`, `title_format` and
`poll_interval_s` columns go, `articles.author` is added, and feed URLs are
canonicalised. Articles keep their history. The config does **not** migrate:
delete every key but the four above (the server names the stale ones), drop
`rss-ticker.env`, and pull `ghcr.io/artcashin/rss-feedhandler:8.0.0`. The
OpenBB Workspace widgets are gone; the consumer is bdobb-v2 v8.0.0's News
widget.

## Multi-arch builds

`make buildx` builds and pushes a `linux/amd64,linux/arm64` image to
`ghcr.io/artcashin/rss-feedhandler`. Multi-platform builds require a
`docker-container`-driver builder; the target auto-creates one named
`rss-ticker-builder` on first use.

## Development

    uv venv --python 3.12 && uv pip install -e ".[dev]"
    make test
    make lint

Docs: [base design](docs/superpowers/specs/2026-07-21-rss-news-ticker-design.md) ·
[user-agnostic rework](docs/superpowers/specs/2026-09-01-user-agnostic-rework-design.md) ·
[implementation plan](docs/superpowers/plans/2026-09-03-user-agnostic-rework.md)
```

- [ ] **Step 5: Base design header**

In `docs/superpowers/specs/2026-07-21-rss-news-ticker-design.md`, after the "Partially superseded" paragraph add one line: `**Implemented 2026-09-03** by \`docs/superpowers/plans/2026-09-03-user-agnostic-rework.md\` (version 8.0.0).`

- [ ] **Step 6: Verify and commit**

```bash
uv run pytest -q && uv run ruff check src tests && bash scripts/scrub-check.sh
docker build -t rss-ticker:plan-check . && docker run --rm -e CONFIG_PATH=/cfg/config.example.yaml -v "$PWD:/cfg:ro" rss-ticker:plan-check python -c "from rss_ticker import __version__; print(__version__)"
git add -A && git commit -m "chore!: 8.0.0 — config, compose, README and CI for the user-agnostic pool"
```

Expected: green; the container prints `8.0.0`.

---

## Final verification

```bash
uv run pytest -q
uv run ruff check src tests
bash scripts/scrub-check.sh
docker build -t rss-ticker:final .
grep -rn 'admin_key\|manifest_key\|tailscale_auth\|token\|widgets.json\|title_format' src/ config.example.yaml docker-compose.yml docker-compose.nas.yml; echo "grep exit $?"
```

The grep must print nothing (exit 1). It deliberately excludes `tests/` and `README.md`: the migration tests build a v8 schema that names the retired keys, and the README's upgrade note tells operators which keys to delete. Release (outside this plan, after review): `make buildx` pushes `ghcr.io/artcashin/rss-feedhandler:8.0.0` and `:latest`; the NAS pulls it and its `config/config.yaml` shrinks to the four keys.

## Self-review notes

- Spec coverage: protocol (T2 api + tests), subscriber counting and drop at zero (T2 broadcast/store, T2 main sweeper), boot-disabled (T2 main), favicon-on-add (T2 main/favicon, decision D), canonical URL + migration (T2 store), `author` (T1), config shrink with stale-key errors (T2 config, decision H), CORS (T2 api, decision G), version/image/UA/access log (T1 fetch, T3), deletions (T2 step 1, decision K), README/compose/CI (T3).
- Names consistent across tasks: `canonical_url`, `feed_by_url`, `set_enabled`, `disable_all_feeds`, `drop_disabled_feeds`, `page_news(limit, before, after)`, `Broadcaster.subscribe()/set_feeds/unsubscribe/subscriber_count/session_count/publish`, `parse_subscribe`, `feed_record`, `create_app(..., on_feed_added)`, `resolve_and_store`, `INVALID_SUBSCRIBE = 4400`.
- Deliberate ceiling: `drop_disabled_feeds` deletes per feed in a loop (n feeds, three statements each) — fine at pool sizes of tens.
