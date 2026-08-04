from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import __version__
from .broadcast import Broadcaster, article_payload
from .config import Config
from .fetch import redact_feed_url
from .filters import highlights
from .store import CursorError, Store

ALLOWED_ORIGINS = [
    "https://pro.openbb.co",
    "https://pro.openbb.dev",
    "https://excel.openbb.co",
    "http://localhost:1420",
]

STATIC = Path(__file__).parent / "static"

log = logging.getLogger(__name__)


INVALID_CREDENTIALS = "Invalid credentials"

# Compared against when a user has no stored token, so that path does the same
# work as a wrong-token path. Random per process; never returned or logged.
_DECOY_TOKEN = secrets.token_urlsafe(32)


# A single failed poll is a network blip, not an unhealthy deployment. With
# `restart: unless-stopped` in front of the container, flipping on the first
# failure is a restart loop caused by someone else's flaky feed.
DEGRADED_AFTER_FAILURES = 3


def secret_ok(provided: str | None, expected: str | None) -> bool:
    """Constant-time secret check that never raises on caller-controlled input.

    Both operands are encoded before comparison: hmac.compare_digest raises
    TypeError on str operands containing non-ASCII characters, and every
    credential here arrives from the network. An empty or missing expected
    value never matches -- compare_digest(b"", b"") is True, so a user row with
    a NULL token would otherwise be an open account.
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def token_ok(provided: str | None, expected: str | None) -> bool:
    """Constant-work token check.

    `expected` is None for a user that does not exist. Returning early there
    would make an unknown user measurably faster than a wrong token, which is
    the user-id oracle this endpoint exists to close, wearing a stopwatch.
    Compare against a decoy of the same shape instead and discard the result.
    """
    matched = secret_ok(provided, expected or _DECOY_TOKEN)
    return matched and bool(expected)


def admin_key_ok(provided: str | None, expected: str) -> bool:
    return secret_ok(provided, expected)


def bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


class FeedCreate(BaseModel):
    user: str
    url: str
    name: str | None = None
    # ge=1: a non-positive interval becomes a once-per-tick hammer loop that
    # backoff can never slow down (see config._feed for the arithmetic).
    poll_interval_s: int | None = Field(default=None, ge=1)
    group: str | None = None
    title_format: str | None = None


def create_app(
    config: Config,
    store: Store,
    broadcaster: Broadcaster,
    lifespan=None,
    health_strict: bool = False,
) -> FastAPI:
    app = FastAPI(title="rss-ticker", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.config = config
    app.state.store = store
    app.state.broadcaster = broadcaster
    app.state.health_strict = health_strict

    def is_admin(x_admin_key: str | None = Header(default=None)) -> bool:
        return secret_ok(x_admin_key, config.admin_key)

    def require_admin(admin: bool = Depends(is_admin)) -> None:
        if not admin:
            raise HTTPException(status_code=401, detail="Admin key required")

    # Serve-supplied login -> configured user id, built once at startup.
    # Tailscale Serve overwrites any client-supplied Tailscale-User-* header
    # (verified 2026-07-27), so this value cannot be forged THROUGH Serve --
    # but it is forgeable by anything that reaches this server around Serve,
    # which is why tailscale_auth demands a loopback bind (see config.py).
    identity_users = {u.tailscale_login: u.id for u in config.users if u.tailscale_login}

    def identity_user(login: str | None) -> str | None:
        """Resolve a Serve identity to a user id, or None.

        Returns None whenever tailscale_auth is off, so a deployment that has
        not opted in can never be authenticated by a header a client typed.
        """
        if not config.tailscale_auth or not login:
            return None
        return identity_users.get(login)

    def require_manifest_key(
        x_api_key: str | None = Header(default=None),
        tailscale_user_login: str | None = Header(default=None),
    ) -> None:
        # X-API-KEY is OpenBB Workspace's documented convention for a custom
        # backend's key; its value here is manifest_key, deliberately NOT
        # admin_key -- this is the secret that leaves the operator's control.
        # Under tailscale_auth the caller's own Serve identity is accepted too,
        # which is what lets the manifest carry no tokens at all.
        if identity_user(tailscale_user_login):
            return
        if not secret_ok(x_api_key, config.manifest_key):
            raise HTTPException(status_code=401, detail="API key required")

    def require_user(user: str) -> str:
        # Write endpoints only. Their caller holds the admin key and can already
        # list every user, so naming an unknown one leaks nothing.
        if not store.user_exists(user):
            raise HTTPException(status_code=400, detail=f"Request from unknown user {user}")
        return user

    def require_user_token(
        user: str = Query(...),
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
        tailscale_user_login: str | None = Header(default=None),
        admin: bool = Depends(is_admin),
    ) -> str:
        # Both lookups run on every path so that an unknown user and a wrong
        # token cost the same. The rejection is identical in status and body.
        exists = store.user_exists(user)
        expected = store.token_for(user)
        if admin:
            if not exists:
                raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)
            return user
        if exists and identity_user(tailscale_user_login) == user:
            return user
        if not token_ok(bearer_token(authorization) or token, expected):
            raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)
        return user

    @app.get("/")
    def root() -> dict:
        return {
            "service": "rss-ticker",
            "version": __version__,
            "widgets": f"{config.public_base_url}/widgets.json",
        }

    @app.get("/widgets.json", dependencies=[Depends(require_manifest_key)])
    def widgets_manifest() -> dict:
        from .widgets import render_widgets

        return render_widgets(config)

    @app.get("/api/news")
    def news(
        user: str = Depends(require_user_token),
        limit: int = Query(50, ge=1, le=200),
        before: str | None = Query(None),
        after: str | None = Query(None),
    ) -> dict:
        if before and after:
            raise HTTPException(status_code=400, detail="Pass before or after, not both")
        try:
            articles, next_cursor = store.page_news(
                user, limit=limit, before=before, after=after
            )
        except CursorError:
            raise HTTPException(status_code=400, detail="Cursor is not valid") from None

        rules = store.filters_for(user)
        names: dict[int, str | None] = {}
        payloads = []
        for article in articles:
            if article.feed_id not in names:
                feed = store.get_feed(article.feed_id)
                names[article.feed_id] = feed.name if feed else None
            highlighted = highlights(rules, article.title, article.summary)
            payloads.append(article_payload(article, names[article.feed_id], highlighted))
        return {"articles": payloads, "next_cursor": next_cursor}

    @app.get("/api/feeds")
    def list_feeds(
        user: str = Depends(require_user_token),
        admin: bool = Depends(is_admin),
    ) -> dict:
        return {
            "feeds": [
                {
                    "id": f.id,
                    "url": f.url if admin else redact_feed_url(f.url),
                    "name": f.name,
                    "poll_interval_s": f.poll_interval_s,
                    "enabled": f.enabled,
                    "favicon": f.favicon,
                    "group": f.group,
                    "title_format": f.title_format,
                }
                for f in store.list_feeds(user)
            ]
        }

    @app.post("/api/feeds", status_code=201, dependencies=[Depends(require_admin)])
    def add_feed(body: FeedCreate) -> dict:
        require_user(body.user)
        feed_id = store.upsert_feed(
            body.url,
            name=body.name,
            poll_interval_s=body.poll_interval_s,
            now=int(time.time()),
            group=body.group,
            title_format=body.title_format,
        )
        store.subscribe(body.user, feed_id)
        return {"id": feed_id, "url": body.url}

    @app.delete("/api/feeds/{feed_id}", status_code=204,
                dependencies=[Depends(require_admin)])
    def remove_feed(feed_id: int, user: str = Query(...)) -> Response:
        require_user(user)
        if not store.unsubscribe(user, feed_id):
            raise HTTPException(status_code=404, detail="User is not subscribed to that feed")
        return Response(status_code=204)

    @app.get("/api/health")
    def health(response: Response, admin: bool = Depends(is_admin)) -> dict:
        feeds = store.all_feed_status()
        degraded = any(
            f["enabled"] and f["consecutive_failures"] >= DEGRADED_AFTER_FAILURES
            for f in feeds
        )
        # Default: 200 even when degraded, so a transient feed blip never makes
        # an orchestrator kill or restart the container. HEALTH_STRICT flips
        # this to 503 when degraded, so the container's own HEALTHCHECK trips
        # and the NAS container manager surfaces it as an event. The
        # body is identical either way -- only the status code changes.
        if degraded and health_strict:
            response.status_code = 503
        body: dict = {
            "status": "degraded" if degraded else "ok",
            "version": __version__,
            # Not a secret -- the caller already knows the host -- and the only
            # symptom of a wrong base URL, which otherwise shows as a blank frame.
            "public_base_url": config.public_base_url,
        }
        if admin:
            body["feeds"] = feeds
        return body

    @app.get("/widget", response_class=HTMLResponse)
    def widget(user: str = Depends(require_user_token)) -> HTMLResponse:
        return HTMLResponse(
            (STATIC / "widget.html").read_text(),
            headers={
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            },
        )

    @app.websocket("/ws/news")
    async def ws_news(websocket: WebSocket) -> None:
        user = websocket.query_params.get("user")
        token = websocket.query_params.get("token")
        # A browser cannot set headers on a WebSocket, but Tailscale Serve
        # injects the identity on the upgrade request itself (verified), which
        # is what lets the socket authenticate with no token in the URL.
        login = websocket.headers.get("tailscale-user-login")
        await websocket.accept()
        # Mirrors require_user_token's `exists and identity_user(...) == user`:
        # an identity resolving to a user absent from the store must not be
        # accepted on this path either, even though today every config user
        # is upserted by reconcile before the map is built.
        exists = bool(user) and store.user_exists(user)
        if not user or (
            not (exists and identity_user(login) == user)
            and not token_ok(token, store.token_for(user))
        ):
            # One code and one reason for "no such user", "wrong identity" and
            # "wrong token": a distinct code would be the enumeration oracle
            # over again. Reject before subscribing -- a subscription
            # registered here and torn down on the next line can still be
            # handed a frame.
            await websocket.close(code=4401, reason="Invalid credentials")
            return

        sub = broadcaster.subscribe(user)
        # The client never sends us anything, but the socket must still be read:
        # without a pending receive() a disconnect is invisible until the next
        # publish, which leaks a subscription per closed tab and blocks shutdown.
        receiver = asyncio.create_task(websocket.receive())
        sender = asyncio.create_task(sub.queue.get())
        # A full queue means publish() gave up on this subscriber and can no
        # longer hand it a sentinel through the queue itself -- it signals the
        # drop here instead, so the handler can close the socket and let the
        # client's own reconnect/gap-fill logic recover what it missed.
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
                    message = receiver.result()  # raises WebSocketDisconnect on close
                    if message.get("type") == "websocket.disconnect":
                        break
                    receiver = asyncio.create_task(websocket.receive())
                if sender in done:
                    await websocket.send_json(sender.result())
                    sender = asyncio.create_task(sub.queue.get())
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Websocket handler failed for user %s", user)
        finally:
            for task in (receiver, sender, closer):
                task.cancel()
            broadcaster.unsubscribe(sub)

    return app
