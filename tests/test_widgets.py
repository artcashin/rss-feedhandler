import pytest
from fastapi.testclient import TestClient

from rss_ticker.api import create_app
from rss_ticker.broadcast import Broadcaster
from rss_ticker.config import Config, FeedConfig, UserConfig
from rss_ticker.store import Store
from rss_ticker.widgets import render_widgets

TOKEN = "tkn-" + "0123456789abcdef" * 3
MANIFEST = {"X-API-KEY": "mk"}

CFG = Config(
    public_base_url="http://nas.local:8088",
    admin_key="s3cret",
    manifest_key="mk",
    users=(UserConfig(id="art", name="Art", token=TOKEN),),
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
    r = client.get("/widgets.json", headers=MANIFEST)
    assert r.status_code == 200
    assert "news_window_art" in r.json()


def test_no_users_yields_empty_manifest():
    empty = Config(public_base_url="http://x", admin_key="k", manifest_key="mk")
    assert render_widgets(empty) == {}


def test_endpoint_url_encodes_unsafe_user_id():
    # Config is a plain frozen dataclass with no validation of its own, so a
    # caller (or a future code path) can construct one directly with an
    # unsafe id, bypassing load_config's slug check entirely.
    cfg = Config(
        public_base_url="http://nas.local:8088",
        admin_key="k",
        manifest_key="mk",
        users=(UserConfig(id="a b&c"),),
    )
    manifest = render_widgets(cfg)
    endpoint = manifest["news_window_a b&c"]["endpoint"]
    assert "a%20b%26c" in endpoint
    # Split on the literal "&token=" boundary, not a bare "&": the user id is
    # attacker-controlled, so if it were ever interpolated unencoded, an id
    # like "art&admin=1" would inject its own "&" into the query string. A
    # split on a bare "&" would just cut in a different place -- the isolated
    # user_value would still be "&"-free, making the assertion below vacuous.
    # Anchoring on "&token=" keeps the whole (unencoded) user segment intact.
    user_value = endpoint.split("user=", 1)[1].split("&token=", 1)[0]
    assert "&" not in user_value
    assert " " not in user_value


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


def test_an_empty_manifest_key_opens_nothing():
    # Config is a plain dataclass with a "" default; only load_config requires
    # the key. An empty expected secret must match nothing -- including an
    # empty or absent header, since compare_digest(b"", b"") is True.
    cfg = Config(public_base_url="http://x", admin_key="k", manifest_key="")
    store = Store(":memory:")
    client = TestClient(create_app(cfg, store, Broadcaster(store)))
    try:
        assert client.get("/widgets.json").status_code == 401
        assert client.get("/widgets.json", headers={"X-API-KEY": ""}).status_code == 401
    finally:
        store.close()


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


def test_a_users_real_token_never_leaks_under_tailscale_auth():
    # The brief's own token-free test uses a user with no token at all
    # (tailscale_login only), so a buggy fix that keys off "does this user
    # have a token" rather than off config.tailscale_auth would pass that
    # test spuriously -- there's nothing for it to leak. This fixture gives
    # the user a real token *and* turns tailscale_auth on, so the only way
    # to pass is to key on the flag, per the Task 2 finding: under
    # tailscale_auth every endpoint must be token-free regardless of
    # whether the user happens to have one configured.
    real_token = "tkn-" + "0123456789abcdef" * 3
    cfg = Config(
        public_base_url="https://t.example",
        admin_key="k",
        tailscale_auth=True,
        bind_host="127.0.0.1",
        users=(UserConfig(id="art", tailscale_login="you@github", token=real_token),),
    )
    endpoint = render_widgets(cfg)["news_window_art"]["endpoint"]
    assert "token" not in endpoint
    assert real_token not in endpoint


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


def test_grid_data_has_min_bounds_so_the_widget_cannot_be_squashed():
    # A row is ~31px; resized below a few grid units the headline list is all
    # scrollbar and no content. Workspace enforces minW/minH on the drag handle.
    m = render_widgets(CFG)
    window = m["news_window_art"]["gridData"]
    rail = m["news_rail_art"]["gridData"]
    assert window["minW"] == 12 and window["minH"] == 4
    # The rail is deliberately short, so its floor is its natural height.
    assert rail["minW"] == 12 and rail["minH"] == 2
    # The starting size is unchanged.
    assert window["w"] == 40 and window["h"] == 8
    assert rail["w"] == 40 and rail["h"] == 2


def test_sub_category_lists_the_users_feed_groups_for_search():
    # subCategory's documented purpose is "refining search results", so it
    # carries what the widget actually contains -- searching "Substack" in the
    # widget picker should surface this ticker.
    cfg = Config(
        public_base_url="http://x",
        admin_key="k",
        manifest_key="mk",
        users=(
            UserConfig(
                id="art",
                token="tkn-" + "0123456789abcdef" * 3,
                feeds=(
                    FeedConfig(url="https://a.example/rss", group="Markets"),
                    FeedConfig(url="https://b.example/rss", group="Substack"),
                    FeedConfig(url="https://c.example/rss", group="Markets"),
                ),
            ),
        ),
    )
    assert render_widgets(cfg)["news_window_art"]["subCategory"] == "Markets, Substack"


def test_sub_category_is_omitted_when_no_feed_has_a_group():
    cfg = Config(
        public_base_url="http://x",
        admin_key="k",
        manifest_key="mk",
        users=(
            UserConfig(
                id="art",
                token="tkn-" + "0123456789abcdef" * 3,
                feeds=(FeedConfig(url="https://a.example/rss"),),
            ),
        ),
    )
    assert "subCategory" not in render_widgets(cfg)["news_window_art"]
