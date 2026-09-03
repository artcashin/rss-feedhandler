import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
import websockets
from fastapi.responses import Response

from rss_ticker.main import build

ROUND1 = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Backfilled</title><guid>urn:1</guid></item></channel></rss>"""

ROUND2 = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Breaking now</title><guid>urn:2</guid></item>
<item><title>Backfilled</title><guid>urn:1</guid></item></channel></rss>"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path: Path, request):
    poll_interval_s = getattr(request, "param", 1)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"default_poll_interval_s: {poll_interval_s}\n")
    app = build(cfg, str(tmp_path / "t.db"), env={})
    served = {"n": 0}

    @app.get("/fixture.xml")
    def fixture() -> Response:
        served["n"] += 1
        return Response(
            content=ROUND1 if served["n"] == 1 else ROUND2, media_type="application/rss+xml"
        )

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        threading.Event().wait(0.1)
    assert server.started, "uvicorn did not start"
    yield base, port, app
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive(), "uvicorn thread did not shut down"


async def subscribe(ws, base: str) -> dict:
    await ws.send(json.dumps({"subscribe": [{"url": f"{base}/fixture.xml", "name": "Fixture"}]}))
    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    assert list(reply) == ["feeds"]
    return reply["feeds"][0]


async def test_subscribe_reply_then_new_article_reaches_a_real_client(live_server):
    base, port, _ = live_server
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/news") as ws:
        record = await subscribe(ws, base)
        assert record["title"] == "Fixture"
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
    msg = json.loads(raw)
    assert msg["title"] == "Breaking now"
    assert msg["feed_id"] == record["id"]
    assert msg["source"] == "Fixture"
    assert "highlighted" not in msg


async def test_backfilled_article_is_pageable_but_was_not_pushed(live_server):
    base, port, _ = live_server
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/news") as ws:
        await subscribe(ws, base)
        assert json.loads(await asyncio.wait_for(ws.recv(), timeout=15))["title"] == "Breaking now"
    import httpx

    async with httpx.AsyncClient(base_url=base) as client:
        titles = [a["title"] for a in (await client.get("/api/news")).json()["articles"]]
    assert "Backfilled" in titles and titles[0] == "Breaking now"


async def wait_for(fn, want, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn() == want:
            return want
        await asyncio.sleep(0.05)
    return fn()


@pytest.mark.parametrize("live_server", [3600], indirect=True)
async def test_closing_a_real_socket_releases_its_feeds_without_a_publish(live_server):
    base, port, app = live_server
    broadcaster = app.state.broadcaster
    ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws/news")
    record = await subscribe(ws, base)
    assert await wait_for(lambda: broadcaster.subscriber_count(record["id"]), 1, 2.0) == 1
    await ws.close()
    assert await wait_for(lambda: broadcaster.subscriber_count(record["id"]), 0, 2.0) == 0
    assert await wait_for(lambda: app.state.store.get_feed(record["id"]).enabled, False, 2.0) is False


@pytest.mark.parametrize("live_server", [3600], indirect=True)
async def test_repeated_connect_disconnect_cycles_do_not_leak(live_server):
    base, port, app = live_server
    broadcaster = app.state.broadcaster
    for _ in range(5):
        ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws/news")
        record = await subscribe(ws, base)
        assert await wait_for(lambda: broadcaster.subscriber_count(record["id"]), 1, 2.0) == 1
        await ws.close()
        assert await wait_for(lambda: broadcaster.subscriber_count(record["id"]), 0, 2.0) == 0
    # The count drops inside unsubscribe, a moment before the session is
    # discarded, and this test observes the server from another thread -- so
    # the session count is waited on rather than read once.
    assert await wait_for(broadcaster.session_count, 0, 2.0) == 0
