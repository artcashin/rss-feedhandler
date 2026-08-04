import asyncio

import httpx

from rss_ticker import fetch
from rss_ticker.fetch import (
    MAX_BACKOFF_S,
    MAX_BODY_BYTES,
    MIN_RETRY_AFTER_S,
    fetch_feed,
    next_interval,
    user_agent,
)


def transport(handler):
    return httpx.MockTransport(handler)


async def call(handler, etag=None, last_modified=None):
    async with httpx.AsyncClient(transport=transport(handler)) as client:
        return await fetch_feed(client, "https://x.example/rss", etag, last_modified)


async def test_200_returns_body_and_validators():
    def handler(request):
        return httpx.Response(
            200, content=b"<rss/>", headers={"ETag": '"v1"', "Last-Modified": "Mon"}
        )

    out = await call(handler)
    assert out.status == "ok"
    assert out.body == b"<rss/>"
    assert out.etag == '"v1"'
    assert out.last_modified == "Mon"


async def test_conditional_headers_are_sent_when_known():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(304)

    await call(handler, etag='"v1"', last_modified="Mon")
    assert seen["if-none-match"] == '"v1"'
    assert seen["if-modified-since"] == "Mon"


async def test_304_is_not_modified_not_failure():
    out = await call(lambda r: httpx.Response(304))
    assert out.status == "not_modified"
    assert out.error is None


async def test_500_is_failure():
    out = await call(lambda r: httpx.Response(500))
    assert out.status == "failed"
    assert "500" in out.error


async def test_429_captures_retry_after():
    out = await call(lambda r: httpx.Response(429, headers={"Retry-After": "120"}))
    assert out.status == "failed"
    assert out.retry_after == 120


async def test_non_numeric_retry_after_is_ignored():
    out = await call(lambda r: httpx.Response(503, headers={"Retry-After": "Wed, 21 Oct"}))
    assert out.retry_after is None


async def test_connection_error_is_failure_not_exception():
    def handler(request):
        raise httpx.ConnectError("refused")

    out = await call(handler)
    assert out.status == "failed"
    assert "refused" in out.error


async def test_oversized_body_is_rejected():
    def handler(request):
        return httpx.Response(200, content=b"x" * (6 * 1024 * 1024))

    out = await call(handler)
    assert out.status == "failed"
    assert "too large" in out.error


async def test_body_just_under_cap_is_ok():
    def handler(request):
        return httpx.Response(200, content=b"x" * (MAX_BODY_BYTES - 1))

    out = await call(handler)
    assert out.status == "ok"


class _StalledStream(httpx.AsyncByteStream):
    """A body stream that dribbles a small amount of data after a long delay.

    Simulates a server that sends headers and then stalls mid-body: no single
    httpx read/connect/write ever times out (MockTransport doesn't enforce
    httpx's per-operation `timeout=` at all -- only a real transport does),
    so the only thing that can stop this is a wall-clock deadline wrapped
    around the whole fetch.
    """

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s

    async def __aiter__(self):
        await asyncio.sleep(self.delay_s)
        yield b"<rss/>"


async def test_stalled_body_hits_total_deadline(monkeypatch):
    monkeypatch.setattr(fetch, "TOTAL_TIMEOUT_S", 0.1)

    def handler(request):
        return httpx.Response(200, stream=_StalledStream(1.0))

    out = await call(handler)
    assert out.status == "failed"
    assert "0.1s deadline" in out.error


async def test_malformed_url_with_no_scheme_is_failure_not_exception():
    # No scheme means httpx's own URL parsing lets it through (no network
    # transport is capable of touching such a URL, so this is not real I/O);
    # a bare ValueError surfaces later from stdlib cookie-jar handling of the
    # response. MockTransport reproduces that path without any socket use.
    def handler(request):
        return httpx.Response(200, content=b"<rss/>")

    async with httpx.AsyncClient(transport=transport(handler)) as client:
        out = await fetch_feed(client, "not a url", None, None)
    assert out.status == "failed"


async def test_invalid_url_raising_httpx_invalid_url_is_failure_not_exception():
    # httpx.InvalidURL is raised while parsing the URL itself, before any
    # transport is consulted, so the handler here is never invoked and no
    # network I/O is possible.
    def unreachable_handler(request):
        raise AssertionError("transport should never be reached for an invalid URL")

    async with httpx.AsyncClient(transport=transport(unreachable_handler)) as client:
        out = await fetch_feed(client, "http://[::1", None, None)
    assert out.status == "failed"


def test_next_interval_is_base_on_success():
    assert next_interval(300, consecutive_failures=0, retry_after=None) == 300


def test_next_interval_backs_off_exponentially():
    assert next_interval(300, consecutive_failures=1, retry_after=None) == 600
    assert next_interval(300, consecutive_failures=2, retry_after=None) == 1200


def test_next_interval_is_capped():
    assert next_interval(300, consecutive_failures=99, retry_after=None) == MAX_BACKOFF_S


def test_retry_after_overrides_backoff():
    assert next_interval(300, consecutive_failures=5, retry_after=45) == 45


def test_retry_after_is_clamped_to_max_backoff():
    assert next_interval(300, consecutive_failures=1, retry_after=999999) == MAX_BACKOFF_S


def test_retry_after_negative_floors_at_min_retry_after():
    assert next_interval(300, consecutive_failures=1, retry_after=-5) == MIN_RETRY_AFTER_S


def test_retry_after_zero_floors_at_min_retry_after():
    # A server sending Retry-After: 0 must not cause an immediate re-poll --
    # that becomes a ~1-second hammer loop against the server.
    assert next_interval(300, consecutive_failures=1, retry_after=0) >= MIN_RETRY_AFTER_S
    assert next_interval(300, consecutive_failures=1, retry_after=0) == MIN_RETRY_AFTER_S


def test_retry_after_comfortably_above_floor_is_respected():
    assert next_interval(300, consecutive_failures=1, retry_after=120) == 120


# test_retry_after_is_clamped_to_max_backoff (above) already covers the
# above-MAX_BACKOFF_S cap; the floor does not change that behavior.


def test_user_agent_includes_version_and_url():
    ua = user_agent("0.1.0", "http://nas.local:8088")
    assert "rss-ticker/0.1.0" in ua
    assert "nas.local" in ua


async def test_http_date_retry_after_is_honored():
    import email.utils
    import time
    target = time.time() + 120
    date_form = email.utils.formatdate(target, usegmt=True)
    transport = httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": date_form})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        out = await fetch_feed(client, "https://x.example/rss", None, None)
    assert out.status == "failed"
    assert out.retry_after is not None and 110 <= out.retry_after <= 125
