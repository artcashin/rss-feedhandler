from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .broadcast import Broadcaster, article_payload
from .config import Config
from .live import LiveTV, wrapper_html
from .store import CursorError, Feed, Store

# bdobb-v2's origins: the Vite dev server, its browser-mode e2e server, and
# the Tauri webview on macOS/iOS and on Windows. No credentials -- there are
# no cookies and no keys.
ALLOWED_ORIGINS = [
    "http://localhost:1420",
    "http://localhost:4173",
    "tauri://localhost",
    "http://tauri.localhost",
]

log = logging.getLogger(__name__)

# A single failed poll is a network blip, not an unhealthy deployment.
DEGRADED_AFTER_FAILURES = 3

# Bounds on the one thing a client can make this server do: poll a URL.
MAX_SUBSCRIBE_URLS = 200
MAX_URL_LEN = 2048

# Close code for a malformed subscribe frame. Not 1003/1008 (which some
# clients treat as terminal) and not 4401 (the retired auth code).
INVALID_SUBSCRIBE = 4400

OnFeedAdded = Callable[[Feed], None]


def parse_subscribe(frame: object) -> list[tuple[str, str | None]] | None:
    """The (url, name) pairs of a subscribe frame, or None if it is not one.

    Hostile input: every field is checked by type and size before use.
    """
    if not isinstance(frame, dict) or not isinstance(frame.get("subscribe"), list):
        return None
    entries = frame["subscribe"]
    if len(entries) > MAX_SUBSCRIBE_URLS:
        return None
    out: list[tuple[str, str | None]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
            return None
        url = entry["url"].strip()
        if not url or len(url) > MAX_URL_LEN:
            return None
        try:
            parts = urlsplit(url)
            scheme, host = parts.scheme.lower(), parts.hostname
        except ValueError:
            # An unparseable authority (`http://[oops`) raises rather than
            # returning parts; that is a malformed frame, not a server error.
            return None
        if scheme not in ("http", "https") or not host:
            return None
        name = entry.get("name")
        if name is not None and not isinstance(name, str):
            return None
        name = name.strip() if name else None
        out.append((url, name or None))
    return out


def feed_record(feed: Feed) -> dict:
    return {"id": feed.id, "url": feed.url, "title": feed.name, "favicon": feed.favicon}


def live_widgets_manifest(channels) -> dict:
    """The OpenBB Workspace widgets manifest for the Live TV widget.

    Empty when no channels are configured: an empty dict is a valid manifest
    ("this backend publishes no widgets"), which OpenBB clients accept, whereas
    a 404 would surface as a backend error. So /widgets.json is always 200 and
    the *feature* being off is expressed as {} rather than a missing route.
    """
    if not channels:
        return {}
    return {
        "live_tv": {
            "name": "Live TV",
            "type": "youtube",
            "endpoint": "/api/live/video",
            "gridData": {"w": 20, "h": 12},
            "params": [
                {
                    "paramName": "channel",
                    "type": "endpoint",
                    "label": "Channel",
                    "optionsEndpoint": "/api/live/channels",
                    "value": channels[0].key,
                }
            ],
        }
    }


def create_app(
    config: Config,
    store: Store,
    broadcaster: Broadcaster,
    lifespan=None,
    health_strict: bool = False,
    on_feed_added: OnFeedAdded | None = None,
    live: LiveTV | None = None,
) -> FastAPI:
    app = FastAPI(title="rss-ticker", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.config = config
    app.state.store = store
    app.state.broadcaster = broadcaster
    app.state.health_strict = health_strict
    app.state.live = live

    @app.get("/")
    def root() -> dict:
        return {"service": "rss-ticker", "version": __version__}

    @app.get("/api/news")
    def news(
        limit: int = Query(50, ge=1, le=200),
        before: str | None = Query(None),
        after: str | None = Query(None),
        feed_id: int | None = Query(None, ge=1),
    ) -> dict:
        if before and after:
            raise HTTPException(status_code=400, detail="Pass before or after, not both")
        try:
            articles, next_cursor = store.page_news(
                limit=limit, before=before, after=after, feed_id=feed_id
            )
        except CursorError:
            raise HTTPException(status_code=400, detail="Cursor is not valid") from None
        names: dict[int, str | None] = {}
        payloads = []
        for article in articles:
            if article.feed_id not in names:
                feed = store.get_feed(article.feed_id)
                names[article.feed_id] = feed.name if feed else None
            payloads.append(article_payload(article, names[article.feed_id]))
        return {"articles": payloads, "next_cursor": next_cursor}

    @app.get("/api/feeds")
    def list_feeds() -> dict:
        return {
            "feeds": [
                {
                    **feed_record(f),
                    "subscribers": broadcaster.subscriber_count(f.id),
                    "enabled": f.enabled,
                }
                for f in store.all_feeds()
            ]
        }

    @app.get("/api/health")
    def health(response: Response) -> dict:
        feeds = store.all_feed_status()
        degraded = any(
            f["enabled"] and f["consecutive_failures"] >= DEGRADED_AFTER_FAILURES for f in feeds
        )
        if degraded and health_strict:
            response.status_code = 503
        return {
            "status": "degraded" if degraded else "ok",
            "version": __version__,
            "sessions": broadcaster.session_count(),
            "feeds": feeds,
        }

    @app.get("/widgets.json")
    def widgets_manifest() -> dict:
        # Open like every other route (this server has no auth by design). When
        # live is None or has no channels, this is {}.
        channels = live.channels if live is not None else ()
        return live_widgets_manifest(channels)

    @app.get("/api/live/channels")
    def live_channels() -> list[dict]:
        if live is None or not live.channels:
            raise HTTPException(status_code=404, detail="live tv is not configured")
        # OpenBB option shape: {label, value}, in config order.
        return [{"label": c.label, "value": c.key} for c in live.channels]

    @app.get("/api/live/video")
    async def live_video(channel: str = Query(...)) -> Response:
        if live is None or channel not in live:
            # Only configured keys are accepted, so this endpoint can never be
            # driven to fetch an arbitrary URL.
            raise HTTPException(status_code=404, detail="unknown channel")
        vid = await live.video_id(channel)
        # text/plain, matching the card's contract: a watch URL when live, an
        # empty body (still 200) when off-air or the lookup has nothing.
        body = f"https://www.youtube.com/watch?v={vid}" if vid else ""
        return Response(content=body, media_type="text/plain")

    @app.get("/yt")
    async def live_wrapper(channel: str = Query(...)) -> HTMLResponse:
        # The Live TV wrapper page (see live.wrapper_html). Same key rule as
        # /api/live/video: only configured channels, so no arbitrary fetch.
        if live is None or channel not in live:
            raise HTTPException(status_code=404, detail="unknown channel")
        vid = await live.video_id(channel)
        label = next(c.label for c in live.channels if c.key == channel)
        return HTMLResponse(
            wrapper_html(label, vid),
            # The embed's Referer must be this origin; say so explicitly rather
            # than rely on the browser default. no-cache: the live id changes
            # when a stream restarts, and the server already caches it 5 min.
            headers={"Referrer-Policy": "strict-origin-when-cross-origin", "Cache-Control": "no-cache"},
        )

    def resolve_subscription(entries: list[tuple[str, str | None]]) -> tuple[list[dict], set[int]]:
        """Upsert each URL into the pool; report which were new."""
        records: list[dict] = []
        ids: set[int] = set()
        now = int(time.time())
        for url, name in entries:
            existing = store.feed_by_url(url)
            feed_id = store.upsert_feed(url, name=name, now=now)
            if feed_id in ids:
                continue
            ids.add(feed_id)
            feed = store.get_feed(feed_id)
            if feed is None:
                # The sweep can drop a disabled feed between the upsert and
                # this read. Nothing to report for it; the next frame will
                # add it back.
                continue
            records.append(feed_record(feed))
            if existing is None and on_feed_added is not None:
                on_feed_added(feed)
        return records, ids

    @app.websocket("/ws/news")
    async def ws_news(websocket: WebSocket) -> None:
        await websocket.accept()
        sub = broadcaster.subscribe()
        receiver = asyncio.create_task(websocket.receive())
        sender = asyncio.create_task(sub.queue.get())
        closer = asyncio.create_task(sub.closed.wait())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {receiver, sender, closer}, return_when=asyncio.FIRST_COMPLETED
                )
                if closer in done:
                    await websocket.close(code=1013, reason="Subscriber queue overflowed")
                    break
                if receiver in done:
                    message = receiver.result()
                    if message.get("type") == "websocket.disconnect":
                        break
                    text = message.get("text")
                    frame: object = None
                    if isinstance(text, str):
                        try:
                            frame = json.loads(text)
                        except (ValueError, RecursionError):
                            # Deeply nested JSON (`[` x 200000) blows the
                            # recursion limit inside the decoder; that is a
                            # malformed frame, so it closes 4400 like any
                            # other rather than falling out as a 1006.
                            frame = None
                    entries = parse_subscribe(frame)
                    if entries is None:
                        await websocket.close(
                            code=INVALID_SUBSCRIBE, reason="Expected a subscribe frame"
                        )
                        break
                    records, ids = resolve_subscription(entries)
                    broadcaster.set_feeds(sub, ids)
                    await websocket.send_json({"feeds": records})
                    receiver = asyncio.create_task(websocket.receive())
                if sender in done:
                    await websocket.send_json(sender.result())
                    sender = asyncio.create_task(sub.queue.get())
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Websocket handler failed")
        finally:
            for task in (receiver, sender, closer):
                task.cancel()
            broadcaster.unsubscribe(sub)

    return app
