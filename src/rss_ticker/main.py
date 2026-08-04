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
from .favicon import refresh_favicons
from .fetch import TIMEOUT_S
from .poller import Poller
from .reconcile import reconcile
from .store import Store

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 3600

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(env: Mapping[str, str], name: str) -> bool:
    return str(env.get(name, "")).strip().lower() in _TRUTHY


def build(config_path: Path, db_path: str, env: Mapping[str, str] | None = None) -> FastAPI:
    resolved_env = os.environ if env is None else env
    config = load_config(config_path, resolved_env)
    # HEALTH_STRICT=1 makes /api/health return 503 when degraded, so a feed
    # outage trips the container's HEALTHCHECK (off by default -- see the
    # health route for why 200-when-degraded is the safe default).
    health_strict = _env_flag(resolved_env, "HEALTH_STRICT")
    store = Store(db_path)
    try:
        reconcile(store, config, now=int(time.time()))
    except Exception:
        store.close()
        raise
    broadcaster = Broadcaster(store)

    # No scheduled VACUUM, deliberately. VACUUM holds the store lock for
    # seconds-to-tens-of-seconds on a retention-full database, and the
    # poller/broadcaster call store methods synchronously on the event-loop
    # thread -- the first such call during a VACUUM freezes every WebSocket
    # send and accept until it finishes. A retention-bounded database reaches
    # steady-state size and reuses freed pages, so VACUUM buys nothing here.
    async def sweeper() -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            try:
                deleted = await asyncio.to_thread(
                    store.sweep, int(time.time()), config.retention_days
                )
                log.info("Retention sweep removed %d articles", deleted)
            except Exception:
                log.exception("Retention sweep failed")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        client = httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True)
        poller = Poller(store, client, config, on_new_articles=broadcaster.publish)
        tasks = [
            asyncio.create_task(poller.run_forever()),
            asyncio.create_task(sweeper()),
            asyncio.create_task(
                refresh_favicons(store, client, concurrency=config.max_concurrent_polls)
            ),
        ]
        log.info("Serving widgets at %s/widgets.json", config.public_base_url)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()
            store.close()

    app = create_app(
        config, store, broadcaster, lifespan=lifespan, health_strict=health_strict
    )
    app.state.bind_host = config.bind_host
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs every outbound request at INFO using str(request.url), which
    # -- unlike repr -- does not mask userinfo. Feed URLs routinely carry
    # `?apikey=` or `user:token@host` credentials, so at the root level set
    # above, every poll would print them straight to the container log. Do
    # not "fix" this with a filter instead: a filter can silently stop
    # matching after httpx changes its record shape and fail open, printing
    # credentials with no signal. Raising this logger's own level cannot fail
    # that way -- it simply never constructs the record. If you need httpx's
    # request logging back for debugging, do it for a single run, not here.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        app = build(
            Path(os.environ.get("CONFIG_PATH", "/config/config.yaml")),
            os.environ.get("DB_PATH", "/data/ticker.db"),
        )
    except ConfigError as exc:
        # A misconfiguration (most often a missing config mount) is an
        # operator error, not a crash: exit with one clean line instead of a
        # traceback, so `docker logs` shows the fix, not a stack.
        raise SystemExit(f"rss-ticker: {exc}") from None
    uvicorn.run(
        app,
        host=app.state.bind_host,
        port=int(os.environ.get("PORT", "8088")),
        # Tokens travel in the query string, and the request line is what the
        # access log records. A redacting filter on `uvicorn.access` was
        # considered and rejected: it depends on the shape of uvicorn's log
        # record arguments, and a filter that silently stops matching after a
        # dependency bump fails OPEN -- writing tokens to disk with no signal.
        # Turning the logger off cannot fail that way. Request logging belongs
        # at the reverse proxy, configured to omit query strings.
        access_log=False,
    )


if __name__ == "__main__":
    main()
