import re

import pytest
from fastapi.testclient import TestClient

from rss_ticker.api import create_app
from rss_ticker.broadcast import Broadcaster
from rss_ticker.config import Config
from rss_ticker.store import Store

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


def test_widget_is_served_as_html(client):
    r = client.get("/widget", params=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_widget_has_no_external_asset_references(client):
    body = client.get("/widget", params=AUTH).text
    refs = re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', body)
    for ref in refs:
        assert not ref.startswith("http://")
        assert not ref.startswith("https://")
        assert not ref.startswith("//")


def test_widget_has_no_external_script_or_stylesheet_tags(client):
    body = client.get("/widget", params=AUTH).text
    assert "<script src=" not in body
    assert '<link rel="stylesheet"' not in body


def test_widget_opens_links_in_a_new_tab(client):
    body = client.get("/widget", params=AUTH).text
    assert "_blank" in body
    assert "noopener" in body


def test_widget_restricts_link_hrefs_to_http_https(client):
    body = client.get("/widget", params=AUTH).text
    assert "safeHref" in body
    assert re.search(r"/\^https\?:\\/\\/", body) or "^https?://" in body


def test_widget_requires_user(client):
    assert client.get("/widget").status_code == 422


def test_widget_unknown_user_is_401(client):
    # Was 400. Same enumeration oracle as /api/news.
    assert client.get("/widget", params={"user": "nobody"}).status_code == 401


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


def test_widget_sets_a_no_referrer_meta(client):
    body = client.get("/widget", params=AUTH).text
    assert 'name="referrer" content="no-referrer"' in body


def test_widget_has_a_bottom_status_bar_and_feed_settings_and_no_top_header(client):
    body = client.get("/widget", params=AUTH).text
    assert "<header" not in body, "the connection status moved to a bottom bar"
    assert 'id="statusbar"' in body
    assert 'id="gear"' in body
    assert 'id="config"' in body


def test_widget_script_never_logs_a_url(client):
    # console.log/error of a request URL would put the token in the browser
    # console, which is the one place a screenshot reaches.
    body = client.get("/widget", params=AUTH).text
    assert "console.log(" not in body
    assert "console.error(" not in body


def test_widget_script_omits_an_empty_token_param(client):
    # With no token in the iframe URL the widget must send `user=art`, never
    # `user=art&token=` -- a blank credential is noise in every request.
    body = client.get("/widget", params=AUTH).text
    assert 'token ? "&token=" + encodeURIComponent(token) : ""' in body


def test_widget_has_a_tab_bar_above_the_status_bar_and_keeps_the_rest(client):
    body = client.get("/widget", params=AUTH).text
    assert 'id="tabs"' in body
    # Still bottom status bar + gear, unchanged by the tabs feature.
    assert 'id="statusbar"' in body
    assert 'id="gear"' in body
    # The tab bar container must appear before the status bar in the markup,
    # matching the required layout: list, then tabs, then status bar.
    assert body.index('id="tabs"') < body.index('id="statusbar"')
