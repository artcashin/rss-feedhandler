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

TOKEN = "tkn-" + "0123456789abcdef" * 3


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path: Path, request):
    # Indirect param sets the poll interval: tests that must not be rescued by
    # a background broadcast ask for an interval longer than they run.
    poll_interval_s = getattr(request, "param", 1)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
public_base_url: {base}
admin_key: k
manifest_key: mk
default_poll_interval_s: {poll_interval_s}
users:
  - id: art
    token: {TOKEN}
    feeds:
      - {{url: "{base}/fixture.xml", name: Fixture}}
"""
    )
    app = build(cfg, str(tmp_path / "t.db"), env={})
    served = {"n": 0}

    @app.get("/fixture.xml")
    def fixture() -> Response:
        served["n"] += 1
        body = ROUND1 if served["n"] == 1 else ROUND2
        return Response(content=body, media_type="application/rss+xml")

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
    # A handler stuck on an outbound queue keeps uvicorn from finishing its
    # graceful shutdown, which in production means Docker SIGKILLs the process
    # and the lifespan cleanup never runs. Silently burning the join timeout
    # would hide exactly that.
    assert not thread.is_alive(), "uvicorn thread did not shut down"


async def test_new_article_reaches_a_real_websocket_client(live_server):
    base, port, _ = live_server
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/ws/news?user=art&token={TOKEN}"
    ) as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
    msg = json.loads(raw)
    assert msg["title"] == "Breaking now"
    assert msg["source"] == "Fixture"
    assert msg["highlighted"] is False


async def test_backfilled_article_is_pageable_but_was_not_pushed(live_server):
    base, port, _ = live_server
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/ws/news?user=art&token={TOKEN}"
    ) as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
    assert json.loads(raw)["title"] == "Breaking now"

    import httpx

    async with httpx.AsyncClient(base_url=base) as client:
        body = (
            await client.get("/api/news", params={"user": "art", "token": TOKEN})
        ).json()
    titles = [a["title"] for a in body["articles"]]
    assert "Backfilled" in titles
    assert titles[0] == "Breaking now"


async def wait_for_count(broadcaster, user: str, want: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = broadcaster.subscriber_count(user)
        if got == want:
            return got
        await asyncio.sleep(0.05)
    return broadcaster.subscriber_count(user)


# The poll interval is longer than the test so that no article is ever
# published: a handler that only notices a disconnect when it next tries to
# send must not be rescued by a background broadcast.
@pytest.mark.parametrize("live_server", [3600], indirect=True)
async def test_closing_a_real_socket_unregisters_the_subscriber_without_a_publish(
    live_server,
):
    _, port, app = live_server
    broadcaster = app.state.broadcaster

    ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws/news?user=art&token={TOKEN}")
    assert await wait_for_count(broadcaster, "art", 1, 2.0) == 1
    await ws.close()

    assert await wait_for_count(broadcaster, "art", 0, 2.0) == 0, (
        "subscriber still registered 2s after the client closed the socket"
    )


@pytest.mark.parametrize("live_server", [3600], indirect=True)
async def test_repeated_connect_disconnect_cycles_do_not_leak(live_server):
    _, port, app = live_server
    broadcaster = app.state.broadcaster
    url = f"ws://127.0.0.1:{port}/ws/news?user=art&token={TOKEN}"

    for _ in range(5):
        ws = await websockets.connect(url)
        assert await wait_for_count(broadcaster, "art", 1, 2.0) == 1
        await ws.close()
        assert await wait_for_count(broadcaster, "art", 0, 2.0) == 0

    assert broadcaster.subscriber_count("art") == 0
