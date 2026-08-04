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
        # A candidate token must be supplied here, not just omitted: token_ok
        # short-circuits to False on a falsy `provided` before ever looking at
        # `expected`, so a tokenless request can't tell an orphan that stayed
        # closed apart from one reconciliation left open. TOKEN is the value
        # config.yaml assigns to art -- the one a reconciliation bug would be
        # most likely to leak onto an unrelated row.
        assert client.get(
            "/api/news", params={"user": "ghost", "token": TOKEN}
        ).status_code == 401

    store = Store(db_path)
    try:
        assert store.token_for("ghost") is None
    finally:
        store.close()


def test_feeds_table_gains_title_format_on_upgrade(tmp_path: Path):
    """A database from before per-feed title formats opens and migrates."""
    db_path = str(tmp_path / "t.db")
    db = sqlite3.connect(db_path)
    db.execute(
        "CREATE TABLE feeds (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, "
        "name TEXT, poll_interval_s INTEGER, enabled INTEGER NOT NULL DEFAULT 1)"
    )
    db.execute("INSERT INTO feeds (url, name) VALUES ('https://a.example/rss', 'A')")
    db.commit()
    db.close()

    store = Store(db_path)
    try:
        feed = store.all_feeds()[0]
        assert feed.title_format is None
        store.upsert_feed("https://a.example/rss", title_format="{title} - {author}")
        assert store.all_feeds()[0].title_format == "{title} - {author}"
    finally:
        store.close()
