import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rss_ticker import main as main_mod
from rss_ticker.main import build
from rss_ticker.store import NewArticle

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


def test_a_disabled_feed_is_reused_with_its_history(tmp_path):
    """Spec testing-delta 3: nobody subscribed means disabled, not forgotten.

    No sweep has run (the sweeper's first pass is an hour out), so the row is
    still there when the next client asks for the same URL -- same id, same
    articles, enabled again.
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    db = str(tmp_path / "t.db")
    url = "https://a.example/rss"

    first = build(cfg, db, env={})
    with TestClient(first) as client:
        with client.websocket_connect("/ws/news") as ws:
            ws.send_json({"subscribe": [{"url": url, "name": "A"}]})
            fid = ws.receive_json()["feeds"][0]["id"]
        first.state.store.insert_articles(
            fid, [NewArticle("g", "Kept", "https://l", None, 1000)], now=1000
        )
        # The socket is gone, so the feed sits at zero subscribers, disabled.
        feeds = client.get("/api/feeds").json()["feeds"]
        assert [(f["id"], f["enabled"], f["subscribers"]) for f in feeds] == [(fid, False, 0)]

    with TestClient(build(cfg, db, env={})) as client:
        with client.websocket_connect("/ws/news") as ws:
            ws.send_json({"subscribe": [{"url": url}]})
            reply = ws.receive_json()["feeds"]
            assert [(r["id"], r["title"]) for r in reply] == [(fid, "A")]
            feeds = client.get("/api/feeds").json()["feeds"]
            assert [(f["id"], f["enabled"], f["subscribers"]) for f in feeds] == [(fid, True, 1)]
            assert [a["title"] for a in client.get("/api/news").json()["articles"]] == ["Kept"]
