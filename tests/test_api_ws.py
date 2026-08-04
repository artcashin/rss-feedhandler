import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rss_ticker.api import create_app
from rss_ticker.broadcast import MAX_QUEUE, Broadcaster
from rss_ticker.config import Config, UserConfig
from rss_ticker.store import NewArticle, Store

TOKEN = "tkn-" + "0123456789abcdef" * 3
GOOD = f"/ws/news?user=art&token={TOKEN}"
LOGIN = "you@github"
IDENT = {"Tailscale-User-Login": LOGIN}
# tailscale_login is inert while tailscale_auth is False -- identity_user
# short-circuits on the flag before ever consulting this map. It is set here
# anyway so a mutation that drops the flag check actually changes this
# fixture's behaviour instead of leaving identity_users empty and the
# mutation silently unobserved; see
# test_ws_ignores_the_identity_header_when_tailscale_auth_is_off.
CFG = Config(
    public_base_url="http://x",
    admin_key="k",
    manifest_key="mk",
    users=(UserConfig(id="art", tailscale_login=LOGIN),),
)
TS_CFG = Config(
    public_base_url="https://t.example",
    admin_key="k",
    tailscale_auth=True,
    bind_host="127.0.0.1",
    users=(UserConfig(id="art", tailscale_login=LOGIN),),
)


@pytest.fixture
def store():
    s = Store(":memory:")
    s.upsert_user("art", None, token=TOKEN)
    yield s
    s.close()


@pytest.fixture
def broadcaster(store):
    return Broadcaster(store)


@pytest.fixture
def client(store, broadcaster):
    return TestClient(create_app(CFG, store, broadcaster))


@pytest.fixture
def ts_client(store, broadcaster):
    return TestClient(create_app(TS_CFG, store, broadcaster))


def test_ws_accepts_known_user_and_registers_subscriber(client, broadcaster):
    with client.websocket_connect(GOOD):
        assert broadcaster.subscriber_count("art") == 1


def test_ws_disconnect_removes_subscriber(client, broadcaster):
    # This only covers the TestClient teardown path: Starlette tears the
    # handler down by cancelling its task, so the `finally` runs even for a
    # handler that never reads the socket. It therefore passes against a
    # broken implementation. The real-socket guarantee -- that a client close
    # unregisters the subscriber under uvicorn, with no publish to force the
    # issue -- is pinned by
    # tests/test_ws_live.py::test_closing_a_real_socket_unregisters_the_subscriber_without_a_publish
    with client.websocket_connect(GOOD):
        pass
    assert broadcaster.subscriber_count("art") == 0


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


def test_ws_unknown_user_is_never_registered(client, broadcaster):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/news?user=nobody") as ws:
            ws.receive_json()
    assert broadcaster.subscriber_count("nobody") == 0


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


def test_ws_closes_with_a_reconnect_code_when_the_subscriber_is_dropped(
    client, store, broadcaster
):
    # A dropped subscriber must not become a zombie socket: the widget only
    # reconnects and gap-fills on a real onclose, and 4401 is reserved for
    # "stop, do not reconnect" -- so the drop has to surface as some other
    # close code. Entering `with client:` shares the TestClient's portal
    # (and therefore its event loop) with the websocket handler task, which
    # lets us drive `broadcaster.publish` on that same loop from here.
    fid = store.upsert_feed("https://x.example/rss", name="X", now=0)
    store.subscribe("art", fid)

    with pytest.raises(WebSocketDisconnect) as exc:
        with client:
            with client.websocket_connect(GOOD) as ws:
                assert broadcaster.subscriber_count("art") == 1
                sub = next(iter(broadcaster._subs["art"]))

                async def overflow_and_publish():
                    for i in range(MAX_QUEUE):
                        sub.queue.put_nowait({"filler": i})
                    arts = store.insert_articles(
                        fid, [NewArticle("a", "Fed holds", None, None, 1)], now=1000
                    )
                    await broadcaster.publish(arts)

                client.portal.call(overflow_and_publish)
                ws.receive_json()

    assert exc.value.code == 1013
    assert exc.value.code != 4401


def test_ws_accepts_a_serve_identity(ts_client, broadcaster):
    with ts_client.websocket_connect("/ws/news?user=art", headers=IDENT):
        assert broadcaster.subscriber_count("art") == 1


def test_ws_rejects_an_identity_for_another_user(ts_client, store, broadcaster):
    store.upsert_user("bob", None)
    with pytest.raises(WebSocketDisconnect) as exc:
        with ts_client.websocket_connect("/ws/news?user=bob", headers=IDENT) as ws:
            # Fails fast if the guard regresses: under the bug the socket is
            # authenticated and subscribed, and the receive below would block
            # forever instead of failing.
            assert broadcaster.subscriber_count("bob") == 0
            ws.receive_json()
    assert exc.value.code == 4401
    assert broadcaster.subscriber_count("bob") == 0


def test_ws_rejects_an_unknown_identity(ts_client, broadcaster):
    with pytest.raises(WebSocketDisconnect) as exc:
        with ts_client.websocket_connect(
            "/ws/news?user=art", headers={"Tailscale-User-Login": "nobody@github"}
        ) as ws:
            # Fails fast if the guard regresses: under the bug the socket is
            # authenticated and subscribed, and the receive below would block
            # forever instead of failing.
            assert broadcaster.subscriber_count("art") == 0
            ws.receive_json()
    assert exc.value.code == 4401
    assert broadcaster.subscriber_count("art") == 0


GHOST_LOGIN = "ghost@github"
TS_CFG_WITH_GHOST = Config(
    public_base_url="https://t.example",
    admin_key="k",
    tailscale_auth=True,
    bind_host="127.0.0.1",
    users=(
        UserConfig(id="art", tailscale_login=LOGIN),
        UserConfig(id="ghost", tailscale_login=GHOST_LOGIN),
    ),
)


@pytest.fixture
def ghost_client(store, broadcaster):
    return TestClient(create_app(TS_CFG_WITH_GHOST, store, broadcaster))


def test_ws_rejects_an_identity_for_a_user_absent_from_the_store(ghost_client, broadcaster):
    # "ghost" resolves through identity_users (built from config) but was
    # never upserted into the store, so store.user_exists("ghost") is False.
    # The REST path already requires `exists` before trusting an identity
    # (require_user_token); this pins the same requirement on the WS path.
    with pytest.raises(WebSocketDisconnect) as exc:
        with ghost_client.websocket_connect(
            "/ws/news?user=ghost", headers={"Tailscale-User-Login": GHOST_LOGIN}
        ) as ws:
            # Fails fast if the guard regresses: under the bug the socket is
            # authenticated and subscribed, and the receive below would block
            # forever instead of failing.
            assert broadcaster.subscriber_count("ghost") == 0
            ws.receive_json()
    assert exc.value.code == 4401
    assert broadcaster.subscriber_count("ghost") == 0


def test_ws_ignores_the_identity_header_when_tailscale_auth_is_off(client, broadcaster):
    # Same security property as the REST path: without the flag the header is
    # attacker-supplied text. Asserted via subscriber_count rather than a
    # receive: if the guard regresses the socket authenticates, and a blocking
    # receive_json() would hang the suite instead of failing it.
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/news?user=art", headers=IDENT) as ws:
            # Fails fast if the guard regresses: under the bug the socket is
            # authenticated and subscribed, and the receive below would block
            # forever instead of failing.
            assert broadcaster.subscriber_count("art") == 0
            ws.receive_json()
    assert exc.value.code == 4401
    assert broadcaster.subscriber_count("art") == 0
