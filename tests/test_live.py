from pathlib import Path

import httpx

from rss_ticker.config import Channel
from rss_ticker.live import LIVE_TTL_S, STALE_TTL_S, LiveTV, parse_live_video_id

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# ---- parser --------------------------------------------------------------

def test_parser_accepts_a_live_page():
    assert parse_live_video_id(fixture("youtube_live.html")) == "livevid0001"


def test_parser_rejects_an_upcoming_page():
    assert parse_live_video_id(fixture("youtube_upcoming.html")) is None


def test_parser_rejects_an_offline_page():
    assert parse_live_video_id(fixture("youtube_offline.html")) is None


def test_parser_rejects_a_page_with_no_player_data():
    assert parse_live_video_id(fixture("youtube_unparseable.html")) is None


def test_parser_rejects_a_malformed_id():
    assert parse_live_video_id(fixture("youtube_malformed_id.html")) is None


def test_parser_rejects_empty_and_garbage():
    assert parse_live_video_id("") is None
    assert parse_live_video_id("ytInitialPlayerResponse = {not json") is None


# ---- cache ---------------------------------------------------------------

class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Backend:
    """A switchable MockTransport handler: counts calls and serves either a
    page body or raises, so a test can flip between live / offline / error."""

    def __init__(self) -> None:
        self.calls = 0
        self.mode = "live"  # "live" | "offline" | "error"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.mode == "error":
            raise httpx.ConnectError("refused")
        body = fixture("youtube_live.html" if self.mode == "live" else "youtube_offline.html")
        return httpx.Response(200, text=body)


def make_live(channels, backend, clock):
    client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
    return LiveTV(channels, client_getter=lambda: client, clock=clock), client


CH = (Channel("bloomberg-tv", "Bloomberg TV", "@markets"),)


async def test_membership():
    live, client = make_live(CH, Backend(), FakeClock())
    assert "bloomberg-tv" in live
    assert "nope" not in live
    await client.aclose()


async def test_fresh_hit_does_not_refetch():
    backend, clock = Backend(), FakeClock()
    live, client = make_live(CH, backend, clock)
    assert await live.video_id("bloomberg-tv") == "livevid0001"
    clock.t += 100  # inside the 5-minute window
    assert await live.video_id("bloomberg-tv") == "livevid0001"
    assert backend.calls == 1
    await client.aclose()


async def test_expiry_refetches_after_five_minutes():
    backend, clock = Backend(), FakeClock()
    live, client = make_live(CH, backend, clock)
    await live.video_id("bloomberg-tv")
    clock.t += LIVE_TTL_S + 1
    await live.video_id("bloomberg-tv")
    assert backend.calls == 2
    await client.aclose()


async def test_stale_on_error_within_thirty_minutes():
    backend, clock = Backend(), FakeClock()
    live, client = make_live(CH, backend, clock)
    assert await live.video_id("bloomberg-tv") == "livevid0001"  # good id obtained at t=0
    backend.mode = "error"
    clock.t += LIVE_TTL_S + 1  # force a refetch; it errors
    assert await live.video_id("bloomberg-tv") == "livevid0001"  # last good id still served
    await client.aclose()


async def test_empty_after_thirty_minutes_of_errors():
    backend, clock = Backend(), FakeClock()
    live, client = make_live(CH, backend, clock)
    await live.video_id("bloomberg-tv")  # good id at t=0
    backend.mode = "error"
    clock.t += STALE_TTL_S + 1  # past the stale window
    assert await live.video_id("bloomberg-tv") is None
    await client.aclose()


async def test_clean_not_live_clears_and_stays_empty_across_a_later_error():
    backend, clock = Backend(), FakeClock()
    live, client = make_live(CH, backend, clock)
    assert await live.video_id("bloomberg-tv") == "livevid0001"
    backend.mode = "offline"
    clock.t += LIVE_TTL_S + 1
    assert await live.video_id("bloomberg-tv") is None  # cleared immediately
    backend.mode = "error"
    clock.t += LIVE_TTL_S + 1
    assert await live.video_id("bloomberg-tv") is None  # not resurrected
    await client.aclose()


async def test_concurrent_requests_share_one_fetch():
    import asyncio
    backend, clock = Backend(), FakeClock()
    live, client = make_live(CH, backend, clock)
    results = await asyncio.gather(*(live.video_id("bloomberg-tv") for _ in range(5)))
    assert results == ["livevid0001"] * 5
    assert backend.calls == 1
    await client.aclose()


async def test_no_client_is_treated_as_an_error_not_a_crash():
    live = LiveTV(CH, client_getter=lambda: None, clock=FakeClock())
    assert await live.video_id("bloomberg-tv") is None  # no good id yet -> empty
