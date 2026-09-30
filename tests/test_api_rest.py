from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from rss_ticker.api import DEGRADED_AFTER_FAILURES, create_app
from rss_ticker.broadcast import Broadcaster
from rss_ticker.config import Channel, Config
from rss_ticker.live import LiveTV
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


FIXTURES = Path(__file__).parent / "fixtures"

CHANNELS = (
    Channel("bloomberg-tv", "Bloomberg TV", "@markets"),
    Channel("yahoo-finance", "Yahoo Finance", "@YahooFinance"),
)


def live_client(store, broadcaster, mode="live"):
    def handler(request):
        if mode == "error":
            raise httpx.ConnectError("refused")
        name = "youtube_live.html" if mode == "live" else "youtube_offline.html"
        return httpx.Response(200, text=(FIXTURES / name).read_text())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    live = LiveTV(CHANNELS, client_getter=lambda: http, clock=lambda: 0.0)
    app = create_app(Config(live_tv=CHANNELS), store, broadcaster, live=live)
    return TestClient(app)


def seed(store, n=3, url="https://x.example/rss", name="X"):
    fid = store.upsert_feed(url, name=name, now=0)
    # now is later than every published_at below, so insert_articles' clamp
    # (sort_at = min(published_at, now)) leaves the ordering alone.
    store.insert_articles(
        fid,
        [NewArticle(f"g{i}", f"headline {i}", "https://l", None, 1000 + i) for i in range(n)],
        now=2000,
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


def test_widget_singular_route_is_gone_and_feeds_is_read_only(client):
    # The old iframe widget page stays gone.
    assert client.get("/widget").status_code == 404
    # /api/feeds still exists, read-only: a write to it is method-not-allowed.
    assert client.post("/api/feeds", json={}).status_code == 405
    # The per-feed delete route is gone outright, so the path itself is unknown.
    assert client.delete("/api/feeds/1").status_code == 404


def test_feature_off_widgets_is_empty_and_live_routes_404(client):
    # Default Config() has no live_tv, so create_app got live=None.
    r = client.get("/widgets.json")
    assert r.status_code == 200
    assert r.json() == {}
    assert client.get("/api/live/channels").status_code == 404
    assert client.get("/api/live/video", params={"channel": "bloomberg-tv"}).status_code == 404


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


def test_news_filters_to_one_feed_so_a_quiet_feed_is_never_crowded_out(client, store):
    seed(store, 150, url="https://wire.example/rss", name="Wire")
    quiet = store.upsert_feed("https://quiet.example/feed", name="Quiet", now=0)
    store.insert_articles(quiet, [NewArticle("q", "Weekly note", "https://l", None, 5)], now=2000)
    # Pool-wide, the wire's 150 newer rows bury the quiet feed.
    pool = client.get("/api/news", params={"limit": 100}).json()["articles"]
    assert all(a["feed_id"] != quiet for a in pool)
    # Per feed, it is right there.
    body = client.get("/api/news", params={"feed_id": quiet, "limit": 50}).json()
    assert [a["title"] for a in body["articles"]] == ["Weekly note"]
    assert body["next_cursor"] is None
    assert client.get("/api/news", params={"feed_id": 0}).status_code == 422


def test_widgets_manifest_shape(store, broadcaster):
    c = live_client(store, broadcaster)
    manifest = c.get("/widgets.json").json()
    assert set(manifest) == {"live_tv"}
    w = manifest["live_tv"]
    assert w["name"] == "Live TV"
    assert w["type"] == "youtube"
    assert w["endpoint"] == "/api/live/video"
    assert w["gridData"] == {"w": 20, "h": 12}
    assert len(w["params"]) == 1
    p = w["params"][0]
    assert p["paramName"] == "channel"
    assert p["type"] == "endpoint"
    assert p["label"] == "Channel"
    assert p["optionsEndpoint"] == "/api/live/channels"
    assert p["value"] == "bloomberg-tv"  # the first channel is the default


def test_channels_are_options_in_config_order(store, broadcaster):
    c = live_client(store, broadcaster)
    assert c.get("/api/live/channels").json() == [
        {"label": "Bloomberg TV", "value": "bloomberg-tv"},
        {"label": "Yahoo Finance", "value": "yahoo-finance"},
    ]


def test_video_unknown_channel_is_404(store, broadcaster):
    c = live_client(store, broadcaster)
    assert c.get("/api/live/video", params={"channel": "nope"}).status_code == 404


def test_video_missing_channel_param_is_422(store, broadcaster):
    c = live_client(store, broadcaster)
    assert c.get("/api/live/video").status_code == 422


def test_video_live_returns_watch_url_as_text(store, broadcaster):
    c = live_client(store, broadcaster, mode="live")
    r = c.get("/api/live/video", params={"channel": "bloomberg-tv"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == "https://www.youtube.com/watch?v=livevid0001"


def test_video_off_air_is_empty_200(store, broadcaster):
    c = live_client(store, broadcaster, mode="offline")
    r = c.get("/api/live/video", params={"channel": "bloomberg-tv"})
    assert r.status_code == 200
    assert r.text == ""
