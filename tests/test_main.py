import logging
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rss_ticker import favicon as favicon_mod
from rss_ticker import main as main_mod
from rss_ticker.main import build

TOKEN = "tkn-" + "0123456789abcdef" * 3

CONFIG = f"""
public_base_url: http://nas.local:8088
admin_key: test-key
manifest_key: manifest-key
retention_days: 2
users:
  - id: art
    name: Art
    token: {TOKEN}
    feeds:
      - {{url: "https://a.example/rss", name: A}}
    filters:
      - {{pattern: nvidia, action: highlight}}
"""


def test_build_reconciles_config_and_serves(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    app = build(cfg, str(tmp_path / "t.db"), env={})
    with TestClient(app) as client:
        health = client.get("/api/health", headers={"X-Admin-Key": "test-key"}).json()
        assert health["feeds"][0]["url"] == "https://a.example/rss"
        assert (
            "news_window_art"
            in client.get("/widgets.json", headers={"X-API-KEY": "manifest-key"}).json()
        )
        assert (
            client.get("/api/news", params={"user": "art", "token": TOKEN}).json()["articles"]
            == []
        )


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
        urls = [
            f["url"]
            for f in c2.get(
                "/api/feeds",
                params={"user": "art"},
                headers={"X-Admin-Key": "test-key"},
            ).json()["feeds"]
        ]
    assert "https://runtime.example/rss" in urls


def test_shutdown_closes_store_even_if_a_background_task_died(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    monkeypatch.setattr(main_mod, "SWEEP_INTERVAL_S", 0)

    def boom(*a, **kw):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(main_mod.Store, "sweep", boom)

    app = main_mod.build(cfg, str(tmp_path / "t.db"), env={})
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200

    store = app.state.store
    with pytest.raises(sqlite3.ProgrammingError):
        store.user_exists("art")


BOB_TOKEN = "bob-" + "0123456789abcdef" * 3

CONFIG_WITH_BOB = f"""
public_base_url: http://nas.local:8088
admin_key: test-key
manifest_key: manifest-key
retention_days: 2
users:
  - id: art
    name: Art
    token: {TOKEN}
    feeds:
      - {{url: "https://a.example/rss", name: A}}
  - id: bob
    name: Bob
    token: {BOB_TOKEN}
"""


def test_user_removed_from_config_loses_access_on_restart(tmp_path: Path):
    # Reproduces the offboarding story end to end: a user configured (and
    # therefore serving reads) in one boot, then dropped from config.yaml
    # before the next boot, must come back closed everywhere they were open.
    cfg = tmp_path / "config.yaml"
    db = str(tmp_path / "t.db")

    cfg.write_text(CONFIG_WITH_BOB)
    app1 = build(cfg, db, env={})
    with TestClient(app1) as client:
        assert client.get(
            "/api/news", params={"user": "bob", "token": BOB_TOKEN}
        ).status_code == 200
        assert client.get(
            "/api/feeds", params={"user": "bob", "token": BOB_TOKEN}
        ).status_code == 200
        assert client.get(
            "/widget", params={"user": "bob", "token": BOB_TOKEN}
        ).status_code == 200
        with client.websocket_connect(f"/ws/news?user=bob&token={BOB_TOKEN}"):
            pass

    cfg.write_text(CONFIG)  # bob is no longer in this config
    app2 = build(cfg, db, env={})
    with TestClient(app2) as client:
        assert client.get(
            "/api/news", params={"user": "bob", "token": BOB_TOKEN}
        ).status_code == 401
        assert client.get(
            "/api/feeds", params={"user": "bob", "token": BOB_TOKEN}
        ).status_code == 401
        assert client.get(
            "/widget", params={"user": "bob", "token": BOB_TOKEN}
        ).status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ws/news?user=bob&token={BOB_TOKEN}") as ws:
                ws.receive_json()


def test_startup_resolves_favicons_for_configured_feeds(tmp_path: Path, monkeypatch):
    # Favicons are re-checked once, at startup, never on the poll path (see
    # refresh_favicons in favicon.py, wired into main.py's lifespan as a
    # managed task). resolve_favicon is monkeypatched here at the favicon
    # module level -- refresh_favicons calls it as a bare module-global name,
    # resolved dynamically from favicon.py's own __dict__ at call time, so
    # this patch is seen regardless of how main.py imported refresh_favicons
    # itself.
    known_icon = "data:image/png;base64,c3RhcnR1cA=="

    async def fake_resolve_favicon(client, feed_url):
        return known_icon

    monkeypatch.setattr(favicon_mod, "resolve_favicon", fake_resolve_favicon)

    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    app = build(cfg, str(tmp_path / "t.db"), env={})
    with TestClient(app):
        store = app.state.store
        fid = store.upsert_feed("https://a.example/rss")  # same feed as CONFIG's, id stable
        # The startup task runs concurrently with the block above on the
        # TestClient's own event-loop thread; the mocked resolver returns
        # instantly with no real network I/O, so a short bounded poll (not a
        # bare sleep) is enough to observe its result deterministically.
        favicon = None
        for _ in range(200):
            favicon = store.get_feed(fid).favicon
            if favicon is not None:
                break
            time.sleep(0.01)
        assert favicon == known_icon


def test_httpx_request_log_cannot_reach_the_handler_at_the_default_level(monkeypatch):
    # httpx logs "HTTP Request: GET <url> ..." at INFO using str(request.url),
    # which keeps userinfo -- unlike repr. Feed URLs carry credentials, so at
    # the app's default level this logger must not be able to emit that line.
    #
    # Asserting isEnabledFor(INFO) here would be a test that cannot fail: the
    # pytest session's own root logger is already at its ambient level (often
    # WARNING) before main() ever runs, and logging.basicConfig() is a no-op
    # once any handler exists on the root logger -- which one already does by
    # the time tests run. That leaves isEnabledFor(INFO) false regardless of
    # whether main() sets anything at all, verified by mutation: removing the
    # setLevel call in main() left this assertion passing. Assert on the
    # explicit level main() sets on the "httpx" logger itself instead --
    # that attribute is only ever touched by this code, independent of
    # whatever the ambient root logger happens to be configured to.
    logging.getLogger("httpx").setLevel(logging.NOTSET)  # undo any prior test's effect
    monkeypatch.setattr(
        main_mod, "build", lambda *a, **kw: SimpleNamespace(state=SimpleNamespace(bind_host="0.0.0.0"))
    )
    monkeypatch.setattr(main_mod.uvicorn, "run", lambda app, **kw: None)
    main_mod.main()
    assert logging.getLogger("httpx").level == logging.WARNING


def test_request_logging_is_disabled(monkeypatch):
    # Tokens are in the query string and the request line is what an access log
    # records. Request logging belongs at the reverse proxy, which is
    # terminating TLS anyway and can be told to omit query strings.
    captured: dict = {}
    monkeypatch.setattr(
        main_mod, "build", lambda *a, **kw: SimpleNamespace(state=SimpleNamespace(bind_host="0.0.0.0"))
    )
    monkeypatch.setattr(main_mod.uvicorn, "run", lambda app, **kw: captured.update(kw))
    main_mod.main()
    assert captured["access_log"] is False


def test_main_exits_cleanly_on_missing_config(monkeypatch, tmp_path):
    # A missing config mount must exit with a single clean message, not a
    # Python traceback (the whole point of the friendly ConfigError).
    import pytest
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "does-not-exist.yaml"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    from rss_ticker.main import main
    with pytest.raises(SystemExit) as ei:
        main()
    assert ei.value.code not in (0, None)
    assert "config file not found" in str(ei.value.code)


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("no", False),
])
def test_build_reads_health_strict_from_env(tmp_path, value, expected):
    from rss_ticker.main import build
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "public_base_url: http://x\nadmin_key: a\nmanifest_key: m\n"
        "users:\n  - id: art\n    token: " + ("t" * 40) + "\n"
    )
    app = build(cfg, str(tmp_path / "t.db"), env={"HEALTH_STRICT": value})
    assert app.state.health_strict is expected


def test_build_health_strict_defaults_false_when_unset(tmp_path):
    from rss_ticker.main import build
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "public_base_url: http://x\nadmin_key: a\nmanifest_key: m\n"
        "users:\n  - id: art\n    token: " + ("t" * 40) + "\n"
    )
    app = build(cfg, str(tmp_path / "t.db"), env={})
    assert app.state.health_strict is False


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
