from __future__ import annotations

import asyncio
import email.utils
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

MAX_BACKOFF_S = 3600
# Floor for a server-supplied Retry-After. A broken or hostile server can
# send Retry-After: 0 (or 1, or 2) on a 429/503, which without a floor would
# re-poll on the very next tick -- a ~1-second hammer loop against that
# server. 5s is small enough to still respect any real Retry-After at or
# above it, but stops a degenerate near-zero value from becoming a busy loop.
MIN_RETRY_AFTER_S = 5
MAX_BODY_BYTES = 5 * 1024 * 1024
# Per-operation timeout: httpx applies this separately to each connect, each
# read, and each write. It does NOT bound the request as a whole -- a server
# that keeps sending a trickle of bytes (or a fresh chunk) more often than
# every TIMEOUT_S seconds never trips this, and client.stream() will keep
# reading forever.
TIMEOUT_S = 15.0
# Total wall-clock deadline for the entire fetch (connect + all reads + the
# body-accumulation loop), independent of how httpx schedules individual
# operations. This is what actually protects the poller from a stalled feed:
# without it, one dribbling server can hang fetch_feed indefinitely, which
# blocks every other feed behind it (poller.run_once awaits all due feeds).
# 60s is comfortably above TIMEOUT_S (15s) so a slow-but-healthy feed -- one
# retry-able connect plus a large-but-legitimate body streamed in several
# chunks -- still has room to finish, while a stalled feed is only ever
# wedged for well under a minute instead of hours or days.
TOTAL_TIMEOUT_S = 60.0


def user_agent(version: str) -> str:
    return f"rss-ticker/{version} (+https://github.com/artcashin/rss-feedhandler)"


def redact_feed_url(url: str) -> str:
    """Reduce a feed URL to scheme and host.

    Feed URLs routinely carry credentials -- `?apikey=`, a signed path segment,
    or `user:secret@host`. `.hostname` is used rather than `.netloc` precisely
    because netloc keeps the userinfo. `.port` raises ValueError for a
    non-numeric port (e.g. `host:abc`), so that access is guarded too --
    anything unparseable redacts rather than raising.

    Lives here because the poller's log lines need it -- feed.url must stay
    out of every poll line -- and fetch.py is the lowest-level module that
    already speaks in feed URLs, with no dependency on its caller. Nothing in
    the API redacts any more: `/api/feeds` returns feed URLs verbatim to
    whoever can reach the port.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        return "(redacted)"
    if port:
        host = f"{host}:{port}"
    if not parts.scheme or not host:
        return "(redacted)"
    return f"{parts.scheme}://{host}"


@dataclass(frozen=True)
class FetchOutcome:
    status: str
    body: bytes | None = None
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    retry_after: int | None = None


def _retry_after(response: httpx.Response) -> int | None:
    """Seconds the server asked us to wait, or None.

    RFC 9110 allows two forms: delta-seconds and an HTTP-date. Real CDNs use
    both. The date form converts to a delta against the local clock; a date
    in the past (or clock skew) yields a small/negative delta, which the
    MIN_RETRY_AFTER_S floor in next_interval clamps up.
    """
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return int(when.timestamp() - time.time())


async def fetch_feed(
    client: httpx.AsyncClient,
    url: str,
    etag: str | None,
    last_modified: str | None,
) -> FetchOutcome:
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_S):
            async with client.stream(
                "GET", url, headers=headers, timeout=TIMEOUT_S, follow_redirects=True
            ) as response:
                if response.status_code == 304:
                    return FetchOutcome(status="not_modified")

                if response.status_code >= 400:
                    return FetchOutcome(
                        status="failed",
                        error=f"http {response.status_code}",
                        retry_after=_retry_after(response),
                    )

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_BODY_BYTES:
                        return FetchOutcome(status="failed", error="response body too large")
                    chunks.append(chunk)

                return FetchOutcome(
                    status="ok",
                    body=b"".join(chunks),
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                )
    except TimeoutError:
        return FetchOutcome(
            status="failed", error=f"fetch exceeded {TOTAL_TIMEOUT_S}s deadline"
        )
    except Exception as exc:
        return FetchOutcome(status="failed", error=f"{type(exc).__name__}: {exc}")


def next_interval(
    base_interval: int, consecutive_failures: int, retry_after: int | None
) -> int:
    # Server-requested delay overrides backoff entirely: if the server says
    # "retry after 120s", we respect that even on the first failure. The
    # MIN_RETRY_AFTER_S floor only stops that override from being used
    # against us -- a Retry-After of 0/1/2 is clamped up instead of producing
    # a tight repoll loop.
    if retry_after is not None:
        return max(MIN_RETRY_AFTER_S, min(retry_after, MAX_BACKOFF_S))
    if consecutive_failures <= 0:
        return base_interval
    return min(base_interval * (2**consecutive_failures), MAX_BACKOFF_S)
