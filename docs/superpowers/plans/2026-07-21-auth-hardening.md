# Auth hardening implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the server to an untrusted network. Give every user a secret token, put
`/widget`, `/api/news`, `/api/feeds`, and `/ws/news` behind it, put `/widgets.json` behind
a separate `manifest_key`, make an unknown user indistinguishable from a wrong token,
redact feed URLs from non-admin callers, move the health detail behind the admin key, and
stop writing request lines that contain tokens.

**Architecture:** No new modules. `config` gains a required `manifest_key` and a required
per-user `token`, and refuses to load without either; `store` persists the token on the
`users` row; `api` grows two credential primitives (`secret_ok`, `token_ok`) and one auth
dependency (`require_user_token`) that every protected read endpoint depends on;
`widgets` publishes the token inside the iframe `endpoint` URL; the widget page reads it
back out of `location.search`. The `poller`/`api` seam is untouched — authentication
lives entirely on the `api` side of it.

**Tech Stack:** Unchanged — Python 3.12, FastAPI, uvicorn, stdlib `hmac`/`secrets`/
`sqlite3`. Tests with pytest + pytest-asyncio, plus the existing node harness for the
widget script. Lint with ruff.

**Spec:** `docs/superpowers/specs/2026-07-21-rss-news-ticker-design.md`, section
"Amendment: untrusted-network authentication (2026-07-21)"

## Global constraints

Every task's requirements implicitly include these.

- **Compare secrets on bytes, never on `str`.** `hmac.compare_digest` raises `TypeError`
  when given `str` operands containing non-ASCII characters. Every credential in this
  design is caller-controlled, so a `str` comparison is a remotely triggerable 500. All
  comparisons go through `secret_ok`, which encodes to UTF-8 first. The existing
  `tests/test_api_rest.py::test_admin_key_ok_non_ascii_does_not_raise` is the regression
  test for this and must keep passing.
- **Fail closed.** An empty or missing expected secret never matches — `compare_digest(b"",
  b"")` is `True`, so the emptiness check must come first. A user row with a NULL token
  authenticates nothing. A config missing a required secret does not start.
- **A rejection must not say why.** An unknown user and a wrong token return the same
  status, the same body, and take the same code path. A response that is only *usually*
  identical is a user-id oracle, which is step 1 of the attack chain this work exists to
  break.
- **Never log a token or a key, at any level.** Not in an exception message, not in a
  `ConfigError`. Config errors name the *user* or the *field*, never the value.
- **The WebSocket must reject before `broadcaster.subscribe`.** Subscribing first and
  closing after leaks a subscription and opens a race in which a frame can be queued for
  an unauthenticated socket.
- **Do not weaken an existing assertion to make it pass.** Several existing tests will
  start failing the moment a task lands. The fix is always to supply a valid credential
  from a fixture, or — where this work deliberately *changes* behaviour — to update the
  expectation and rename the test to say what it now pins. Never drop the assertion,
  relax a status check, or mark the test `xfail`. Task 0 lists every affected file.
- **Every task ends green.** `uv run pytest -q` and `uv run ruff check src tests` both
  clean before the commit. 177 tests pass on `feat/ticker-server` today; the count only
  goes up.
- **Copy rule (carried over):** user-visible strings — log messages and API `detail`
  values — are sentence case, no trailing period, no exclamation marks. Python exception
  messages (`raise ConfigError("public_base_url is required")`) keep the lowercase,
  no-period stdlib convention.
- **Three secrets, and they are not interchangeable.** `admin_key` gates writes and the
  health detail, and is a master credential for the per-user read endpoints.
  `manifest_key` gates `/widgets.json` and **nothing else** — it is the value pasted into
  OpenBB Workspace, so it must not confer write access. A per-user `token` gates that
  user's reads. The admin key does **not** open `/widgets.json`; a test pins that.

### The test token

Every task that needs a token in a fixture uses the same value, so it is greppable and so
a leak into rendered output is unmistakable:

```python
TOKEN = "tkn-" + "0123456789abcdef" * 3   # 52 chars, comfortably over the 32 minimum
```

---

## Task 0: Existing tests this plan will break

Not a work item — a map. Read it before starting so a `401` in a test you did not touch
reads as expected, not as a regression.

| File | Why it breaks | Repaired in |
|---|---|---|
| `tests/test_config.py` | Every fixture lacks `manifest_key`, and every `users:` fixture lacks `token`, so `load_config` now raises. `test_duplicate_user_id_is_error` in particular would raise the *token* error and fail its `match="duplicate user id"` | Task 1 |
| `tests/test_integration.py` | Module-level `CONFIG` YAML has neither → `load_config` raises in the `wiring` fixture → all 5 tests error | Task 1 |
| `tests/test_ws_live.py` | `live_server` fixture writes a config missing both → `build` raises → all 4 tests error. Then the `websockets.connect` URLs need `&token=` | Tasks 1, 2 |
| `tests/test_main.py` | `CONFIG` missing both (Task 1); `/api/news?user=art` is now `401` (Task 2); `/widgets.json` needs `manifest_key` (Task 3); `/api/health` no longer carries `feeds` (Task 4) | Tasks 1–4 |
| `tests/test_api_rest.py` | `/api/news` and `/api/feeds` GET now `401`; the store fixture creates a tokenless user; **`test_news_unknown_user_is_400` now gets `401` — a deliberate behaviour change, rename and update it, do not delete it**; `test_health_reports_feed_status_and_published_url` asserts on `feeds`, which moved behind the admin key | Tasks 2, 4 |
| `tests/test_api_ws.py` | All 5 tests connect with no token → closed `4401`. **`test_ws_rejects_unknown_user` and `test_ws_rejects_missing_user` expect `4400` and now get `4401`** — same deliberate change | Task 2 |
| `tests/test_widget_route.py` | All 7 tests `GET /widget?user=art` with no token → `401`. **`test_widget_unknown_user_is_400` now gets `401`** | Task 2 |
| `tests/test_widgets.py` | `/widgets.json` now needs `X-API-KEY: <manifest_key>`; the `CFG` needs a `manifest_key` and its `UserConfig` needs a token | Task 3 |
| `tests/test_widget_js.py` + `tests/harness/widget_prelude.js` + `tests/harness/widget_driver.js` | The fake `location.search` has no token, and the widget's fetch URLs change shape | Task 5 |

Files that need **no** change: `test_store_entities.py`, `test_store_articles.py`,
`test_store_paging.py`, `test_store_retention.py`, `test_filters.py`,
`test_normalize.py`, `test_fetch.py`, `test_poller.py`, `test_broadcast.py`.
`test_reconcile.py` constructs `UserConfig(id="art")` and `Config(...)` directly and keeps
working because `token` and `manifest_key` both get `""` defaults on their dataclasses —
it only gains new tests.

**Four status codes change on purpose.** `400 unknown user` becomes `401` on `/api/news`,
`/api/feeds` (GET) and `/widget`; WebSocket close `4400` becomes `4401`. The `400`
survives only on the admin-gated write endpoints. Any test asserting the old codes on a
read endpoint is asserting the vulnerability.

---

### Task 1: Required secrets in config, per-user tokens in the store

**Files:**
- Modify: `src/rss_ticker/config.py`
- Modify: `src/rss_ticker/store.py`
- Modify: `src/rss_ticker/reconcile.py`
- Modify: `config.example.yaml`
- Test: `tests/test_config.py`, `tests/test_store_entities.py`, `tests/test_reconcile.py`
- Fix (config fixtures only): `tests/test_integration.py`, `tests/test_ws_live.py`,
  `tests/test_main.py`

**Interfaces:**
- Consumes: `UserConfig`, `Config`, `load_config` (existing), `Store.upsert_user`
  (existing)
- Produces:
  - `Config(public_base_url: str, admin_key: str, manifest_key: str = "", retention_days: int = 7, default_poll_interval_s: int = 300, max_concurrent_polls: int = 8, users: tuple[UserConfig, ...] = ())`
  - `UserConfig(id: str, name: str | None = None, feeds: tuple[FeedConfig, ...] = (), filters: tuple[FilterConfig, ...] = (), token: str = "")`
  - `config.MIN_TOKEN_LEN: int = 32`
  - `Store.upsert_user(user_id: str, name: str | None, now: int = 0, token: str | None = None) -> None`
  - `Store.token_for(user_id: str) -> str | None`
  - `Store.users_without_tokens() -> list[str]`
  - `reconcile(store, config, now)` unchanged in signature; now writes tokens and warns
    about tokenless DB users

Both new config fields get a `""` default **on the dataclass** and are required **in
`load_config`**. That split is deliberate and matches the existing note in
`tests/test_widgets.py`: `Config` is a plain frozen dataclass with no validation of its
own, so direct construction in tests keeps working, while a `""` secret matches nothing
at request time. There is no path by which a defaulted field becomes an open door.

- [ ] **Step 1: Write the failing config tests**

Append to `tests/test_config.py`:

```python
TOKEN = "tkn-" + "0123456789abcdef" * 3


def test_manifest_key_is_loaded_and_env_expanded(tmp_path):
    p = write(tmp_path, """
public_base_url: http://x
admin_key: k
manifest_key: ${TICKER_MANIFEST_KEY}
""")
    cfg = load_config(p, {"TICKER_MANIFEST_KEY": "mk"})
    assert cfg.manifest_key == "mk"


def test_missing_manifest_key_is_error(tmp_path):
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: k\n")
    with pytest.raises(ConfigError, match="manifest_key"):
        load_config(p, {})


def test_manifest_key_must_differ_from_admin_key(tmp_path):
    # manifest_key is pasted into a third-party UI. If it is the admin key,
    # handing OpenBB a read credential also hands it write access.
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: k\nmanifest_key: k\n")
    with pytest.raises(ConfigError, match="must differ"):
        load_config(p, {})


def test_user_token_is_loaded_and_env_expanded(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: ${{TICKER_TOKEN_ART}}
""")
    cfg = load_config(p, {"TICKER_TOKEN_ART": TOKEN})
    assert cfg.users[0].token == TOKEN


def test_user_without_a_token_is_a_startup_error(tmp_path):
    p = write(tmp_path, """
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
""")
    with pytest.raises(ConfigError, match="art"):
        load_config(p, {})


def test_token_error_does_not_leak_the_value(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: shortsecret
  - id: bob
    token: {TOKEN}
""")
    with pytest.raises(ConfigError) as exc:
        load_config(p, {})
    assert "shortsecret" not in str(exc.value)
    assert "art" in str(exc.value)


def test_short_token_is_an_error(tmp_path):
    # A token arriving through an env var can be a placeholder or a truncated
    # paste; nothing else would catch it.
    p = write(tmp_path, """
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: abc
""")
    with pytest.raises(ConfigError, match="at least 32"):
        load_config(p, {})


def test_duplicate_token_across_users_is_an_error(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: {TOKEN}
  - id: bob
    token: {TOKEN}
""")
    with pytest.raises(ConfigError, match="duplicate token"):
        load_config(p, {})
```

Then repair the six existing fixtures in that file: add `manifest_key: mk` to every one
(including `test_defaults_applied`, `test_unset_env_var_is_error`, and
`test_missing_public_base_url_is_error`), and add a `token:` line to the three that
declare users — `test_loads_full_config`, `test_bad_filter_action_is_error`, and
`test_duplicate_user_id_is_error`.

`test_duplicate_user_id_is_error` matters most: both `{id: art}` entries need a token, and
they must be **different** tokens, or the duplicate-token check fires first and the
`match="duplicate user id"` assertion passes or fails for the wrong reason.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'manifest_key'`, and
the required-field tests fail with `DID NOT RAISE`.

- [ ] **Step 3: Add the secrets to `config.py`**

Add the constant beside the other module-level ones:

```python
MIN_TOKEN_LEN = 32
```

Add `manifest_key` to `Config` — after `admin_key`, with a default, so the keyword-only
construction used throughout the tests keeps working:

```python
@dataclass(frozen=True)
class Config:
    public_base_url: str
    admin_key: str
    manifest_key: str = ""
    retention_days: int = 7
    default_poll_interval_s: int = 300
    max_concurrent_polls: int = 8
    users: tuple[UserConfig, ...] = ()
```

Add `token` to `UserConfig` — **last, with a default**, so the direct-construction call
sites in `tests/test_reconcile.py` and `tests/test_widgets.py` keep working:

```python
@dataclass(frozen=True)
class UserConfig:
    id: str
    name: str | None = None
    feeds: tuple[FeedConfig, ...] = ()
    filters: tuple[FilterConfig, ...] = ()
    token: str = ""
```

Validate the token inside `_user`, after the id check and before constructing:

```python
    token = raw.get("token")
    if not token:
        raise ConfigError(
            f"user {uid!r} has no token; generate one with "
            "python -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    if not isinstance(token, str) or len(token) < MIN_TOKEN_LEN:
        raise ConfigError(
            f"token for user {uid!r} must be a string of at least {MIN_TOKEN_LEN} characters"
        )
    return UserConfig(
        id=uid,
        name=raw.get("name"),
        feeds=tuple(_feed(f) for f in raw.get("feeds") or []),
        filters=tuple(_filter(f) for f in raw.get("filters") or []),
        token=token,
    )
```

The error text names the user and never interpolates the token.

In `load_config`, require `manifest_key` beside the other required fields and add the
duplicate-token check beside the duplicate-id check:

```python
    if not raw.get("admin_key"):
        raise ConfigError("admin_key is required")
    if not raw.get("manifest_key"):
        raise ConfigError(
            "manifest_key is required; it gates widgets.json and is the value pasted "
            "into OpenBB Workspace, so it must not be the admin key"
        )
    if raw["manifest_key"] == raw["admin_key"]:
        raise ConfigError("manifest_key must differ from admin_key")

    users = tuple(_user(u) for u in raw.get("users") or [])
    seen: set[str] = set()
    seen_tokens: set[str] = set()
    for u in users:
        if u.id in seen:
            raise ConfigError(f"duplicate user id {u.id!r}")
        seen.add(u.id)
        if u.token in seen_tokens:
            raise ConfigError(f"duplicate token configured for user {u.id!r}")
        seen_tokens.add(u.token)
```

and pass `manifest_key=raw["manifest_key"]` into the returned `Config`.

The id check runs before the token check for a given user, so
`test_duplicate_user_id_is_error` keeps reporting the id collision.

- [ ] **Step 4: Run the config tests**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS — 14 passed

- [ ] **Step 5: Write the failing store tests**

Append to `tests/test_store_entities.py`:

```python
TOKEN = "tkn-" + "0123456789abcdef" * 3


def test_token_round_trips(store):
    store.upsert_user("art", "Art", token=TOKEN)
    assert store.token_for("art") == TOKEN


def test_token_for_unknown_user_is_none(store):
    assert store.token_for("nobody") is None


def test_user_created_without_a_token_has_none(store):
    store.upsert_user("art", "Art")
    assert store.token_for("art") is None


def test_upsert_without_a_token_preserves_the_existing_one(store):
    store.upsert_user("art", "Art", token=TOKEN)
    store.upsert_user("art", "Art renamed")
    assert store.token_for("art") == TOKEN


def test_upsert_with_a_new_token_rotates_it(store):
    store.upsert_user("art", "Art", token=TOKEN)
    store.upsert_user("art", "Art", token="rotated-" + TOKEN)
    assert store.token_for("art") == "rotated-" + TOKEN


def test_users_without_tokens_lists_them(store):
    store.upsert_user("art", None, token=TOKEN)
    store.upsert_user("bob", None)
    assert store.users_without_tokens() == ["bob"]


def test_a_database_predating_the_token_column_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    old = Store(path)
    old.upsert_user("art", "Art")
    old.db.execute("ALTER TABLE users DROP COLUMN token")
    old.db.commit()
    old.close()

    migrated = Store(path)
    try:
        assert migrated.user_exists("art") is True
        assert migrated.token_for("art") is None
        assert migrated.users_without_tokens() == ["art"]
    finally:
        migrated.close()
```

`ALTER TABLE ... DROP COLUMN` needs SQLite 3.35+ (2021); the `python:3.12-slim` base and
any current macOS are well past it. If the host SQLite is older, build the pre-migration
database with a raw `CREATE TABLE users (...)` through `sqlite3.connect` instead — which
is what `tests/test_migration.py` does in Task 6.

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/test_store_entities.py -v`
Expected: FAIL — `TypeError: upsert_user() got an unexpected keyword argument 'token'`

- [ ] **Step 7: Add the column, the migration, and the accessors to `store.py`**

In `SCHEMA`, add the column to the `users` table:

```sql
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT,
    created_at INTEGER NOT NULL DEFAULT 0,
    token TEXT
);
```

In `Store._migrate`, **before** the existing `articles` block:

```python
        user_columns = {
            r["name"] for r in self.db.execute("PRAGMA table_info(users)").fetchall()
        }
        if "token" not in user_columns:
            # Existing deployments have users and no tokens. The column arrives
            # NULL, and a NULL token authenticates nothing, so those accounts are
            # closed until boot reconciliation writes the configured value.
            self.db.execute("ALTER TABLE users ADD COLUMN token TEXT")
```

Replace `upsert_user` and add the two accessors:

```python
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

    def token_for(self, user_id: str) -> str | None:
        row = self.db.execute(
            "SELECT token FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return row["token"] if row else None

    def users_without_tokens(self) -> list[str]:
        rows = self.db.execute(
            "SELECT id FROM users WHERE token IS NULL OR token = '' ORDER BY id"
        ).fetchall()
        return [r["id"] for r in rows]
```

`COALESCE(excluded.token, users.token)` is what makes rotation work while leaving the
existing `upsert_user("art", "Art")` call sites harmless.

- [ ] **Step 8: Run the store tests**

Run: `uv run pytest tests/test_store_entities.py -v`
Expected: PASS — 16 passed

- [ ] **Step 9: Write the failing reconcile test**

Append to `tests/test_reconcile.py`:

```python
TOKEN = "tkn-" + "0123456789abcdef" * 3


def test_token_is_persisted(store):
    reconcile(store, cfg([UserConfig(id="art", token=TOKEN)]), now=100)
    assert store.token_for("art") == TOKEN


def test_rotated_token_overwrites_the_stored_one(store):
    reconcile(store, cfg([UserConfig(id="art", token=TOKEN)]), now=100)
    reconcile(store, cfg([UserConfig(id="art", token="rotated-" + TOKEN)]), now=200)
    assert store.token_for("art") == "rotated-" + TOKEN


def test_tokenless_database_user_is_warned_about(store, caplog):
    store.upsert_user("orphan", None)
    with caplog.at_level("WARNING"):
        reconcile(store, cfg([UserConfig(id="art", token=TOKEN)]), now=100)
    assert "orphan" in caplog.text


def test_the_warning_does_not_contain_a_token(store, caplog):
    with caplog.at_level("WARNING"):
        reconcile(store, cfg([UserConfig(id="art", token=TOKEN)]), now=100)
    assert TOKEN not in caplog.text
```

- [ ] **Step 10: Run test to verify it fails**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: FAIL — `assert None == 'tkn-...'`

- [ ] **Step 11: Write tokens in `reconcile.py`**

```python
def reconcile(store: Store, config: Config, now: int) -> None:
    """Additively apply config to the database. Never deletes."""
    for user in config.users:
        store.upsert_user(user.id, user.name, now=now, token=user.token or None)
        ...

    orphans = store.users_without_tokens()
    if orphans:
        log.warning(
            "Users in the database with no token cannot authenticate: %s",
            ", ".join(orphans),
        )
```

The warning names ids only. A user who is in the database but no longer in `config.yaml`
is otherwise a silently-dead account: every request they make returns `401` with nothing
in the log explaining why.

- [ ] **Step 12: Repair the config fixtures in the three integration-style test files**

These do not test auth — they merely stop loading. Add `manifest_key` and a token to each.

`tests/test_integration.py`, module-level `CONFIG`:

```python
TOKEN = "tkn-" + "0123456789abcdef" * 3

CONFIG = f"""
public_base_url: http://localhost:8088
admin_key: k
manifest_key: mk
retention_days: 7
default_poll_interval_s: 1
users:
  - id: art
    token: {TOKEN}
    feeds:
      - {{url: "https://live.example/rss", name: Live}}
      - {{url: "https://dead.example/rss", name: Dead}}
"""
```

`tests/test_ws_live.py`, inside the `live_server` fixture's `cfg.write_text(...)`: add
`manifest_key: mk` at top level and `    token: {TOKEN}` under `- id: art`, with `TOKEN`
defined at module level.

`tests/test_main.py`, module-level `CONFIG`: same, converted to an f-string with doubled
braces on the feed and filter mappings. Its `admin_key` is `test-key`; give it
`manifest_key: manifest-key` so the two are distinct.

- [ ] **Step 13: Run the whole suite**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: PASS — 196 passed. Nothing is behind a token yet; the config and store simply
now carry the secrets.

- [ ] **Step 14: Update `config.example.yaml`**

```yaml
public_base_url: https://ticker.example.net

# Gates writes on /api/feeds and the per-feed detail on /api/health.
# Held by the operator only.
admin_key: ${TICKER_ADMIN_KEY}

# Gates /widgets.json and nothing else. This is the value you paste into OpenBB
# Workspace, so it must NOT be the admin key -- a leak there must not confer
# write access. Startup fails if the two are equal.
manifest_key: ${TICKER_MANIFEST_KEY}

retention_days: 7
default_poll_interval_s: 300
max_concurrent_polls: 8

# Every user needs a token, minimum 32 characters. Generate one per user with:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
# Keep it in the environment, never in this file. A user with no token is a
# startup error, not an open account.
users:
  - id: art
    name: Art
    token: ${TICKER_TOKEN_ART}
    feeds:
      - {url: "https://feeds.reuters.com/reuters/businessNews", name: Reuters Business}
      - {url: "https://www.ft.com/rss/home", name: FT, poll_interval_s: 600}
    filters:
      - {pattern: nvidia, action: highlight}
```

`public_base_url` becomes `https` because the token travels in the URL and plain HTTP
publishes it to the network. See Task 6.

- [ ] **Step 15: Commit**

```bash
git add src/rss_ticker/config.py src/rss_ticker/store.py src/rss_ticker/reconcile.py \
        config.example.yaml tests/
git commit -m "feat: required manifest key and per-user tokens in config and store"
```

---

### Task 2: Auth primitives and the protected read endpoints

**Files:**
- Modify: `src/rss_ticker/api.py`
- Test: `tests/test_api_rest.py`, `tests/test_api_ws.py`, `tests/test_widget_route.py`
- Fix: `tests/test_ws_live.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: `Store.token_for`, `Store.user_exists` (Task 1), `Config.admin_key`
- Produces:
  - `secret_ok(provided: str | None, expected: str | None) -> bool`
  - `token_ok(provided: str | None, expected: str | None) -> bool` — constant-work
  - `admin_key_ok(provided: str | None, expected: str) -> bool` — kept, now delegating
  - `bearer_token(authorization: str | None) -> str | None`
  - `INVALID_CREDENTIALS: str = "Invalid credentials"`
  - request-scoped dependencies inside `create_app`: `is_admin() -> bool`,
    `require_admin() -> None`, `require_user(user: str) -> str`,
    `require_user_token() -> str`
  - `/api/news`, `/api/feeds` (GET), `/widget`, `/ws/news` all requiring `(user, token)`

- [ ] **Step 1: Write the failing REST tests**

In `tests/test_api_rest.py`, add the token to the fixtures at the top:

```python
TOKEN = "tkn-" + "0123456789abcdef" * 3
CFG = Config(
    public_base_url="http://nas.local:8088", admin_key="s3cret", manifest_key="mk"
)
AUTH = {"user": "art", "token": TOKEN}
BEARER = {"Authorization": f"Bearer {TOKEN}"}
ADMIN = {"X-Admin-Key": "s3cret"}


@pytest.fixture
def store():
    s = Store(":memory:")
    s.upsert_user("art", "Art", token=TOKEN)
    yield s
    s.close()
```

Then update every existing `params={"user": "art", ...}` call in this file to include the
token — the mechanical form is `params={**AUTH, "limit": 2}` — and switch the write tests
to `headers=ADMIN`. `test_news_requires_user` (422), `test_news_limit_is_capped` (422) and
`test_news_bad_cursor_is_400` keep their current expectations, because the token is valid
in each.

**`test_news_unknown_user_is_400` is replaced, not deleted.** It currently pins the
behaviour this task removes. Rename and invert it:

```python
def test_unknown_user_and_bad_token_are_indistinguishable(client, store):
    # Was test_news_unknown_user_is_400. A 400 here is a user-id oracle, and
    # enumerating user ids is step 1 of the chain this work exists to break.
    seed(store, 1)
    unknown = client.get("/api/news", params={"user": "nobody", "token": TOKEN})
    wrong = client.get("/api/news", params={"user": "art", "token": "wrong-" + TOKEN})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json() == {"detail": "Invalid credentials"}


def test_unknown_user_without_a_token_is_also_401(client):
    r = client.get("/api/news", params={"user": "nobody"})
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid credentials"}
```

Now append the rest:

```python
def test_news_without_a_token_is_401(client, store):
    seed(store, 1)
    assert client.get("/api/news", params={"user": "art"}).status_code == 401


def test_news_accepts_a_bearer_header(client, store):
    seed(store, 1)
    assert client.get("/api/news", params={"user": "art"}, headers=BEARER).status_code == 200


def test_bearer_header_wins_over_a_bad_query_param(client, store):
    seed(store, 1)
    r = client.get("/api/news", params={"user": "art", "token": "junk"}, headers=BEARER)
    assert r.status_code == 200


def test_news_missing_user_is_still_422(client):
    # A request with no `user` at all reveals nothing about which users exist,
    # so this one keeps its distinct code.
    assert client.get("/api/news", params={"token": TOKEN}).status_code == 422


def test_admin_key_alone_satisfies_a_read_endpoint(client, store):
    seed(store, 1)
    assert client.get("/api/news", params={"user": "art"}, headers=ADMIN).status_code == 200


def test_admin_key_with_an_unknown_user_is_401(client):
    assert client.get("/api/news", params={"user": "nobody"}, headers=ADMIN).status_code == 401


def test_feeds_without_a_token_is_401(client, store):
    seed(store)
    assert client.get("/api/feeds", params={"user": "art"}).status_code == 401


def test_a_users_token_does_not_unlock_another_user(client, store):
    store.upsert_user("bob", None, token="bobs-" + TOKEN)
    seed(store)
    assert client.get("/api/news", params={"user": "bob", "token": TOKEN}).status_code == 401


def test_write_endpoints_keep_400_for_an_unknown_user(client):
    # The admin caller can already list every user, so there is nothing to leak.
    r = client.post(
        "/api/feeds", json={"user": "nobody", "url": "https://n.example/rss"},
        headers=ADMIN,
    )
    assert r.status_code == 400


def test_secret_ok_rejects_an_empty_expected_secret():
    # compare_digest(b"", b"") is True, so the emptiness guard must come first:
    # a user row whose token is NULL or "" must authenticate nothing.
    assert secret_ok("", "") is False
    assert secret_ok(None, None) is False
    assert secret_ok("anything", None) is False
    assert secret_ok("anything", "") is False


def test_secret_ok_non_ascii_does_not_raise():
    assert secret_ok("\xe9", "k") is False


def test_token_ok_rejects_a_missing_expected_token():
    assert token_ok(TOKEN, None) is False
    assert token_ok(TOKEN, "") is False
    assert token_ok(None, TOKEN) is False
    assert token_ok(TOKEN, TOKEN) is True


def test_token_ok_compares_even_when_there_is_no_stored_token(monkeypatch):
    # Returning early on a missing expected token makes an unknown user
    # measurably faster than a wrong token -- the same oracle with a stopwatch.
    calls = []
    real = api_mod.hmac.compare_digest
    monkeypatch.setattr(
        api_mod.hmac, "compare_digest",
        lambda a, b: calls.append(1) or real(a, b),
    )
    token_ok(TOKEN, None)
    token_ok(TOKEN, "other-" + TOKEN)
    assert len(calls) == 2


def test_bearer_token_parsing():
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer abc") == "abc"
    assert bearer_token("Bearer  abc ") == "abc"
    assert bearer_token("Basic abc") is None
    assert bearer_token("Bearer") is None
    assert bearer_token("Bearer   ") is None
    assert bearer_token(None) is None
```

Update the imports to `from rss_ticker import api as api_mod` and
`from rss_ticker.api import admin_key_ok, bearer_token, create_app, secret_ok, token_ok`.

- [ ] **Step 2: Write the failing WebSocket tests**

In `tests/test_api_ws.py`, seed the token and point the passing tests at an authenticated
URL:

```python
TOKEN = "tkn-" + "0123456789abcdef" * 3
GOOD = f"/ws/news?user=art&token={TOKEN}"
CFG = Config(public_base_url="http://x", admin_key="k", manifest_key="mk")


@pytest.fixture
def store():
    s = Store(":memory:")
    s.upsert_user("art", None, token=TOKEN)
    yield s
    s.close()
```

Change `test_ws_accepts_known_user_and_registers_subscriber` and
`test_ws_disconnect_removes_subscriber` to connect to `GOOD`, keeping their comments.

**`test_ws_rejects_unknown_user` and `test_ws_rejects_missing_user` change their expected
code from 4400 to 4401** — rename the first so the file says why:

```python
def test_ws_rejects_unknown_user_with_the_same_code_as_a_bad_token(client):
    # Was asserting 4400. A distinct code for "no such user" is the same
    # enumeration oracle the REST endpoints just closed.
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/news?user=nobody&token={TOKEN}") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_ws_rejects_missing_user(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/news") as ws:
            ws.receive_json()
    assert exc.value.code == 4401
```

`test_ws_unknown_user_is_never_registered` keeps its `subscriber_count` assertion
unchanged. Append:

```python
def test_ws_rejects_a_missing_token(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/news?user=art") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_ws_rejects_a_wrong_token(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/news?user=art&token=wrong-{TOKEN}") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_ws_bad_token_never_registers_a_subscriber(client, broadcaster):
    # Subscribing first and closing after leaks a subscription and lets a frame
    # be queued for an unauthenticated socket in the race window.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/news?user=art&token=nope") as ws:
            ws.receive_json()
    assert broadcaster.subscriber_count("art") == 0


def test_ws_rejects_another_users_token(client, store, broadcaster):
    store.upsert_user("bob", None, token="bobs-" + TOKEN)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/news?user=bob&token={TOKEN}") as ws:
            ws.receive_json()
    assert exc.value.code == 4401
    assert broadcaster.subscriber_count("bob") == 0
```

- [ ] **Step 3: Write the failing widget-route tests**

In `tests/test_widget_route.py`, seed the token and pass it everywhere:

```python
TOKEN = "tkn-" + "0123456789abcdef" * 3
AUTH = {"user": "art", "token": TOKEN}
CFG = Config(
    public_base_url="http://nas.local:8088", admin_key="k", manifest_key="mk"
)


@pytest.fixture
def client():
    store = Store(":memory:")
    store.upsert_user("art", None, token=TOKEN)
    yield TestClient(create_app(CFG, store, Broadcaster(store)))
    store.close()
```

Replace every `params={"user": "art"}` with `params=AUTH`. `test_widget_requires_user`
(422) is unchanged. **`test_widget_unknown_user_is_400` becomes:**

```python
def test_widget_unknown_user_is_401(client):
    # Was 400. Same enumeration oracle as /api/news.
    assert client.get("/widget", params={"user": "nobody"}).status_code == 401
```

Append:

```python
def test_widget_without_a_token_is_401(client):
    assert client.get("/widget", params={"user": "art"}).status_code == 401


def test_widget_with_a_wrong_token_is_401(client):
    assert client.get("/widget", params={"user": "art", "token": "wrong"}).status_code == 401


def test_widget_response_suppresses_referrer_and_caching(client):
    # The token is in this document's URL: it must not travel in a Referer
    # header, and a shared cache must not keep a copy of the page.
    r = client.get("/widget", params=AUTH)
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "no-store" in r.headers["cache-control"]
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api_rest.py tests/test_api_ws.py tests/test_widget_route.py -v`
Expected: FAIL — `ImportError: cannot import name 'token_ok'`, and once that is stubbed,
`assert 200 == 401` on the negative tests and `assert 400 == 401` on the three renamed
ones.

- [ ] **Step 5: Add the credential primitives to `api.py`**

Add `import secrets` to the imports, then replace `admin_key_ok`:

```python
INVALID_CREDENTIALS = "Invalid credentials"

# Compared against when a user has no stored token, so that path does the same
# work as a wrong-token path. Random per process; never returned or logged.
_DECOY_TOKEN = secrets.token_urlsafe(32)


def secret_ok(provided: str | None, expected: str | None) -> bool:
    """Constant-time secret check that never raises on caller-controlled input.

    Both operands are encoded before comparison: hmac.compare_digest raises
    TypeError on str operands containing non-ASCII characters, and every
    credential here arrives from the network. An empty or missing expected
    value never matches -- compare_digest(b"", b"") is True, so a user row with
    a NULL token would otherwise be an open account.
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def token_ok(provided: str | None, expected: str | None) -> bool:
    """Constant-work token check.

    `expected` is None for a user that does not exist. Returning early there
    would make an unknown user measurably faster than a wrong token, which is
    the user-id oracle this endpoint exists to close, wearing a stopwatch.
    Compare against a decoy of the same shape instead and discard the result.
    """
    matched = secret_ok(provided, expected or _DECOY_TOKEN)
    # `bool(expected)`, not `expected is not None`: the decoy is substituted for
    # any falsy expected, so an `is not None` gate let a user row whose token is
    # the empty string authenticate anyone who knew the decoy. Corrected during
    # Task 2 review; pinned by test_an_empty_stored_token_rejects_even_the_decoy_value.
    return matched and bool(expected)


def admin_key_ok(provided: str | None, expected: str) -> bool:
    return secret_ok(provided, expected)
```

`admin_key_ok` is kept rather than renamed at the call sites so the five existing
`test_admin_key_ok_*` tests — including the non-ASCII regression — stay untouched and keep
pinning the byte comparison.

- [ ] **Step 6: Replace the dependencies inside `create_app`**

```python
    def is_admin(x_admin_key: str | None = Header(default=None)) -> bool:
        return secret_ok(x_admin_key, config.admin_key)

    def require_admin(admin: bool = Depends(is_admin)) -> None:
        if not admin:
            raise HTTPException(status_code=401, detail="Admin key required")

    def require_user(user: str) -> str:
        # Write endpoints only. Their caller holds the admin key and can already
        # list every user, so naming an unknown one leaks nothing.
        if not store.user_exists(user):
            raise HTTPException(status_code=400, detail=f"Request from unknown user {user}")
        return user

    def require_user_token(
        user: str = Query(...),
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
        admin: bool = Depends(is_admin),
    ) -> str:
        # Both lookups run on every path so that an unknown user and a wrong
        # token cost the same. The rejection is identical in status and body.
        exists = store.user_exists(user)
        expected = store.token_for(user)
        if admin:
            if not exists:
                raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)
            return user
        if not token_ok(bearer_token(authorization) or token, expected):
            raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)
        return user
```

- [ ] **Step 7: Put the read endpoints behind it**

`/api/news` — the `user`/`token` query params now come from the dependency, so drop the
in-body `require_user(user)`:

```python
    @app.get("/api/news")
    def news(
        user: str = Depends(require_user_token),
        limit: int = Query(50, ge=1, le=200),
        before: str | None = Query(None),
        after: str | None = Query(None),
    ) -> dict:
        if before and after:
            raise HTTPException(status_code=400, detail="Pass before or after, not both")
        ...
```

`/api/feeds` GET (redaction lands in Task 4):

```python
    @app.get("/api/feeds")
    def list_feeds(user: str = Depends(require_user_token)) -> dict:
```

`/widget`:

```python
    @app.get("/widget", response_class=HTMLResponse)
    def widget(user: str = Depends(require_user_token)) -> HTMLResponse:
        return HTMLResponse(
            (STATIC / "widget.html").read_text(),
            headers={
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            },
        )
```

`/ws/news` — one rejection for both cases, before `broadcaster.subscribe`:

```python
    @app.websocket("/ws/news")
    async def ws_news(websocket: WebSocket) -> None:
        user = websocket.query_params.get("user")
        token = websocket.query_params.get("token")
        await websocket.accept()
        if not user or not token_ok(token, store.token_for(user)):
            # One code and one reason for "no such user" and "wrong token":
            # a distinct 4400 would be the enumeration oracle over again.
            # Reject before subscribing -- a subscription registered here and
            # torn down on the next line can still be handed a frame.
            await websocket.close(code=4401, reason="Invalid credentials")
            return

        sub = broadcaster.subscribe(user)
        ...
```

The socket is accepted before being closed, as it already was, so the client sees a close
code rather than a bare handshake failure. `Authorization` is deliberately not consulted
here — constraint 2 in the spec says a browser cannot set it on a `WebSocket`, so
supporting it would be dead code implying a capability the widget does not have.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api_rest.py tests/test_api_ws.py tests/test_widget_route.py -v`
Expected: PASS — 36, 10 and 11 tests respectively.

- [ ] **Step 9: Repair the two live-wiring test files**

`tests/test_ws_live.py`: append `&token={TOKEN}` to all four `websockets.connect` URLs and
to the `httpx` `/api/news` call in
`test_backfilled_article_is_pageable_but_was_not_pushed`:

```python
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/ws/news?user=art&token={TOKEN}"
    ) as ws:
```

```python
        body = (
            await client.get("/api/news", params={"user": "art", "token": TOKEN})
        ).json()
```

`tests/test_main.py`: `test_build_reconciles_config_and_serves` passes the token on
`/api/news`; `test_database_persists_across_builds` passes it on `/api/feeds`. Both keep
`X-Admin-Key` on the `POST`. The `/api/health` and `/widgets.json` assertions in that file
are still valid until Tasks 3 and 4.

- [ ] **Step 10: Run the whole suite**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: PASS — 214 passed

- [ ] **Step 11: Commit**

```bash
git add src/rss_ticker/api.py tests/
git commit -m "feat: token auth on reads, with unknown user indistinguishable from bad token"
```

---

### Task 3: `/widgets.json` behind `manifest_key`, with the token in the endpoint URL

**Files:**
- Modify: `src/rss_ticker/api.py`
- Modify: `src/rss_ticker/widgets.py`
- Test: `tests/test_widgets.py`
- Fix: `tests/test_main.py`

**Interfaces:**
- Consumes: `Config.manifest_key`, `UserConfig.token` (Task 1), `secret_ok` (Task 2)
- Produces:
  - `require_manifest_key(x_api_key: str | None = Header(default=None)) -> None` inside
    `create_app`
  - `render_widgets(config: Config) -> dict` with `endpoint` of the form
    `<base>/widget?user=<id>&token=<token>`, both percent-encoded

- [ ] **Step 1: Write the failing tests**

In `tests/test_widgets.py`, give the config a manifest key and its user a token:

```python
TOKEN = "tkn-" + "0123456789abcdef" * 3
MANIFEST = {"X-API-KEY": "mk"}

CFG = Config(
    public_base_url="http://nas.local:8088",
    admin_key="s3cret",
    manifest_key="mk",
    users=(UserConfig(id="art", name="Art", token=TOKEN),),
)
```

Change `test_widgets_json_is_served` to send `headers=MANIFEST`, and give the two
locally-constructed `Config` objects in `test_no_users_yields_empty_manifest` and
`test_endpoint_url_encodes_unsafe_user_id` a `manifest_key="mk"`. Then append:

```python
def test_widgets_json_without_the_api_key_is_401(client):
    # The manifest contains every user's token. Unauthenticated, it is the
    # directory an attacker walks: user ids, then feeds, then headlines.
    assert client.get("/widgets.json").status_code == 401


def test_widgets_json_rejects_a_wrong_api_key(client):
    assert client.get("/widgets.json", headers={"X-API-KEY": "wrong"}).status_code == 401


def test_the_admin_key_does_not_open_widgets_json(client):
    # manifest_key is pasted into OpenBB Workspace; admin_key is not. Accepting
    # either here would erase the separation that makes that safe.
    assert client.get("/widgets.json", headers={"X-API-KEY": "s3cret"}).status_code == 401
    assert client.get("/widgets.json", headers={"X-Admin-Key": "s3cret"}).status_code == 401


def test_widgets_json_accepts_the_manifest_key(client):
    r = client.get("/widgets.json", headers=MANIFEST)
    assert r.status_code == 200
    assert "news_window_art" in r.json()


def test_endpoint_carries_the_token():
    assert f"token={TOKEN}" in render_widgets(CFG)["news_window_art"]["endpoint"]


def test_endpoint_url_encodes_an_unsafe_token():
    cfg = Config(
        public_base_url="http://nas.local:8088",
        admin_key="k",
        manifest_key="mk",
        users=(UserConfig(id="art", token="a b&c=d"),),
    )
    endpoint = render_widgets(cfg)["news_window_art"]["endpoint"]
    assert "token=a%20b%26c%3Dd" in endpoint
    assert endpoint.count("&") == 1


def test_every_user_gets_their_own_token_in_their_own_endpoint():
    cfg = Config(
        public_base_url="http://x",
        admin_key="k",
        manifest_key="mk",
        users=(
            UserConfig(id="art", token="art-" + TOKEN),
            UserConfig(id="bob", token="bob-" + TOKEN),
        ),
    )
    manifest = render_widgets(cfg)
    assert "token=art-" in manifest["news_window_art"]["endpoint"]
    assert "token=bob-" not in manifest["news_window_art"]["endpoint"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_widgets.py -v`
Expected: FAIL — `assert 200 == 401` on the unauthenticated test, and
`assert 'token=tkn-...' in 'http://nas.local:8088/widget?user=art'`.

- [ ] **Step 3: Gate the route in `api.py`**

Add the dependency beside the others in `create_app`:

```python
    def require_manifest_key(x_api_key: str | None = Header(default=None)) -> None:
        # X-API-KEY is OpenBB Workspace's documented convention for a custom
        # backend's key; its value here is manifest_key, deliberately NOT
        # admin_key -- this is the secret that leaves the operator's control.
        # Workspace fetches widgets.json itself, so unlike the iframe document
        # load, the header does arrive.
        if not secret_ok(x_api_key, config.manifest_key):
            raise HTTPException(status_code=401, detail="API key required")
```

and apply it:

```python
    @app.get("/widgets.json", dependencies=[Depends(require_manifest_key)])
    def widgets_manifest() -> dict:
        from .widgets import render_widgets

        return render_widgets(config)
```

This route deliberately does **not** accept the admin key. It is the one place the
master-credential rule does not apply, because the point of `manifest_key` is that the
manifest can be fetched by something holding nothing else.

- [ ] **Step 4: Put the token in the endpoint in `widgets.py`**

```python
            manifest[f"{prefix}_{user.id}"] = {
                "name": f"{label} ({user.name or user.id})",
                "description": description,
                "category": "News",
                "type": "iframe",
                "endpoint": (
                    f"{config.public_base_url}/widget"
                    f"?user={quote(user.id, safe='')}"
                    f"&token={quote(user.token, safe='')}"
                ),
                "gridData": {"w": 40, "h": height},
                "source": "RSS",
            }
```

`safe=''` on the token matters as much as on the id: an unencoded `&` or `=` in a token
would split the query string and silently truncate the credential.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_widgets.py -v`
Expected: PASS — 14 passed

- [ ] **Step 6: Repair `tests/test_main.py`**

`test_build_reconciles_config_and_serves` fetches `/widgets.json`; add
`headers={"X-API-KEY": "manifest-key"}` to match that file's `CONFIG`. Add one test to
the same file while you are there:

```python
def test_widgets_json_publishes_a_working_token(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    app = build(cfg, str(tmp_path / "t.db"), env={})
    with TestClient(app) as client:
        manifest = client.get(
            "/widgets.json", headers={"X-API-KEY": "manifest-key"}
        ).json()
        endpoint = manifest["news_window_art"]["endpoint"]
        query = endpoint.split("?", 1)[1]
        assert client.get(f"/widget?{query}").status_code == 200
```

That is the closest an automated test gets to acceptance criterion 13: the credential the
manifest publishes is the credential `/widget` accepts, end to end through the real
wiring.

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: PASS — 221 passed

- [ ] **Step 8: Commit**

```bash
git add src/rss_ticker/api.py src/rss_ticker/widgets.py tests/
git commit -m "feat: gate widgets.json on manifest_key and publish per-user tokens"
```

---

### Task 4: Feed-URL redaction, health detail behind the admin key, no request log

**Files:**
- Modify: `src/rss_ticker/api.py`
- Modify: `src/rss_ticker/main.py`
- Test: `tests/test_api_rest.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: `is_admin` (Task 2), `Store.all_feed_status` (existing)
- Produces:
  - `redact_feed_url(url: str) -> str` in `api.py`
  - `api.DEGRADED_AFTER_FAILURES: int = 3`
  - `/api/feeds` returning redacted URLs to token callers, full URLs to admin callers
  - `/api/health` returning `{"status", "version", "public_base_url"}` publicly and
    `feeds` additionally to admin
  - `uvicorn.run(..., access_log=False)` in `main.main()`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_rest.py`:

```python
def test_feeds_are_redacted_to_scheme_and_host_for_a_token_caller(client, store):
    fid = store.upsert_feed("https://api.vendor.example/rss?apikey=SUPERSECRET", now=0)
    store.subscribe("art", fid)
    urls = [f["url"] for f in client.get("/api/feeds", params=AUTH).json()["feeds"]]
    assert "https://api.vendor.example" in urls
    assert not any("SUPERSECRET" in u for u in urls)


def test_feeds_redaction_drops_userinfo(client, store):
    # urlsplit().netloc keeps `user:password@`; only .hostname drops it, and
    # feed URLs of this shape are exactly how vendors ship a token.
    fid = store.upsert_feed("https://user:SUPERSECRET@host.example/rss", now=0)
    store.subscribe("art", fid)
    body = client.get("/api/feeds", params=AUTH).json()
    assert body["feeds"][0]["url"] == "https://host.example"


def test_feeds_redaction_keeps_a_nondefault_port(client, store):
    fid = store.upsert_feed("http://host.example:9000/rss?k=v", now=0)
    store.subscribe("art", fid)
    body = client.get("/api/feeds", params=AUTH).json()
    assert body["feeds"][0]["url"] == "http://host.example:9000"


def test_feeds_are_full_for_an_admin_caller(client, store):
    fid = store.upsert_feed("https://api.vendor.example/rss?apikey=SUPERSECRET", now=0)
    store.subscribe("art", fid)
    body = client.get("/api/feeds", params={"user": "art"}, headers=ADMIN).json()
    assert body["feeds"][0]["url"].endswith("apikey=SUPERSECRET")


def test_redact_feed_url_handles_garbage():
    assert redact_feed_url("not a url") == "(redacted)"
    assert redact_feed_url("") == "(redacted)"


def test_public_health_keeps_the_published_url_but_drops_feed_detail(client, store):
    # public_base_url is not a secret -- the caller already knows the host --
    # and it is the only symptom of a misconfigured base URL, which otherwise
    # presents as a blank iframe with nothing else to go on.
    seed(store)
    body = client.get("/api/health").json()
    assert body["public_base_url"] == "http://nas.local:8088"
    assert body["status"] == "ok"
    assert "feeds" not in body


def test_admin_health_has_feed_detail(client, store):
    seed(store)
    body = client.get("/api/health", headers=ADMIN).json()
    assert body["feeds"][0]["url"] == "https://x.example/rss"
    assert body["public_base_url"] == "http://nas.local:8088"


def test_health_is_ok_below_the_degraded_threshold(client, store):
    # One transient failure must not mark the deployment unhealthy: with
    # `restart: unless-stopped` in front of it that is a restart loop caused by
    # someone else's flaky feed.
    fid = seed(store)
    store.record_failure(fid, error="http 500", now=10, next_poll_at=20)
    store.record_failure(fid, error="http 500", now=30, next_poll_at=40)
    assert client.get("/api/health").json()["status"] == "ok"


def test_health_reports_degraded_at_the_threshold(client, store):
    fid = seed(store)
    for i in range(DEGRADED_AFTER_FAILURES):
        store.record_failure(fid, error="http 500", now=10 * i, next_poll_at=20 * i)
    assert client.get("/api/health").json()["status"] == "degraded"


def test_health_stays_200_when_degraded(client, store):
    # Docker's HEALTHCHECK only tests for HTTP 200.
    fid = seed(store)
    for i in range(DEGRADED_AFTER_FAILURES):
        store.record_failure(fid, error="http 500", now=10 * i, next_poll_at=20 * i)
    assert client.get("/api/health").status_code == 200


def test_health_ignores_disabled_feeds_when_deciding_degraded(client, store):
    fid = seed(store)
    for i in range(DEGRADED_AFTER_FAILURES):
        store.record_failure(fid, error="http 500", now=10 * i, next_poll_at=20 * i)
    store.unsubscribe("art", fid)
    assert client.get("/api/health").json()["status"] == "ok"
```

Delete `test_health_reports_feed_status_and_published_url` — it is superseded by the two
health tests above, which assert the same facts on the correct side of the admin key.
Update the import line to add `DEGRADED_AFTER_FAILURES` and `redact_feed_url`.

Append to `tests/test_main.py`:

```python
def test_request_logging_is_disabled(monkeypatch):
    # Tokens are in the query string and the request line is what an access log
    # records. Request logging belongs at the reverse proxy, which is
    # terminating TLS anyway and can be told to omit query strings.
    captured: dict = {}
    monkeypatch.setattr(main_mod, "build", lambda *a, **kw: object())
    monkeypatch.setattr(main_mod.uvicorn, "run", lambda app, **kw: captured.update(kw))
    main_mod.main()
    assert captured["access_log"] is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api_rest.py tests/test_main.py -v`
Expected: FAIL — `ImportError: cannot import name 'redact_feed_url'` and
`KeyError: 'access_log'`.

- [ ] **Step 3: Add redaction and the threshold to `api.py`**

Add `from urllib.parse import urlsplit` to the imports, then, beside `secret_ok`:

```python
# A single failed poll is a network blip, not an unhealthy deployment. With
# `restart: unless-stopped` in front of the container, flipping on the first
# failure is a restart loop caused by someone else's flaky feed.
DEGRADED_AFTER_FAILURES = 3


def redact_feed_url(url: str) -> str:
    """Reduce a feed URL to scheme and host.

    Feed URLs routinely carry credentials -- `?apikey=`, a signed path segment,
    or `user:token@host`. `.hostname` is used rather than `.netloc` precisely
    because netloc keeps the userinfo.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    # `.port` raises ValueError on a non-numeric port (`host:abc`), and it
    # raises on attribute access -- before the scheme/host guard below could
    # run. Unguarded, one malformed stored feed URL 500s the whole feed list
    # for every token caller, since the response is built in a comprehension.
    # POST /api/feeds does no URL validation, so an admin typo reaches the DB.
    # Corrected during Task 4 review; pinned by a unit test and a route test.
    try:
        port = parts.port
    except ValueError:
        return "(redacted)"
    if port:
        host = f"{host}:{port}"
    if not parts.scheme or not host:
        return "(redacted)"
    return f"{parts.scheme}://{host}"
```

- [ ] **Step 4: Apply it on `/api/feeds`**

```python
    @app.get("/api/feeds")
    def list_feeds(
        user: str = Depends(require_user_token),
        admin: bool = Depends(is_admin),
    ) -> dict:
        return {
            "feeds": [
                {
                    "id": f.id,
                    "url": f.url if admin else redact_feed_url(f.url),
                    "name": f.name,
                    "poll_interval_s": f.poll_interval_s,
                    "enabled": f.enabled,
                }
                for f in store.list_feeds(user)
            ]
        }
```

- [ ] **Step 5: Split `/api/health`**

```python
    @app.get("/api/health")
    def health(admin: bool = Depends(is_admin)) -> dict:
        feeds = store.all_feed_status()
        degraded = any(
            f["enabled"] and f["consecutive_failures"] >= DEGRADED_AFTER_FAILURES
            for f in feeds
        )
        body: dict = {
            "status": "degraded" if degraded else "ok",
            "version": __version__,
            # Not a secret -- the caller already knows the host -- and the only
            # symptom of a wrong base URL, which otherwise shows as a blank frame.
            "public_base_url": config.public_base_url,
        }
        if admin:
            body["feeds"] = feeds
        return body
```

The status code stays 200 in both states: Docker's `HEALTHCHECK` tests the code, not the
body, so `degraded` must never restart the container.

- [ ] **Step 6: Turn off request logging in `main.py`**

```python
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8088")),
        # Tokens travel in the query string, and the request line is what the
        # access log records. A redacting filter on `uvicorn.access` was
        # considered and rejected: it depends on the shape of uvicorn's log
        # record arguments, and a filter that silently stops matching after a
        # dependency bump fails OPEN -- writing tokens to disk with no signal.
        # Turning the logger off cannot fail that way. Request logging belongs
        # at the reverse proxy, configured to omit query strings.
        access_log=False,
    )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api_rest.py tests/test_main.py -v`
Expected: PASS — 46 and 6 tests respectively.

- [ ] **Step 8: Repair the remaining health assertion**

`tests/test_main.py::test_build_reconciles_config_and_serves` asserts
`client.get("/api/health").json()["feeds"][0]["url"]`. Send the admin key:

```python
        health = client.get("/api/health", headers={"X-Admin-Key": "test-key"}).json()
        assert health["feeds"][0]["url"] == "https://a.example/rss"
```

`test_shutdown_closes_store_even_if_a_background_task_died` only asserts a 200 on
`/api/health` and needs no change.

- [ ] **Step 9: Run the whole suite**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: PASS — 232 passed

- [ ] **Step 10: Commit**

```bash
git add src/rss_ticker/api.py src/rss_ticker/main.py tests/
git commit -m "feat: redact feed urls, gate health detail, stop logging request lines"
```

---

### Task 5: The widget page carries its token

**Files:**
- Modify: `src/rss_ticker/static/widget.html`
- Modify: `tests/harness/widget_prelude.js`
- Modify: `tests/harness/widget_driver.js`
- Test: `tests/test_widget_js.py`, `tests/test_widget_route.py`

**Interfaces:**
- Consumes: `/widget?user=&token=` (Task 2), `/api/news`, `/api/feeds`, `/ws/news`
- Produces: a widget that reads `token` from `location.search`, sends it on every fetch
  and on the WebSocket URL, never renders it into the DOM, and stops rather than
  reconnect-loops on a rejection

- [ ] **Step 1: Update the harness to supply a token and observe it**

In `tests/harness/widget_prelude.js`, replace the `location` stub and the `WebSocket`
stub:

```js
const TOKEN = "tkn-" + "0123456789abcdef".repeat(3);

globalThis.location = {
  search: "?user=art&token=" + TOKEN,
  protocol: "http:",
  host: "ticker.test",
};
```

```js
globalThis.WebSocket = function (url) {
  state.ws = this;
  state.wsUrl = url;
  this.close = function () {};
};
```

In `tests/harness/widget_driver.js`, add before the `console.log`:

```js
  function textOf(el) {
    return (
      [el.textContent, el.href, el.title, el.className].join(" ") +
      el.children.map(textOf).join(" ")
    );
  }
  const dom = ["list", "state", "count", "empty", "pill", "dot"]
    .map((id) => textOf(els[id]))
    .join(" ");
```

and these three fields inside the reported object:

```js
      ws_url_has_token: String(state.wsUrl || "").indexOf(TOKEN) >= 0,
      news_fetches_all_authed: state.newsFetches.every(
        (u) => u.indexOf("token=" + TOKEN) >= 0
      ),
      dom_has_token: dom.indexOf(TOKEN) >= 0,
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_widget_js.py`:

```python
def test_every_news_fetch_carries_the_token(tmp_path: Path):
    result = run_widget(tmp_path)
    assert result["news_fetches_all_authed"] is True


def test_the_websocket_url_carries_the_token(tmp_path: Path):
    # A browser cannot set a header on a WebSocket constructor, so the URL is
    # the only channel there is.
    result = run_widget(tmp_path)
    assert result["ws_url_has_token"] is True


def test_the_token_is_never_rendered_into_the_dom(tmp_path: Path):
    result = run_widget(tmp_path)
    assert result["dom_has_token"] is False
```

And append to `tests/test_widget_route.py`, in the style of the existing
`test_widget_restricts_link_hrefs_to_http_https`:

```python
def test_widget_sets_a_no_referrer_meta(client):
    body = client.get("/widget", params=AUTH).text
    assert 'name="referrer" content="no-referrer"' in body


def test_widget_script_never_logs_a_url(client):
    # console.log/error of a request URL would put the token in the browser
    # console, which is the one place a screenshot reaches.
    body = client.get("/widget", params=AUTH).text
    assert "console.log(" not in body
    assert "console.error(" not in body
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_widget_js.py tests/test_widget_route.py -v`
Expected: FAIL — `assert False is True` on both token tests, since the widget still builds
`?user=art` URLs with no token.

- [ ] **Step 4: Add the referrer meta to `widget.html`**

In `<head>`, after the viewport meta:

```html
<meta name="referrer" content="no-referrer">
```

Belt to the `Referrer-Policy` header's braces: the token is in this document's URL, and a
`Referer` on any outbound request would carry it.

- [ ] **Step 5: Read the token and build one auth prefix**

Replace the top of the IIFE:

```js
  var params = new URLSearchParams(location.search);
  var user = params.get("user") || "";
  var token = params.get("token") || "";
  // Kept in this closure only. It is never assigned to textContent, an
  // attribute, or a console call -- the only place it may appear is a request
  // URL.
  var auth = "user=" + encodeURIComponent(user) + "&token=" + encodeURIComponent(token);
```

- [ ] **Step 6: Send it on every request**

Replace `api`, and add the denial path:

```js
  var denied = false;

  function api(qs) {
    return fetch("/api/news?" + auth + (qs ? "&" + qs : "")).then(function (r) {
      if (r.status === 401) throw new Error("denied");
      if (!r.ok) throw new Error("http " + r.status);
      return r.json();
    });
  }

  function deny() {
    if (denied) return;
    denied = true;
    exhausted = true;
    setLive(false, "unauthorized");
    empty.textContent = "not authorized";
    empty.style.display = "block";
    if (ws) { try { ws.close(); } catch (e) {} }
  }
```

401 is now the only rejection a read endpoint returns — an unknown user is no longer 400 —
so one branch covers both cases.

In `loadOlder`, drop `user=` from `qs` and handle the rejection:

```js
  function loadOlder() {
    if (loading || exhausted || denied) return;
    loading = true;
    var qs = "limit=50" +
             (oldestCursor ? "&before=" + encodeURIComponent(oldestCursor) : "");
    api(qs).then(function (body) {
      append(body.articles);
      if (body.articles.length) {
        oldestCursor = body.articles[body.articles.length - 1].cursor;
      }
      if (!body.next_cursor) exhausted = true;
      if (!seen.size) empty.textContent = "no headlines yet";
      loading = false;
    }).catch(function (e) {
      loading = false;
      if (e && e.message === "denied") deny();
    });
  }
```

In `fillPage`:

```js
    return api("limit=200&after=" + encodeURIComponent(cursor)).then(function (body) {
```

And the feed count fetch:

```js
  fetch("/api/feeds?" + auth)
    .then(function (r) {
      if (r.status === 401) { deny(); return null; }
      return r.json();
    })
    .then(function (b) { if (b) count.textContent = b.feeds.length + " feeds"; })
    .catch(function () {});
```

**Corrected during Task 5 review.** As originally written this block, and `fillGap`'s
bare `.catch(function () {})`, swallowed a 401 without ever reaching `deny()` — which
contradicts this task's own stop-on-rejection requirement. At page load the damage is
masked, because `loadOlder`'s own `/api/news` call 401s and denies first. The real case
is a token invalidated *after* the socket opened: the pill keeps reading "live" while the
historical gap silently fails to fill and the feed count never populates. `fillGap` gains
the same treatment, denying only on the `"denied"` error so an ordinary network blip
during gap-fill still does not:

```js
    fillPage(newestCursor, 1).catch(function (e) {
      if (e && e.message === "denied") deny();
    }).then(function () {
```

`deny()` writes the string `"not authorized"` — never the token, and never the response
body, which is `{"detail": "Invalid credentials"}` anyway.

- [ ] **Step 7: Send it on the WebSocket and stop reconnecting when rejected**

```js
  function connect() {
    if (denied) return;
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws/news?" + auth);
    ws.onopen = function () { backoff = 1000; setLive(true, "live"); fillGap(); };
    ws.onmessage = function (ev) {
      var article = JSON.parse(ev.data);
      if (filling) { pendingLive.push(article); } else { prepend([article]); }
    };
    ws.onclose = function (ev) {
      // 4401 is the server's one rejection code. It is not fixable by waiting,
      // and an endless reconnect loop against a rejecting server is a denial of
      // service we would be inflicting on ourselves.
      if (denied || (ev && ev.code === 4401)) {
        deny();
        return;
      }
      setLive(false, "reconnecting");
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 30000);
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }
```

`ev` is guarded because the node harness calls `onclose` with no argument in some paths;
a real browser always supplies a `CloseEvent`.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_widget_js.py tests/test_widget_route.py -v`
Expected: PASS — 5 and 13 tests respectively. `test_widget_script_is_valid_javascript`
and both gap-fill tests must still pass: the `api()` rewrite changed URL construction, not
paging.

- [ ] **Step 9: Run the whole suite**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: PASS — 237 passed

- [ ] **Step 10: Commit**

```bash
git add src/rss_ticker/static/widget.html tests/
git commit -m "feat: widget reads its token from the url and sends it on every call"
```

---

### Task 6: Migration, deployment notes, and the README

**Files:**
- Modify: `README.md`
- Modify: `docker-compose.yml`
- Test: `tests/test_migration.py` (new)

**Interfaces:**
- Consumes: everything above
- Produces: no code path — an upgrade runbook, an operator-facing endpoint table, and the
  tests that pin what an existing deployment actually experiences

There are three pieces of pre-existing state and each behaves differently:

| Existing state | On upgrade |
|---|---|
| Database with users and no `token` column | Migrates silently. The column is added NULL, and a NULL token authenticates nothing, so those accounts are **closed** until boot writes the configured value |
| `config.yaml` with no `manifest_key` | **Startup fails**: `ConfigError: manifest_key is required …` |
| `config.yaml` with users and no `token` | **Startup fails**: `ConfigError: user 'art' has no token; generate one with …` |

There is no half-open state. Every path either starts with all secrets present or does
not start at all, and each failure message names exactly what is missing.

- [ ] **Step 1: Write the migration tests**

`tests/test_migration.py`:

```python
"""What an existing deployment experiences on upgrade.

The database migrates itself and leaves pre-existing accounts closed. The
config does not migrate itself: both new secrets are required, and a config
missing either must fail at startup rather than boot into a server that 401s
everything or, worse, one that serves something.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rss_ticker.config import ConfigError
from rss_ticker.main import build
from rss_ticker.store import Store

TOKEN = "tkn-" + "0123456789abcdef" * 3

OLD_CONFIG = """
public_base_url: http://nas.local:8088
admin_key: test-key
users:
  - id: art
    name: Art
    feeds:
      - {url: "https://a.example/rss", name: A}
"""

WITH_MANIFEST_KEY = OLD_CONFIG.replace(
    "admin_key: test-key\n", "admin_key: test-key\nmanifest_key: manifest-key\n"
)
NEW_CONFIG = WITH_MANIFEST_KEY.replace(
    "    name: Art\n", f"    name: Art\n    token: {TOKEN}\n"
)


def old_database(path: str) -> None:
    """A users table as it existed before the token column."""
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT, "
        "created_at INTEGER NOT NULL DEFAULT 0)"
    )
    db.execute("INSERT INTO users (id, name) VALUES ('art', 'Art')")
    db.commit()
    db.close()


def test_upgrading_without_a_manifest_key_fails_loudly(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(OLD_CONFIG)
    with pytest.raises(ConfigError, match="manifest_key"):
        build(cfg, str(tmp_path / "t.db"), env={})


def test_upgrading_with_a_tokenless_config_fails_loudly(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(WITH_MANIFEST_KEY)
    with pytest.raises(ConfigError, match="art"):
        build(cfg, str(tmp_path / "t.db"), env={})


def test_an_old_database_gains_the_column_and_keeps_its_users(tmp_path: Path):
    db_path = str(tmp_path / "t.db")
    old_database(db_path)
    store = Store(db_path)
    try:
        assert store.user_exists("art") is True
        assert store.token_for("art") is None
    finally:
        store.close()


def test_boot_writes_the_configured_token_onto_an_existing_user(tmp_path: Path):
    db_path = str(tmp_path / "t.db")
    old_database(db_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(NEW_CONFIG)

    app = build(cfg, db_path, env={})
    with TestClient(app) as client:
        assert client.get(
            "/api/news", params={"user": "art", "token": TOKEN}
        ).status_code == 200
        assert client.get("/api/news", params={"user": "art"}).status_code == 401


def test_a_user_left_out_of_the_config_stays_closed(tmp_path: Path):
    db_path = str(tmp_path / "t.db")
    old_database(db_path)
    store = Store(db_path)
    store.upsert_user("ghost", "Ghost")
    store.close()

    cfg = tmp_path / "config.yaml"
    cfg.write_text(NEW_CONFIG)
    app = build(cfg, db_path, env={})
    with TestClient(app) as client:
        assert client.get("/api/news", params={"user": "ghost"}).status_code == 401
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_migration.py -v`
Expected: PASS — 5 passed, with no source change. This task is characterization: the
behaviour was built in Tasks 1–5, and these tests pin the upgrade story so a later
"convenience" default cannot quietly reopen it.

If either `fails_loudly` test does **not** raise, stop: Task 1 Step 3 was not applied, and
existing deployments would boot with unauthenticated accounts.

- [ ] **Step 3: Update `docker-compose.yml`**

```yaml
services:
  ticker:
    image: ghcr.io/artcashin/rss-ticker:latest
    build: .
    restart: unless-stopped
    ports:
      - "8088:8088"
    environment:
      # Writes and the health detail. Operator only.
      TICKER_ADMIN_KEY: ${TICKER_ADMIN_KEY:?set TICKER_ADMIN_KEY}
      # widgets.json only. This is what you paste into OpenBB Workspace, so it
      # must not be the admin key -- startup fails if they are equal.
      TICKER_MANIFEST_KEY: ${TICKER_MANIFEST_KEY:?set TICKER_MANIFEST_KEY}
      # One per user id in config.yaml. The `:?` form makes a missing token a
      # compose-level failure with a readable message, before the container
      # starts and the config loader has to say it less helpfully.
      TICKER_TOKEN_ART: ${TICKER_TOKEN_ART:?set TICKER_TOKEN_ART}
      LOG_LEVEL: INFO
    volumes:
      - ticker-data:/data
      - ./config.yaml:/config/config.yaml:ro

volumes:
  ticker-data:
```

- [ ] **Step 4: Rewrite the README's setup and endpoint sections**

Replace the "Quick start", "OpenBB setup", and "Endpoints" sections of `README.md` with:

```markdown
## Quick start

    cp config.example.yaml config.yaml   # edit feeds and public_base_url
    gen() { python -c "import secrets; print(secrets.token_urlsafe(32))"; }
    export TICKER_ADMIN_KEY=$(gen)       # writes; operator only
    export TICKER_MANIFEST_KEY=$(gen)    # widgets.json; goes into OpenBB
    export TICKER_TOKEN_ART=$(gen)       # one per user id in config.yaml
    docker compose up -d

Three kinds of secret, and they are not interchangeable:

| Secret | Opens | Who holds it |
|---|---|---|
| `admin_key` | writes on `/api/feeds`, the per-feed detail on `/api/health` | you |
| `manifest_key` | `/widgets.json`, and nothing else | pasted into OpenBB Workspace |
| per-user `token` | that user's reads | published in the widget's iframe URL |

`manifest_key` is separate from `admin_key` on purpose: it is the value that leaves your
control and lives in a third-party UI, so leaking it must not confer write access.
Startup fails if the two are equal.

Every user in `config.yaml` needs a `token` of at least 32 characters. A user without one
is a startup error, not an open account — the container exits and `docker compose logs`
names the user.

`public_base_url` must be the URL OpenBB Workspace can reach this server at. It is baked
into `widgets.json` at startup; if it is wrong the widget renders a blank frame.
`GET /api/health` echoes the value in use, without a credential.

## Deployment requirements

This server authenticates with bearer tokens carried in the URL. That is forced by the
embedding — OpenBB attaches no auth to an iframe document load, browsers cannot set
headers on a WebSocket, and third-party cookies are blocked in cross-site iframes. See
the design spec's amendment for the full reasoning.

Three consequences are operational, not architectural, and are **your** responsibility:

- **TLS is mandatory.** Terminate it at a reverse proxy in front of the container and set
  `public_base_url` to the `https://` URL so `widgets.json` publishes `https`/`wss`
  endpoints. Over plain HTTP every token is readable by anyone on the path.
- **A private network is strongly recommended.** Tailscale, WireGuard, or an equivalent
  overlay makes the token a second layer rather than the only one.
- **Request logging is the proxy's job, and it must omit query strings.** This server
  writes no access log at all — the request line contains tokens, and a log filter that
  silently stopped matching after a dependency bump would fail open. Configure the proxy
  to log the path only (nginx: `$uri` rather than `$request`; Caddy: strip `uri.query`).
  A proxy left on its default combined-log format writes every user token to disk on
  every request.

Not built, and deliberately: a login page, server-side sessions, mTLS, and rate limiting.
Rate limiting is noted as future hardening.

## OpenBB setup

Add a custom backend in OpenBB Workspace pointing at `<public_base_url>`, and set its API
key to the value of **`TICKER_MANIFEST_KEY`** — not the admin key. Workspace sends it as
`X-API-KEY`, which `/widgets.json` requires. Then drop the "News window" or "News rail"
widget onto a dashboard; the per-user token travels inside the widget's published
endpoint URL.

## Endpoints

| Path | Credential |
|---|---|
| `GET /` | none |
| `GET /widgets.json` | `X-API-KEY: <manifest_key>` |
| `GET /widget?user=&token=` | user token, or admin key |
| `GET /api/news?user=&token=&limit=&before=&after=` | user token, or admin key |
| `WS /ws/news?user=&token=` | user token |
| `GET /api/feeds?user=&token=` | user token → host-only URLs; admin key → full URLs |
| `POST`, `DELETE /api/feeds` | `X-Admin-Key` |
| `GET /api/health` | none → status, version, base URL; `X-Admin-Key` → feed detail |

The token may be sent as `Authorization: Bearer <token>` instead of the `token` query
param anywhere the query param is accepted. The WebSocket and the iframe document load
have no such option, which is why the query param exists at all.

A rejected read is always `401 {"detail": "Invalid credentials"}` — the same response for
an unknown user as for a wrong token, so user ids cannot be enumerated. A request with no
`user` param at all is `422`.

## Upgrading an existing deployment

The database migrates itself: a `token` column is added to `users`, NULL for every
existing row, and a NULL token authenticates nothing. `config.yaml` does **not** migrate
itself, and the server will not start until it is complete.

1. Generate the secrets:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"` — one `manifest_key`,
   one token per user.
2. Add `manifest_key: ${TICKER_MANIFEST_KEY}` at the top level and
   `token: ${TICKER_TOKEN_<USER>}` under each user in `config.yaml`, then export the
   variables. Skipping either means the container fails to start with
   `manifest_key is required` or `user 'art' has no token`. Those failures are
   intentional and are the only thing standing between an upgrade and an open server.
3. `docker compose up -d`. Boot reconciliation writes each configured token onto the
   existing user row. Any user still in the database but no longer in `config.yaml` is
   logged as unable to authenticate and stays closed.
4. In OpenBB Workspace, set the backend's API key to `TICKER_MANIFEST_KEY` and **re-add
   the widget** — its endpoint URL changed and the old saved widget has no token in it.

Rotating a token is the same loop: change the variable, restart, re-add the widget. The
previous token stops working on restart. Rotating `manifest_key` means rotating every
user token too, since the manifest is what published them.
```

- [ ] **Step 5: Verify the container end to end**

```bash
cd ~/Developer/rss-ticker
gen() { python3 -c "import secrets; print(secrets.token_urlsafe(32))"; }
export TICKER_ADMIN_KEY=$(gen)
export TICKER_MANIFEST_KEY=$(gen)
export TICKER_TOKEN_ART=$(gen)
docker compose up -d --build
sleep 5
code() { curl -s -o /dev/null -w '%{http_code}\n' "$@"; }
code localhost:8088/widgets.json
code -H "X-API-KEY: $TICKER_ADMIN_KEY" localhost:8088/widgets.json
code -H "X-API-KEY: $TICKER_MANIFEST_KEY" localhost:8088/widgets.json
code "localhost:8088/api/news?user=art"
code "localhost:8088/api/news?user=nobody"
code "localhost:8088/api/news?user=art&token=$TICKER_TOKEN_ART"
curl -s localhost:8088/api/health
docker compose logs ticker 2>&1 | grep -c "$TICKER_TOKEN_ART"
```

Expected, in order: `401`, `401` (the admin key must **not** open the manifest), `200`,
`401`, `401` (identical to the previous line — no user-id oracle), `200`, a health body
with `status`, `version` and `public_base_url` but no `feeds`, and `0` — the last is the
log check, and a non-zero count is a failure of Task 4.

- [ ] **Step 6: Verify the incomplete-config failures are loud**

```bash
docker compose down
cp config.yaml config.yaml.bak
python3 - <<'PY'
import pathlib, re
p = pathlib.Path("config.yaml")
p.write_text(re.sub(r"^manifest_key:.*\n", "", p.read_text(), flags=re.M))
PY
docker compose up 2>&1 | tail -5

cp config.yaml.bak config.yaml
python3 - <<'PY'
import pathlib, re
p = pathlib.Path("config.yaml")
p.write_text(re.sub(r"^\s*token:.*\n", "", p.read_text(), flags=re.M))
PY
docker compose up 2>&1 | tail -5
mv config.yaml.bak config.yaml
```

Expected: the container exits both times, first with
`ConfigError: manifest_key is required; …` and then with
`ConfigError: user 'art' has no token; generate one with …`. Neither run serves a
request.

- [ ] **Step 7: Tear down and commit**

```bash
docker compose down
git add README.md docker-compose.yml tests/test_migration.py
git commit -m "docs: auth deployment requirements and the upgrade runbook"
```

---

## Acceptance criteria

Mapping to the spec amendment's criteria 7–13.

| # | Criterion | Verified by |
|---|---|---|
| 7 | Every protected endpoint is `401` without a token and `200` with one; `/ws/news` closes `4401` on a bad token **without registering a subscriber** | `test_api_rest.py`, `test_api_ws.py`, `test_widget_route.py` (Task 2) |
| 8 | An unknown user and a wrong token produce byte-identical `401` responses; a missing `user` is still `422`; the token check does the same work either way | `test_api_rest.py`, `test_api_ws.py` (Task 2) + Task 6 Step 5 |
| 9 | `/widgets.json` is `401` unauthenticated, `401` with the **admin** key, and returns the manifest with `manifest_key` | `test_widgets.py` (Task 3) + Task 6 Step 5 |
| 10 | `/api/feeds` gives scheme-and-host URLs to a token caller and full URLs to an admin caller, including a URL containing userinfo | `test_api_rest.py` (Task 4) |
| 11 | Public `/api/health` keeps `public_base_url` but has no feed detail; `degraded` only at `DEGRADED_AFTER_FAILURES`; the code stays 200 | `test_api_rest.py` (Task 4) |
| 12 | Booting against a config missing `manifest_key`, or missing any user's `token`, fails with a `ConfigError` naming exactly what is absent | `test_migration.py` (Task 6) + Task 6 Step 6 |
| 13 | The widget renders in OpenBB Workspace with the token supplied only by the published endpoint URL, and its WebSocket connects | **Manual** — see below |

Plus three structural invariants that must hold after every task:

- The existing 177 tests either pass unchanged, pass with a credential added by a fixture,
  or — for the four tests pinning the old `400`/`4400` behaviour — are **renamed and
  updated** to pin the new behaviour with a comment saying why. Nothing is deleted except
  `test_health_reports_feed_status_and_published_url`, which Task 4 replaces with two
  tests asserting the same facts on the correct side of the admin key.
- No test asserts a distinguishable rejection between an unknown user and a wrong token.
- `uv run ruff check src tests` is clean.

### Manual verification (acceptance criterion 13)

Not automatable — this is the only real test of the spec's constraint 1, that no OpenBB
auth reaches an iframe document load.

- [ ] Deploy behind TLS with `public_base_url` set to the `https://` URL.
- [ ] In OpenBB Workspace, edit the custom backend and set its API key to
      `TICKER_MANIFEST_KEY`. Confirm the widget list populates — if it is empty, Workspace
      is getting a 401 from `/widgets.json` and the key is wrong or is the admin key.
- [ ] Remove the old "News window" widget from the dashboard and add it again. Confirm
      headlines render and the connection dot goes green, which proves both the iframe
      document load and the `wss` handshake accepted the token from the URL.
- [ ] Open DevTools on the iframe and confirm the `/api/news` request carries `token=` and
      returns 200.
- [ ] Temporarily corrupt the token in the iframe URL and reload: the widget must show
      "unauthorized" and must **not** reconnect in a loop. Check the network pane for a
      single failed WebSocket, not a growing list.
- [ ] Confirm the reverse proxy's access log does not contain the token. The application
      writes no request log, so the proxy is the only place this can leak.
- [ ] Record the outcome against "Unverified" item 1 in the spec (whether Workspace
      applies any authentication to iframe document loads) and update that section with
      what was observed.
