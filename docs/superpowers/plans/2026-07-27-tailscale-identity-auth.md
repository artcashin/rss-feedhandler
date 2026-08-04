# Tailscale identity auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Authenticate the ticker with the identity headers Tailscale Serve injects, so no per-user token ever travels in a URL, and run it on the NAS behind a Tailscale sidecar, the same pattern as the other services in this series.

**Architecture:** A new `tailscale_auth` config flag makes the app trust `Tailscale-User-Login`, mapping that identity to a configured user. Trusting a proxy header is only sound while the proxy is the sole ingress, so enabling the flag forces a loopback bind and the app refuses to start otherwise. Token auth is untouched and stays the default, so every existing deployment and all 381 current tests keep working.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, pytest (+ pytest-asyncio), ruff, `uv`. Deployment: Docker Compose on the NAS container manager, `tailscale/tailscale:v1.98.9` sidecar, image published to GHCR.

**Spec:** `docs/superpowers/specs/2026-07-27-tailscale-identity-auth-design.md`

## Global Constraints

Every task's requirements implicitly include these.

- **The header is only trustworthy because Serve is the sole ingress.** Measured 2026-07-27: a request that bypasses Serve carries a client's *forged* `Tailscale-User-Login` verbatim, while a forged header sent *through* Serve is overwritten with the real identity. Any change that publishes a port, or trusts the header without the flag, is a security defect.
- **`tailscale_auth` defaults to `false`.** No existing config, deployment, or test may change behaviour. The suite is **381 passing**; the count only goes up.
- **Never log a token, a key, or an auth key**, at any level. Config errors name the *user* or the *field*, never the value.
- **Rejections stay indistinguishable.** Every failed read is `401 {"detail": "Invalid credentials"}`; a structurally missing `user` stays `422`; WebSocket rejects close `4401` **before** `broadcaster.subscribe`. An unknown identity, an identity mapped to another user, and a wrong token are the same rejection.
- **`admin_key` is required in every mode** and remains the only credential for writes.
- **Copy rule:** user-visible strings (log messages, API `detail`) are sentence case, no trailing period, no exclamation marks. Python exception messages (`raise ConfigError("admin_key is required")`) keep the lowercase, no-period stdlib convention.
- **The literal identity value is `you@github`** — a GitHub OIDC login, not an email. Use it verbatim in fixtures and example config.
- **Every task ends green:** `uv run pytest -q` and `uv run ruff check src tests` both clean before the commit. One pre-existing `StarletteDeprecationWarning` is expected; any other warning is a finding.
- **Every new test must be mutation-verified** — shown to fail against the unfixed code, with the real output in the task report. This codebase has repeatedly shipped tests that could not fail.

## File structure

| File | Responsibility |
|---|---|
| `src/rss_ticker/config.py` | New fields + all validation, including the fail-closed loopback guard |
| `src/rss_ticker/main.py` | Bind to `config.bind_host` instead of a hardcoded `0.0.0.0` |
| `src/rss_ticker/api.py` | `identity_user()` resolver; identity branch on reads, `/widgets.json`, and `/ws/news` |
| `src/rss_ticker/widgets.py` | Omit `&token=` from published endpoints when `tailscale_auth` |
| `src/rss_ticker/static/widget.html` | Omit an empty `token` query param |
| `docker-compose.nas.yml` | Tailscale sidecar, no published ports |
| `scripts/nas-setup.sh` | Tailnet provisioning mode |
| `config.example.yaml`, `README.md` | Document the mode, its precondition, the tagged-device caveat |

---

### Task 1: Config surface, fail-closed guard, and the real bind host

**Files:**
- Modify: `src/rss_ticker/config.py`
- Modify: `src/rss_ticker/main.py`
- Test: `tests/test_config.py`, `tests/test_main.py`

**Interfaces:**
- Produces:
  - `config.LOOPBACK_HOSTS: frozenset[str]`
  - `Config(..., tailscale_auth: bool = False, bind_host: str = "0.0.0.0")`
  - `UserConfig(..., tailscale_login: str = "")`
  - `build()` sets `app.state.bind_host`
- Consumed by: Tasks 2–4.

- [ ] **Step 1: Write the failing config tests**

Append to `tests/test_config.py`. The existing helper in that file is `write(tmp_path, text)`; follow it.

```python
LOGIN = "you@github"


def test_tailscale_auth_defaults_off(tmp_path):
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: k\nmanifest_key: mk\n")
    cfg = load_config(p, {})
    assert cfg.tailscale_auth is False
    assert cfg.bind_host == "0.0.0.0"


def test_tailscale_auth_requires_a_loopback_bind(tmp_path):
    # The Tailscale-User-Login header is only trustworthy while Serve is the
    # sole way in. Bound to 0.0.0.0 inside the sidecar's netns the port is
    # reachable by every tailnet peer, around Serve, where the header is
    # trivially forged. Refuse to start rather than serve a forgeable door.
    p = write(tmp_path, f"""
public_base_url: https://x
admin_key: k
tailscale_auth: true
bind_host: 0.0.0.0
users:
  - id: art
    tailscale_login: {LOGIN}
""")
    with pytest.raises(ConfigError, match="loopback"):
        load_config(p, {})


def test_tailscale_auth_accepts_loopback(tmp_path):
    p = write(tmp_path, f"""
public_base_url: https://x
admin_key: k
tailscale_auth: true
bind_host: 127.0.0.1
users:
  - id: art
    tailscale_login: {LOGIN}
""")
    cfg = load_config(p, {})
    assert cfg.tailscale_auth is True
    assert cfg.bind_host == "127.0.0.1"
    assert cfg.users[0].tailscale_login == LOGIN
    assert cfg.users[0].token == ""


def test_manifest_key_optional_under_tailscale_auth(tmp_path):
    p = write(tmp_path, f"""
public_base_url: https://x
admin_key: k
tailscale_auth: true
bind_host: 127.0.0.1
users:
  - id: art
    tailscale_login: {LOGIN}
""")
    assert load_config(p, {}).manifest_key == ""


def test_manifest_key_still_required_without_tailscale_auth(tmp_path):
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: k\n")
    with pytest.raises(ConfigError, match="manifest_key"):
        load_config(p, {})


def test_user_with_neither_token_nor_login_is_an_error(tmp_path):
    # A user nothing can authenticate as is a config mistake, not a silently
    # closed account.
    p = write(tmp_path, """
public_base_url: https://x
admin_key: k
tailscale_auth: true
bind_host: 127.0.0.1
users:
  - id: art
""")
    with pytest.raises(ConfigError, match="art"):
        load_config(p, {})


def test_duplicate_tailscale_login_is_an_error(tmp_path):
    p = write(tmp_path, f"""
public_base_url: https://x
admin_key: k
tailscale_auth: true
bind_host: 127.0.0.1
users:
  - id: art
    tailscale_login: {LOGIN}
  - id: bob
    tailscale_login: {LOGIN}
""")
    with pytest.raises(ConfigError, match="duplicate tailscale_login"):
        load_config(p, {})


def test_two_tokenless_users_are_not_a_duplicate_token_error(tmp_path):
    # Both tokens are "", which must not trip the duplicate-token check.
    p = write(tmp_path, f"""
public_base_url: https://x
admin_key: k
tailscale_auth: true
bind_host: 127.0.0.1
users:
  - id: art
    tailscale_login: {LOGIN}
  - id: bob
    tailscale_login: bob@github
""")
    assert [u.id for u in load_config(p, {}).users] == ["art", "bob"]


def test_token_still_required_without_tailscale_auth(tmp_path):
    p = write(tmp_path, """
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
""")
    with pytest.raises(ConfigError, match="art"):
        load_config(p, {})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'tailscale_auth'`, and `DID NOT RAISE` on the guard tests.

- [ ] **Step 3: Add the fields and validation to `config.py`**

Add beside the other module-level constants:

```python
# Hosts on which the Tailscale-User-Login header is trustworthy, because only
# a proxy on this machine (Tailscale Serve) can reach the port.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
```

Add the two `Config` fields **last**, with defaults, so direct construction in tests keeps working:

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
    tailscale_auth: bool = False
    bind_host: str = "0.0.0.0"
```

Add the `UserConfig` field **last**, with a default:

```python
@dataclass(frozen=True)
class UserConfig:
    id: str
    name: str | None = None
    feeds: tuple[FeedConfig, ...] = ()
    filters: tuple[FilterConfig, ...] = ()
    token: str = ""
    tailscale_login: str = ""
```

Replace the token block inside `_user`, and give it the new parameter:

```python
def _user(raw: dict, tailscale_auth: bool = False) -> UserConfig:
    uid = raw.get("id")
    if not uid:
        raise ConfigError("every user needs an id")
    if not _USER_ID_RE.match(uid):
        raise ConfigError(
            f"user id {uid!r} must contain only letters, digits, hyphens and underscores"
        )

    login = raw.get("tailscale_login") or ""
    if login and not isinstance(login, str):
        raise ConfigError(f"tailscale_login for user {uid!r} must be a string")

    token = raw.get("token")
    if not token:
        if not tailscale_auth:
            raise ConfigError(
                f"user {uid!r} has no token; generate one with "
                "python -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )
        if not login:
            raise ConfigError(
                f"user {uid!r} has neither a token nor a tailscale_login, so nothing "
                f"can authenticate as them"
            )
        token = ""
    elif not isinstance(token, str) or len(token) < MIN_TOKEN_LEN:
        raise ConfigError(
            f"token for user {uid!r} must be a string of at least {MIN_TOKEN_LEN} characters"
        )

    return UserConfig(
        id=uid,
        name=raw.get("name"),
        feeds=tuple(_feed(f) for f in raw.get("feeds") or []),
        filters=tuple(_filter(f) for f in raw.get("filters") or []),
        token=token,
        tailscale_login=login,
    )
```

In `load_config`, replace the `manifest_key` block and the user loop:

```python
    tailscale_auth = bool(raw.get("tailscale_auth", False))
    bind_host = raw.get("bind_host", "0.0.0.0")
    if not isinstance(bind_host, str) or not bind_host:
        raise ConfigError("bind_host must be a non-empty string")
    if tailscale_auth and bind_host not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"tailscale_auth requires a loopback bind_host (one of "
            f"{', '.join(sorted(LOOPBACK_HOSTS))}), got {bind_host!r}; the "
            f"Tailscale-User-Login header is only trustworthy when Tailscale "
            f"Serve is the only way to reach this server"
        )

    manifest_key = raw.get("manifest_key") or ""
    if not manifest_key and not tailscale_auth:
        raise ConfigError(
            "manifest_key is required; it gates widgets.json and is the value pasted "
            "into OpenBB Workspace, so it must not be the admin key"
        )
    if manifest_key and manifest_key == raw["admin_key"]:
        raise ConfigError("manifest_key must differ from admin_key")

    users = tuple(_user(u, tailscale_auth) for u in raw.get("users") or [])
    seen: set[str] = set()
    seen_tokens: set[str] = set()
    seen_logins: set[str] = set()
    for u in users:
        if u.id in seen:
            raise ConfigError(f"duplicate user id {u.id!r}")
        seen.add(u.id)
        # Empty tokens are legitimate under tailscale_auth; only real ones can collide.
        if u.token and u.token in seen_tokens:
            raise ConfigError(f"duplicate token configured for user {u.id!r}")
        if u.token:
            seen_tokens.add(u.token)
        if u.tailscale_login and u.tailscale_login in seen_logins:
            raise ConfigError(f"duplicate tailscale_login configured for user {u.id!r}")
        if u.tailscale_login:
            seen_logins.add(u.tailscale_login)
```

and pass the new values into the returned `Config`:

```python
    return Config(
        public_base_url=raw["public_base_url"].rstrip("/"),
        admin_key=raw["admin_key"],
        manifest_key=manifest_key,
        retention_days=_positive_int(raw, "retention_days", 7),
        default_poll_interval_s=_positive_int(raw, "default_poll_interval_s", 300),
        max_concurrent_polls=_positive_int(raw, "max_concurrent_polls", 8),
        users=users,
        tailscale_auth=tailscale_auth,
        bind_host=bind_host,
    )
```

- [ ] **Step 4: Run the config tests to verify they pass**

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing bind-host test**

Append to `tests/test_main.py`. Model it on the existing `test_request_logging_is_disabled`, which already monkeypatches `main_mod.uvicorn.run`:

```python
def test_server_binds_the_configured_host(tmp_path: Path, monkeypatch):
    # A hardcoded 0.0.0.0 would publish the port to every tailnet peer inside
    # the sidecar's network namespace, around Serve.
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG.replace("public_base_url:", "bind_host: 127.0.0.1\npublic_base_url:"))
    captured: dict = {}
    app = build(cfg, str(tmp_path / "t.db"), env={})
    monkeypatch.setattr(main_mod, "build", lambda *a, **kw: app)
    monkeypatch.setattr(main_mod.uvicorn, "run", lambda a, **kw: captured.update(kw))
    main_mod.main()
    assert captured["host"] == "127.0.0.1"
```

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/test_main.py::test_server_binds_the_configured_host -q`
Expected: FAIL — `assert '0.0.0.0' == '127.0.0.1'`.

- [ ] **Step 7: Bind the configured host in `main.py`**

In `build()`, after the app is created, stash the bind host so `main()` can read it without re-loading config:

```python
    app = create_app(config, store, broadcaster, lifespan=lifespan)
    app.state.bind_host = config.bind_host
    return app
```

In `main()`, replace the hardcoded host:

```python
    uvicorn.run(
        app,
        host=app.state.bind_host,
        port=int(os.environ.get("PORT", "8088")),
```

Leave the `access_log=False` block and its comment exactly as they are.

- [ ] **Step 8: Run the full suite and lint**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: PASS, 381 + new tests.

- [ ] **Step 9: Mutation-verify the guard**

Temporarily delete the `if tailscale_auth and bind_host not in LOOPBACK_HOSTS:` block, run `uv run pytest tests/test_config.py::test_tailscale_auth_requires_a_loopback_bind -q`, confirm it FAILS with `DID NOT RAISE`, restore, confirm it passes. Record both outputs in the report.

- [ ] **Step 10: Commit**

```bash
git add src/rss_ticker/config.py src/rss_ticker/main.py tests/test_config.py tests/test_main.py
git commit -m "feat: tailscale_auth and bind_host config, with a loopback fail-closed guard"
```

---

### Task 2: Identity authentication on the HTTP endpoints

**Files:**
- Modify: `src/rss_ticker/api.py`
- Test: `tests/test_api_rest.py`, `tests/test_widgets.py`

**Interfaces:**
- Consumes: `Config.tailscale_auth`, `UserConfig.tailscale_login` (Task 1)
- Produces: `identity_user(login: str | None) -> str | None` inside `create_app`; identity accepted by `require_user_token` and `require_manifest_key`.

- [ ] **Step 1: Write the failing REST tests**

In `tests/test_api_rest.py`, add a second app fixture alongside the existing ones. The file already defines `TOKEN`, `AUTH`, `ADMIN`, and a `store` fixture; follow their style.

```python
LOGIN = "you@github"
IDENT = {"Tailscale-User-Login": LOGIN}
TS_CFG = Config(
    public_base_url="https://t.example",
    admin_key="s3cret",
    tailscale_auth=True,
    bind_host="127.0.0.1",
    users=(UserConfig(id="art", tailscale_login=LOGIN),),
)


@pytest.fixture
def ts_client(store):
    return TestClient(create_app(TS_CFG, store, Broadcaster(store)))


def test_identity_authenticates_a_read(ts_client, store):
    seed(store, 1)
    r = ts_client.get("/api/news", params={"user": "art"}, headers=IDENT)
    assert r.status_code == 200


def test_identity_for_a_different_user_is_401(ts_client, store):
    store.upsert_user("bob", None)
    seed(store, 1)
    r = ts_client.get("/api/news", params={"user": "bob"}, headers=IDENT)
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid credentials"}


def test_unknown_identity_is_401(ts_client, store):
    seed(store, 1)
    r = ts_client.get(
        "/api/news", params={"user": "art"},
        headers={"Tailscale-User-Login": "nobody@github"},
    )
    assert r.status_code == 401


def test_no_identity_and_no_token_is_401(ts_client, store):
    seed(store, 1)
    assert ts_client.get("/api/news", params={"user": "art"}).status_code == 401


def test_identity_header_is_ignored_when_tailscale_auth_is_off(client, store):
    # THE security-critical test. Without the flag the header is just something
    # any client can type, so honouring it would be an open door on every
    # non-Tailscale deployment.
    seed(store, 1)
    r = client.get("/api/news", params={"user": "art"}, headers=IDENT)
    assert r.status_code == 401


def test_identity_authenticates_the_widget(ts_client, store):
    seed(store, 1)
    assert ts_client.get("/widget", params={"user": "art"}, headers=IDENT).status_code == 200


def test_identity_authenticates_feeds(ts_client, store):
    seed(store)
    assert ts_client.get("/api/feeds", params={"user": "art"}, headers=IDENT).status_code == 200


def test_admin_key_still_works_under_tailscale_auth(ts_client, store):
    seed(store, 1)
    r = ts_client.get("/api/news", params={"user": "art"}, headers={"X-Admin-Key": "s3cret"})
    assert r.status_code == 200


def test_writes_still_need_the_admin_key_under_tailscale_auth(ts_client):
    r = ts_client.post(
        "/api/feeds", json={"user": "art", "url": "https://n.example/rss"}, headers=IDENT
    )
    assert r.status_code == 401
```

Ensure `Config`, `UserConfig`, `Broadcaster`, and `create_app` are imported in that file (most already are; add `UserConfig` if missing).

- [ ] **Step 2: Write the failing manifest test**

Append to `tests/test_widgets.py`:

```python
def test_identity_opens_widgets_json_under_tailscale_auth():
    cfg = Config(
        public_base_url="https://t.example",
        admin_key="k",
        tailscale_auth=True,
        bind_host="127.0.0.1",
        users=(UserConfig(id="art", tailscale_login="you@github"),),
    )
    store = Store(":memory:")
    store.upsert_user("art", None)
    client = TestClient(create_app(cfg, store, Broadcaster(store)))
    try:
        r = client.get("/widgets.json", headers={"Tailscale-User-Login": "you@github"})
        assert r.status_code == 200
        assert "news_window_art" in r.json()
        # An unknown identity still gets nothing.
        assert client.get(
            "/widgets.json", headers={"Tailscale-User-Login": "nobody@github"}
        ).status_code == 401
    finally:
        store.close()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api_rest.py tests/test_widgets.py -q`
Expected: FAIL — the identity requests return `401` because the header is not consulted yet.

- [ ] **Step 4: Add the identity resolver to `create_app`**

Add near the other dependencies in `api.py`, before `is_admin`:

```python
    # Serve-supplied login -> configured user id, built once at startup.
    # Tailscale Serve overwrites any client-supplied Tailscale-User-* header
    # (verified 2026-07-27), so this value cannot be forged THROUGH Serve --
    # but it is forgeable by anything that reaches this server around Serve,
    # which is why tailscale_auth demands a loopback bind (see config.py).
    identity_users = {u.tailscale_login: u.id for u in config.users if u.tailscale_login}

    def identity_user(login: str | None) -> str | None:
        """Resolve a Serve identity to a user id, or None.

        Returns None whenever tailscale_auth is off, so a deployment that has
        not opted in can never be authenticated by a header a client typed.
        """
        if not config.tailscale_auth or not login:
            return None
        return identity_users.get(login)
```

- [ ] **Step 5: Accept identity on the protected reads**

Replace `require_user_token`:

```python
    def require_user_token(
        user: str = Query(...),
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
        tailscale_user_login: str | None = Header(default=None),
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
        if exists and identity_user(tailscale_user_login) == user:
            return user
        if not token_ok(bearer_token(authorization) or token, expected):
            raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)
        return user
```

FastAPI maps the `tailscale_user_login` parameter to the `Tailscale-User-Login` header automatically (underscores become hyphens).

- [ ] **Step 6: Accept identity on `/widgets.json`**

```python
    def require_manifest_key(
        x_api_key: str | None = Header(default=None),
        tailscale_user_login: str | None = Header(default=None),
    ) -> None:
        # X-API-KEY is OpenBB Workspace's documented convention for a custom
        # backend's key; its value here is manifest_key, deliberately NOT
        # admin_key -- this is the secret that leaves the operator's control.
        # Under tailscale_auth the caller's own Serve identity is accepted too,
        # which is what lets the manifest carry no tokens at all.
        if identity_user(tailscale_user_login):
            return
        if not secret_ok(x_api_key, config.manifest_key):
            raise HTTPException(status_code=401, detail="API key required")
```

Leave the route's `dependencies=[Depends(require_manifest_key)]` unchanged, and do **not** make it accept the admin key — that separation is deliberate.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api_rest.py tests/test_widgets.py -q`
Expected: PASS.

- [ ] **Step 8: Mutation-verify the security-critical test**

Change `identity_user` to drop the flag check (`if not login: return None`), run
`uv run pytest tests/test_api_rest.py::test_identity_header_is_ignored_when_tailscale_auth_is_off -q`,
confirm it FAILS (the header now authenticates on a non-Tailscale deployment), restore, confirm it passes. Record both outputs.

- [ ] **Step 9: Run the full suite and lint**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/rss_ticker/api.py tests/test_api_rest.py tests/test_widgets.py
git commit -m "feat: accept Tailscale Serve identity on reads and widgets.json"
```

---

### Task 3: Identity authentication on the WebSocket

**Files:**
- Modify: `src/rss_ticker/api.py`
- Test: `tests/test_api_ws.py`

**Interfaces:**
- Consumes: `identity_user` (Task 2)
- Produces: `/ws/news` accepting a Serve identity, still closing `4401` before `broadcaster.subscribe`.

Measured 2026-07-27: the identity headers **do** arrive on the WebSocket upgrade through Serve, which is what makes this task possible.

- [ ] **Step 1: Write the failing WebSocket tests**

In `tests/test_api_ws.py`, add a tailscale-mode app beside the existing fixtures:

```python
LOGIN = "you@github"
IDENT = {"Tailscale-User-Login": LOGIN}
TS_CFG = Config(
    public_base_url="https://t.example",
    admin_key="k",
    tailscale_auth=True,
    bind_host="127.0.0.1",
    users=(UserConfig(id="art", tailscale_login=LOGIN),),
)


@pytest.fixture
def ts_client(store, broadcaster):
    return TestClient(create_app(TS_CFG, store, broadcaster))


def test_ws_accepts_a_serve_identity(ts_client, broadcaster):
    with ts_client.websocket_connect("/ws/news?user=art", headers=IDENT):
        assert broadcaster.subscriber_count("art") == 1


def test_ws_rejects_an_identity_for_another_user(ts_client, store, broadcaster):
    store.upsert_user("bob", None)
    with pytest.raises(WebSocketDisconnect) as exc:
        with ts_client.websocket_connect("/ws/news?user=bob", headers=IDENT) as ws:
            ws.receive_json()
    assert exc.value.code == 4401
    assert broadcaster.subscriber_count("bob") == 0


def test_ws_rejects_an_unknown_identity(ts_client, broadcaster):
    with pytest.raises(WebSocketDisconnect) as exc:
        with ts_client.websocket_connect(
            "/ws/news?user=art", headers={"Tailscale-User-Login": "nobody@github"}
        ) as ws:
            ws.receive_json()
    assert exc.value.code == 4401
    assert broadcaster.subscriber_count("art") == 0


def test_ws_ignores_the_identity_header_when_tailscale_auth_is_off(client, broadcaster):
    # Same security property as the REST path: without the flag the header is
    # attacker-supplied text.
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/news?user=art", headers=IDENT) as ws:
            ws.receive_json()
    assert exc.value.code == 4401
    assert broadcaster.subscriber_count("art") == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_api_ws.py -q`
Expected: FAIL — the identity connections close `4401` because the handshake ignores the header.

- [ ] **Step 3: Read the identity off the handshake**

Replace the top of `ws_news`:

```python
    async def ws_news(websocket: WebSocket) -> None:
        user = websocket.query_params.get("user")
        token = websocket.query_params.get("token")
        # A browser cannot set headers on a WebSocket, but Tailscale Serve
        # injects the identity on the upgrade request itself (verified), which
        # is what lets the socket authenticate with no token in the URL.
        login = websocket.headers.get("tailscale-user-login")
        await websocket.accept()
        if not user or (
            identity_user(login) != user
            and not token_ok(token, store.token_for(user))
        ):
            # One code and one reason for "no such user", "wrong identity" and
            # "wrong token": a distinct code would be the enumeration oracle
            # over again. Reject before subscribing -- a subscription
            # registered here and torn down on the next line can still be
            # handed a frame.
            await websocket.close(code=4401, reason="Invalid credentials")
            return

        sub = broadcaster.subscribe(user)
```

The rest of the handler is unchanged.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api_ws.py -q`
Expected: PASS.

- [ ] **Step 5: Mutation-verify the reject-before-subscribe property**

Move `sub = broadcaster.subscribe(user)` above the `if not user or (...)` guard, run
`uv run pytest tests/test_api_ws.py::test_ws_rejects_an_identity_for_another_user -q`,
confirm it FAILS on `subscriber_count("bob") == 0`, restore, confirm it passes. Record both outputs.

- [ ] **Step 6: Run the full suite and lint**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/rss_ticker/api.py tests/test_api_ws.py
git commit -m "feat: authenticate the websocket with the Serve identity"
```

---

### Task 4: Token-free manifest and widget

**Files:**
- Modify: `src/rss_ticker/widgets.py`
- Modify: `src/rss_ticker/static/widget.html`
- Test: `tests/test_widgets.py`, `tests/test_widget_route.py`

**Interfaces:**
- Consumes: `Config.tailscale_auth` (Task 1)
- Produces: `render_widgets` emitting `…/widget?user=<id>` with no `token` under `tailscale_auth`.

This is the substantive security win: no token in the iframe URL, in OpenBB's saved dashboard config, in DevTools, or in any proxy access log.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_widgets.py`:

```python
def test_endpoint_has_no_token_under_tailscale_auth():
    cfg = Config(
        public_base_url="https://t.example",
        admin_key="k",
        tailscale_auth=True,
        bind_host="127.0.0.1",
        users=(UserConfig(id="art", tailscale_login="you@github"),),
    )
    endpoint = render_widgets(cfg)["news_window_art"]["endpoint"]
    assert endpoint == "https://t.example/widget?user=art"
    assert "token" not in endpoint


def test_endpoint_still_carries_the_token_without_tailscale_auth():
    cfg = Config(
        public_base_url="http://x",
        admin_key="k",
        manifest_key="mk",
        users=(UserConfig(id="art", token="tkn-" + "0123456789abcdef" * 3),),
    )
    assert "token=tkn-" in render_widgets(cfg)["news_window_art"]["endpoint"]
```

Append to `tests/test_widget_route.py`:

```python
def test_widget_script_omits_an_empty_token_param(client):
    # With no token in the iframe URL the widget must send `user=art`, never
    # `user=art&token=` -- a blank credential is noise in every request.
    body = client.get("/widget", params=AUTH).text
    assert 'token ? "&token=" + encodeURIComponent(token) : ""' in body
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_widgets.py tests/test_widget_route.py -q`
Expected: FAIL — the endpoint still contains `&token=`, and the widget still always appends the parameter.

- [ ] **Step 3: Drop the token from the published endpoint**

In `src/rss_ticker/widgets.py`, replace the `endpoint` expression:

```python
                "endpoint": (
                    f"{config.public_base_url}/widget"
                    f"?user={quote(user.id, safe='')}"
                    # Under tailscale_auth the caller is identified by the
                    # header Serve injects, so publishing a token here would
                    # put a credential in the iframe URL for nothing.
                    + (
                        ""
                        if config.tailscale_auth
                        else f"&token={quote(user.token, safe='')}"
                    )
                ),
```

- [ ] **Step 4: Stop the widget sending a blank token**

In `src/rss_ticker/static/widget.html`, replace the `auth` construction:

```js
  var auth = "user=" + encodeURIComponent(user) +
             (token ? "&token=" + encodeURIComponent(token) : "");
```

Everything else in the widget is unchanged: with no token it simply sends `user=…`, and Serve supplies the identity on every same-origin fetch and on the WebSocket.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_widgets.py tests/test_widget_route.py tests/test_widget_js.py -q`
Expected: PASS. The existing widget JS harness tests must stay green — they supply a token, so `auth` is unchanged for them.

- [ ] **Step 6: Validate the JS and run the full suite**

Run:
```bash
node --check <(python3 -c "import re,pathlib;print(re.findall(r'<script>(.*?)</script>',pathlib.Path('src/rss_ticker/static/widget.html').read_text(),re.S)[-1])")
uv run pytest -q && uv run ruff check src tests
```
Expected: no JS syntax error; suite PASS.

- [ ] **Step 7: Commit**

```bash
git add src/rss_ticker/widgets.py src/rss_ticker/static/widget.html tests/
git commit -m "feat: publish token-free widget endpoints under tailscale_auth"
```

---

### Task 5: NAS deployment artifacts and documentation

**Files:**
- Modify: `docker-compose.nas.yml`
- Modify: `scripts/nas-setup.sh`
- Modify: `config.example.yaml`, `README.md`
- Modify: `pyproject.toml`, `Makefile` (version bump)

**Interfaces:**
- Consumes: everything above.
- Produces: a compose file and setup script that stand up the tailnet deployment; no new code path.

- [ ] **Step 1: Replace `docker-compose.nas.yml`**

```yaml
# rss-ticker on a NAS, behind a Tailscale sidecar.
#
# The tailscale container owns the network namespace; the ticker joins it, so
# the pair appear on the tailnet as one node named "rss-ticker". NOTHING is
# published to the LAN -- Tailscale Serve is the only way in, which is exactly
# what makes the Tailscale-User-Login header trustworthy.
#
#   https://rss-ticker.your-tailnet.ts.net/   (Serve terminates TLS, real LE cert)
#
# The auth key lives in ./ts.env (chmod 600) and is NOT in this file.
services:
  tailscale:
    image: tailscale/tailscale:v1.98.9
    container_name: rss-ticker-ts
    hostname: rss-ticker
    restart: unless-stopped
    env_file:
      - ./ts.env
    environment:
      - TS_HOSTNAME=rss-ticker
      - TS_STATE_DIR=/var/lib/tailscale
      - TS_SERVE_CONFIG=/config/serve.json
    volumes:
      - ./ts-state:/var/lib/tailscale
      - ./ts-config:/config
    devices:
      - /dev/net/tun:/dev/net/tun
    cap_add:
      - NET_ADMIN
      - NET_RAW

  rss-ticker:
    image: ghcr.io/artcashin/rss-ticker:0.4.0
    container_name: rss-ticker
    restart: unless-stopped
    # Share the sidecar netns: the ticker has no network identity of its own,
    # and deliberately publishes no ports.
    network_mode: service:tailscale
    depends_on:
      - tailscale
    environment:
      - LOG_LEVEL=INFO
    volumes:
      - ./config:/config:ro
      - ./data:/data
```

There is no `ports:` key, and there must never be one: a published port is reachable around Serve, where the identity header is forgeable.

- [ ] **Step 2: Add the tailnet mode to `scripts/nas-setup.sh`**

Add near the other defaults at the top:

```sh
TAILNET="${TAILNET:-0}"
TS_HOSTNAME="${TS_HOSTNAME:-rss-ticker}"
TS_TAILNET_DOMAIN="${TS_TAILNET_DOMAIN:-your-tailnet.ts.net}"
```

and add this branch where the script writes its config and compose (keep the
existing LAN behaviour intact for `TAILNET=0`):

```sh
if [ "$TAILNET" = "1" ]; then
  mkdir -p "$BASE/ts-state" "$BASE/ts-config" "$BASE/config" "$BASE/data"

  cat > "$BASE/ts-config/serve.json" <<'JSON'
{
  "TCP": { "443": { "HTTPS": true } },
  "Web": {
    "${TS_CERT_DOMAIN}:443": {
      "Handlers": { "/": { "Proxy": "http://127.0.0.1:8088" } }
    }
  }
}
JSON

  if [ ! -f "$BASE/ts.env" ]; then
    umask 077
    printf 'TS_AUTHKEY=\n' > "$BASE/ts.env"
    echo "WROTE $BASE/ts.env -- put your Tailscale auth key in it before starting."
  fi
  chmod 600 "$BASE/ts.env"

  if [ ! -f "$BASE/config/config.yaml" ]; then
    ADMIN_KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    umask 077
    cat > "$BASE/config/config.yaml" <<YAML
public_base_url: https://${TS_HOSTNAME}.${TS_TAILNET_DOMAIN}

# Trust the identity Tailscale Serve injects. Sound ONLY because the ticker
# binds loopback inside the sidecar's network namespace, so Serve is the only
# way in. Startup fails if bind_host is not loopback.
tailscale_auth: true
bind_host: 127.0.0.1

admin_key: ${ADMIN_KEY}

users:
  - id: art
    name: Art
    tailscale_login: you@github
    feeds:
      - {url: "https://feeds.bloomberg.com/markets/news.rss", name: Bloomberg Markets, group: Markets}
YAML
    echo "WROTE $BASE/config/config.yaml (admin_key generated; edit feeds to taste)"
  fi
fi
```

- [ ] **Step 3: Document the mode in `config.example.yaml`**

Append:

```yaml
# ---------------------------------------------------------------------------
# Tailscale identity auth (optional; default off)
#
# Behind Tailscale Serve, the caller's verified identity arrives as a request
# header -- on plain requests AND on the WebSocket upgrade -- so no per-user
# token needs to travel in a URL. Serve overwrites any client-supplied
# Tailscale-User-* header, so it cannot be forged THROUGH Serve.
#
# It IS forgeable by anything that reaches this server around Serve, so
# enabling this REQUIRES a loopback bind and startup fails otherwise.
#
# Caveats: every viewer must be on your tailnet, and Serve omits identity
# headers for TAGGED devices -- tagging a viewing device silently breaks it.
#
# tailscale_auth: true
# bind_host: 127.0.0.1
# users:
#   - id: art
#     tailscale_login: you@github   # a Tailscale login, not an email
#     # token and manifest_key become optional in this mode; admin_key does not
```

- [ ] **Step 4: Document it in `README.md`**

Add a section after the existing deployment requirements:

```markdown
## Tailscale mode (no tokens in URLs)

Behind Tailscale Serve the ticker can authenticate you by the identity Serve
injects, so no per-user token appears in any URL — not in `widgets.json`, not
in the iframe address, not in a proxy log.

    tailscale_auth: true
    bind_host: 127.0.0.1
    users:
      - id: art
        tailscale_login: you@github

`docker-compose.nas.yml` runs this: a `tailscale/tailscale` sidecar owns the
network namespace, the ticker joins it and publishes **no ports**, and Serve
terminates TLS with a real Let's Encrypt certificate.

**Why the loopback bind is mandatory.** Trusting a proxy header is safe only
while that proxy is the only way in. Sharing the sidecar's namespace, a
`0.0.0.0` bind would expose the port to every tailnet peer *around* Serve,
where the header is trivially forged. The app refuses to start in that
combination.

**Two caveats.** Every device that views the dashboard must be on your tailnet.
And Tailscale omits identity headers for **tagged** devices, so tagging a
viewing device silently removes its access.

Token mode remains the default and is unchanged for non-Tailscale deployments.
```

- [ ] **Step 5: Bump the version**

`pyproject.toml`: `version = "0.4.0"`. `Makefile`: `TAG ?= 0.4.0`.

- [ ] **Step 6: Run the full suite and lint**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: PASS (documentation and compose changes touch no code paths).

- [ ] **Step 7: Verify the compose file has no published ports**

Run: `grep -n "ports:" docker-compose.nas.yml || echo "no ports key — correct"`
Expected: `no ports key — correct`.

- [ ] **Step 8: Commit**

```bash
git add docker-compose.nas.yml scripts/nas-setup.sh config.example.yaml README.md pyproject.toml Makefile
git commit -m "feat: tailnet NAS deployment behind a Tailscale sidecar"
```

---

### Task 6: Build, deploy to the NAS, and verify end to end

**Files:** none in the repo — this task deploys and proves it.

**Interfaces:** Consumes Tasks 1–5.

This task touches live infrastructure. Do not improvise: if a step's expected output does not appear, stop and report rather than working around it.

- [ ] **Step 1: Build and push the image**

The NAS is `x86_64`; the Makefile target builds `linux/amd64,linux/arm64` and pushes.

```bash
make buildx TAG=0.4.0
```
Expected: the build completes and pushes `ghcr.io/artcashin/rss-ticker:0.4.0`.

- [ ] **Step 2: Provision the NAS directory**

```bash
ssh nas 'mkdir -p /path/on/nas/rss-ticker/{config,data,ts-state,ts-config}'
scp docker-compose.nas.yml nas:/path/on/nas/rss-ticker/docker-compose.yml
```

- [ ] **Step 3: Write `serve.json` on the NAS**

```bash
ssh nas 'cat > /path/on/nas/rss-ticker/ts-config/serve.json' <<'JSON'
{
  "TCP": { "443": { "HTTPS": true } },
  "Web": {
    "${TS_CERT_DOMAIN}:443": {
      "Handlers": { "/": { "Proxy": "http://127.0.0.1:8088" } }
    }
  }
}
JSON
```

- [ ] **Step 4: Have the operator supply the auth key**

The auth key is a credential; it must never be echoed, pasted into chat, or put
on a command line. Ask the operator to run this from their Mac (zsh), using a
key generated at https://login.tailscale.com/admin/settings/keys — **not**
ephemeral, so the node identity survives restarts:

```bash
read -rs "K?Tailscale auth key: "; echo; printf 'TS_AUTHKEY=%s\n' "$K" | ssh nas 'umask 077; cat > /path/on/nas/rss-ticker/ts.env'; unset K; echo written
```

- [ ] **Step 5: Write the config**

Copy the tailnet config onto the NAS at `/path/on/nas/rss-ticker/config/config.yaml`, generating a fresh `admin_key`:

Write the file with a marker, then substitute a freshly generated key on the NAS
so it is never echoed here:

```bash
ssh nas 'umask 077; cat > /path/on/nas/rss-ticker/config/config.yaml' <<'YAML'
public_base_url: https://rss-ticker.your-tailnet.ts.net
tailscale_auth: true
bind_host: 127.0.0.1
admin_key: REPLACE_ME
users:
  - id: art
    name: Art
    tailscale_login: you@github
    feeds:
      - {url: "https://feeds.bloomberg.com/markets/news.rss", name: Bloomberg Markets, group: Markets}
      - {url: "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", name: WSJ Markets, group: Markets}
      - {url: "https://markets.businessinsider.com/rss/news", name: Business Insider, group: Markets}
      - {url: "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", name: NYT Business, group: Markets}
      - {url: "https://www.kiplinger.com/feed/all", name: Kiplinger, group: "Wealth M"}
      - {url: "https://bobpisani.substack.com/feed", name: Bob Pisani, group: Substack}
    filters:
      - {pattern: nvidia, action: highlight}
YAML
ssh nas 'A=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d " \n"); sed -i "s/REPLACE_ME/$A/" /path/on/nas/rss-ticker/config/config.yaml; if grep -q REPLACE_ME /path/on/nas/rss-ticker/config/config.yaml; then echo "SUBSTITUTION FAILED - do not start the container"; else echo "admin_key set"; fi'
```

Expected: `admin_key set`. If it prints the failure line, stop — the config
still contains a literal marker where the admin key belongs.

Copy the real feed list from the working `config.yaml` if it has diverged.

- [ ] **Step 6: Bring it up**

```bash
ssh nas 'D=/path/to/nas/docker; cd /path/on/nas/rss-ticker && $D compose up -d'
```
Expected: both `rss-ticker-ts` and `rss-ticker` start.

- [ ] **Step 7: Confirm the node and Serve**

```bash
ssh nas 'D=/path/to/nas/docker; $D exec rss-ticker-ts tailscale serve status'
```
Expected: `https://rss-ticker.your-tailnet.ts.net (tailnet only)` proxying to `http://127.0.0.1:8088`.

- [ ] **Step 8: Verify identity auth end to end**

From the Mac (first request may take ~19s while the certificate provisions; a `000` on the very first try is not an error — retry):

```bash
curl -sS -o /dev/null -w 'root=%{http_code}\n' --max-time 45 https://rss-ticker.your-tailnet.ts.net/
curl -sS -o /dev/null -w 'news=%{http_code}\n' --max-time 20 'https://rss-ticker.your-tailnet.ts.net/api/news?user=art&limit=3'
curl -sS -o /dev/null -w 'manifest=%{http_code}\n' --max-time 20 https://rss-ticker.your-tailnet.ts.net/widgets.json
curl -sS -o /dev/null -w 'widget=%{http_code}\n' --max-time 20 'https://rss-ticker.your-tailnet.ts.net/widget?user=art'
curl -sS --max-time 20 https://rss-ticker.your-tailnet.ts.net/widgets.json | grep -c token
```
Expected: `root=200`, `news=200`, `manifest=200`, `widget=200`, and the last command prints `0` — **no token anywhere in the manifest**. A non-zero count is a Task 4 failure.

- [ ] **Step 9: Verify the port is NOT reachable around Serve**

This is the security precondition; it must be proven, not assumed.

```bash
ssh nas 'D=/path/to/nas/docker; $D exec rss-ticker-ts tailscale ip -4'
# then, using the address it prints:
curl -sS -o /dev/null -w 'bypass=%{http_code}\n' --max-time 8 http://<TAILNET_IP>:8088/api/news?user=art || echo "bypass refused — correct"
```
Expected: connection refused (or a timeout), **not** an HTTP status. If this returns 200, stop: the ticker is bound wrongly and the identity header is forgeable.

- [ ] **Step 10: Point OpenBB at it**

In OpenBB Workspace, add a custom backend at `https://rss-ticker.your-tailnet.ts.net`. Leave the API key blank (identity auth covers `/widgets.json`). Add the News window widget and confirm headlines render and the connection dot goes green — which proves the iframe document load *and* the `wss` handshake both authenticated by identity, with no token in any URL.

- [ ] **Step 11: Commit any deployment corrections**

If Steps 1–10 required changes to the compose file or the setup script, commit them:

```bash
git add -A && git commit -m "fix: deployment corrections found bringing the tailnet stack up"
```

---

## Acceptance criteria

| # | Criterion | Verified by |
|---|---|---|
| 1 | `tailscale_auth` + non-loopback `bind_host` refuses to start | Task 1, mutation-verified |
| 2 | A matching Serve identity authenticates reads, the widget, and the WebSocket | Tasks 2–3 |
| 3 | An unknown identity, or one mapped to another user, is an indistinguishable `401` / `4401` | Tasks 2–3 |
| 4 | The identity header is ignored entirely when `tailscale_auth` is off | Task 2, mutation-verified — the security-critical test |
| 5 | The WebSocket still rejects before `broadcaster.subscribe` | Task 3, mutation-verified |
| 6 | `widgets.json` publishes no token under `tailscale_auth` | Task 4 + Task 6 Step 8 |
| 7 | The server binds the configured host | Task 1 |
| 8 | The port is unreachable around Serve on the live deployment | Task 6 Step 9 |
| 9 | Token mode is unchanged; the pre-existing suite stays green | every task |
| 10 | The widget renders in OpenBB with no token in any URL | Task 6 Step 10 (manual) |
