from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path
from typing import Mapping

import httpx
import uvicorn
from fastapi import FastAPI

from .api import create_app
from .broadcast import Broadcaster
from .config import ConfigError, load_config
from .favicon import refresh_favicons, resolve_and_store
from .fetch import TIMEOUT_S
from .poller import Poller
from .store import Feed, Store

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 3600

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(env: Mapping[str, str], name: str) -> bool:
    return str(env.get(name, "")).strip().lower() in _TRUTHY


def build(config_path: Path, db_path: str, env: Mapping[str, str] | None = None) -> FastAPI:
    resolved_env = os.environ if env is None else env
    config = load_config(config_path, resolved_env)
    health_strict = _env_flag(resolved_env, "HEALTH_STRICT")
    store = Store(db_path)
    # Nobody is connected at boot, so nothing is polled until a socket asks
    # (decision B). Clients reconnect within seconds and re-enable their feeds.
    store.disable_all_feeds()
    broadcaster = Broadcaster(store)

    # The HTTP client is created in the lifespan; the subscribe path needs it
    # for a new feed's favicon, so it is handed over through this holder.
    holder: dict[str, httpx.AsyncClient] = {}
    # asyncio holds only a weak reference to a bare create_task, so a
    # fire-and-forget favicon task can be collected mid-flight. Keep a strong
    # reference until it finishes.
    background: set[asyncio.Task] = set()

    def on_feed_added(feed: Feed) -> None:
        client = holder.get("client")
        if client is None:
            return
        task = asyncio.create_task(resolve_and_store(store, client, feed))
        background.add(task)
        task.add_done_callback(background.discard)

    async def sweeper() -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            try:
                deleted = await asyncio.to_thread(
                    store.sweep, int(time.time()), config.retention_days
                )
                dropped = await asyncio.to_thread(store.drop_disabled_feeds)
                log.info("Retention sweep removed %d articles and %d idle feeds", deleted, dropped)
            except Exception:
                log.exception("Retention sweep failed")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        client = httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True)
        holder["client"] = client
        poller = Poller(store, client, config, on_new_articles=broadcaster.publish)
        tasks = [
            asyncio.create_task(poller.run_forever()),
            asyncio.create_task(sweeper()),
            asyncio.create_task(
                refresh_favicons(store, client, concurrency=config.max_concurrent_polls)
            ),
        ]
        try:
            yield
        finally:
            holder.pop("client", None)
            for task in (*tasks, *background):
                task.cancel()
            await asyncio.gather(*tasks, *background, return_exceptions=True)
            await client.aclose()
            store.close()

    app = create_app(
        config,
        store,
        broadcaster,
        lifespan=lifespan,
        health_strict=health_strict,
        on_feed_added=on_feed_added,
    )
    app.state.bind_host = config.bind_host
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs every outbound request URL at INFO, and feed URLs can carry
    # credentials; keep it quiet (see the base design's logging notes).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        app = build(
            Path(os.environ.get("CONFIG_PATH", "/config/config.yaml")),
            os.environ.get("DB_PATH", "/data/ticker.db"),
        )
    except ConfigError as exc:
        raise SystemExit(f"rss-ticker: {exc}") from None
    # Access logging is back on: nothing secret rides a request line any more.
    uvicorn.run(app, host=app.state.bind_host, port=int(os.environ.get("PORT", "8088")))


if __name__ == "__main__":
    main()
