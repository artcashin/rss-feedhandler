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
        # urlsplit raises ValueError on an unclosed IPv6 literal ...
        {"subscribe": [{"url": "http://[oops"}]},
        # ... and json.loads raises RecursionError on deep nesting. Both used
        # to escape through the handler's except clause as a bare 1006.
        pytest.param("[" * 200000, id="deep-json"),
    ],
)
def test_an_invalid_subscribe_frame_closes_4400_and_registers_nothing(
    client, store, broadcaster, frame
):
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
                    fid,
                    [NewArticle("g", "Fed holds", "https://l", None, 1, author="Jane")],
                    now=1000,
                )
                await broadcaster.publish(arts)

            client.portal.call(publish)
            msg = ws.receive_json()
    assert (msg["feed_id"], msg["title"], msg["author"], msg["source"]) == (
        fid,
        "Fed holds",
        "Jane",
        "A",
    )
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
                    arts = store.insert_articles(
                        fid, [NewArticle("a", "Fed holds", None, None, 1)], now=1000
                    )
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
