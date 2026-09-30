# Live TV Widget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional Live TV widget to rss-feedhandler that serves an OpenBB `youtube` widget manifest and resolves each configured YouTube channel's currently-live video id on demand.

**Architecture:** A new `live_tv` config section lists channels (`{key, label, handle}`), validated at startup like every other config key. A new `live.py` module holds a `LiveTV` cache: on request it fetches `https://www.youtube.com/@<handle>/live`, parses `ytInitialPlayerResponse` for a video id that is *live now*, caches it 5 minutes per channel, and serves the last good id for up to 30 minutes across fetch errors. Three open routes in `api.py` (`/widgets.json`, `/api/live/channels`, `/api/live/video`) expose it; `main.build()` wires the shared httpx client and real clock in. The lookup is lazy (fetched-on-request, cached) rather than a poller — see the decision in Task 3.

**Tech Stack:** Python 3.12, FastAPI, httpx (existing shared `AsyncClient`), pytest + pytest-asyncio (`asyncio_mode = "auto"`), uv. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-29-live-tv-card-design.md` in the BDOBB (bdobb-v2) repo (private; referenced by path). Only the rss-feedhandler rows apply — pieces 1 and 2 ("Live TV widget", "Live lookup"), the "Live lookup (feedhandler)" section, the feedhandler testing bullet, and release placement "rss-feedhandler 8.1.0". The gateway and bdobb-v2 pieces are context only.

## Global Constraints

- **Version:** bump to `8.1.0` in `pyproject.toml`, `src/rss_ticker/__init__.py`, and `Makefile` `TAG` (current value across all three: `8.0.1`). `tests/test_version.py` asserts `pyproject.toml` and `__version__` match — both must move together.
- **Public repo, no private strings:** no private hostnames, tailnet names, IPs, NAS paths, or auth headers anywhere. The spec's private gateway origin must **not** appear. `bash scripts/scrub-check.sh` must pass; do not add to `scripts/scrub-allowlist.txt` unless unavoidable.
- **No new dependencies:** stdlib (`json`, `re`, `asyncio`, `time`) + the already-present `httpx`, `fastapi`, `pyyaml`.
- **Comment style:** long explanatory comments are the house style — keep and write them; explain *why*, not *what*.
- **Server is open by design:** the whole server has no auth ("never funneled"). All three new routes are open, matching `/api/news` etc. Do not add auth.
- **Feature is off by default:** absent or empty `live_tv` ⇒ no widget in `widgets.json` (returns `{}`, a valid empty manifest, not 404) and `/api/live/*` return 404.
- **Injectable seams for tests:** the fetch uses an injectable httpx client (via a getter) and an injectable clock; tests never touch the network (use `httpx.MockTransport` and a fake clock).
- **Commit trailer:** every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Commits stay **local** — never push.
- **Video id validation:** an accepted id must match `^[A-Za-z0-9_-]{11}$`.
- **Rate ceiling:** at most one outbound request per channel per 5 minutes; concurrent requests for one channel share a single fetch.

## Review Focus

- **Missing `channel` query param** on `/api/live/video` → FastAPI returns 422 (not a 500). Pinned in Task 4.
- **Duplicate keys / bad slug / bad handle across channels** → startup `ConfigError`, not silent last-wins. Pinned in Task 2.
- **Concurrent requests for one channel** must produce a single outbound fetch (spec is explicit). Pinned in Task 3.
- **Clean "not live" then a fetch error** must stay empty — the ended stream's id must not be resurrected by the 30-minute stale-on-error window. Pinned in Task 3.
- **A YouTube page with no `ytInitialPlayerResponse`** (e.g. a consent interstitial served to the non-browser httpx User-Agent) → parser returns `None` → `/api/live/video` returns an empty 200, never a crash. Pinned in Task 3 (`unparseable` fixture). This is the main *live* risk (see Task 3 decision on User-Agent).

---

## Task 1: Version bump to 8.1.0

**Files:**
- Modify: `pyproject.toml:3`
- Modify: `src/rss_ticker/__init__.py:1`
- Modify: `Makefile:2`
- Test: `tests/test_version.py` (exists; no change)

**Interfaces:**
- Consumes: nothing.
- Produces: `rss_ticker.__version__ == "8.1.0"`.

- [ ] **Step 1: Run the version test to see it pass at 8.0.1 first**

Run: `uv run pytest tests/test_version.py -q`
Expected: PASS (both strings are `8.0.1`).

- [ ] **Step 2: Bump all three strings**

`pyproject.toml` line 3:
```toml
version = "8.1.0"
```

`src/rss_ticker/__init__.py`:
```python
__version__ = "8.1.0"
```

`Makefile` line 2 (keeps the built image tag in step with the code — the same drift `test_version.py` was written to catch, but for the image):
```makefile
TAG     ?= 8.1.0
```

- [ ] **Step 3: Run the version test to verify it still passes**

Run: `uv run pytest tests/test_version.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src/rss_ticker/__init__.py Makefile
git commit -m "chore: 8.1.0

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 2: Config — the `live_tv` channel list

**Files:**
- Modify: `src/rss_ticker/config.py` (add `Channel`, extend `KNOWN_KEYS` and `Config`, add `_channels` parser)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Channel` — `@dataclass(frozen=True)` with `key: str`, `label: str`, `handle: str`.
  - `Config.live_tv: tuple[Channel, ...]` (default `()`), populated by `load_config`.
  - `ConfigError` raised on any malformed `live_tv`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:
```python
from rss_ticker.config import Channel


def test_live_tv_is_empty_when_absent(tmp_path):
    cfg = load_config(write(tmp_path, "retention_days: 7\n"), {})
    assert cfg.live_tv == ()


def test_live_tv_parses_channels_in_order(tmp_path):
    p = write(tmp_path, """
live_tv:
  - key: bloomberg-tv
    label: Bloomberg TV
    handle: "@markets"
  - key: yahoo-finance
    label: Yahoo Finance
    handle: "@YahooFinance"
""")
    cfg = load_config(p, {})
    assert cfg.live_tv == (
        Channel("bloomberg-tv", "Bloomberg TV", "@markets"),
        Channel("yahoo-finance", "Yahoo Finance", "@YahooFinance"),
    )


def test_live_tv_empty_list_is_off(tmp_path):
    cfg = load_config(write(tmp_path, "live_tv: []\n"), {})
    assert cfg.live_tv == ()


def test_live_tv_rejects_duplicate_keys(tmp_path):
    p = write(tmp_path, """
live_tv:
  - {key: dup, label: A, handle: "@a"}
  - {key: dup, label: B, handle: "@b"}
""")
    with pytest.raises(ConfigError, match="dup"):
        load_config(p, {})


def test_live_tv_rejects_a_bad_key_slug(tmp_path):
    p = write(tmp_path, 'live_tv:\n  - {key: "Bad Key", label: A, handle: "@a"}\n')
    with pytest.raises(ConfigError, match="key"):
        load_config(p, {})


def test_live_tv_rejects_a_bad_handle(tmp_path):
    p = write(tmp_path, 'live_tv:\n  - {key: ok, label: A, handle: "markets"}\n')
    with pytest.raises(ConfigError, match="handle"):
        load_config(p, {})


def test_live_tv_rejects_a_missing_field(tmp_path):
    p = write(tmp_path, 'live_tv:\n  - {key: ok, handle: "@a"}\n')
    with pytest.raises(ConfigError, match="label"):
        load_config(p, {})


def test_live_tv_rejects_an_unknown_field(tmp_path):
    p = write(tmp_path, 'live_tv:\n  - {key: ok, label: A, handle: "@a", extra: 1}\n')
    with pytest.raises(ConfigError, match="extra"):
        load_config(p, {})


def test_live_tv_must_be_a_list(tmp_path):
    with pytest.raises(ConfigError, match="live_tv"):
        load_config(write(tmp_path, "live_tv: not-a-list\n"), {})
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'Channel'` / unknown-key error for `live_tv`.

- [ ] **Step 3: Implement `Channel`, the validator, and wire it into `Config`/`load_config`**

In `src/rss_ticker/config.py`:

Add to the imports at the top (there is already `import re`):
```python
from typing import Any, Mapping
```
(`Mapping` is already imported; add `Any` alongside it.)

Add the slug/handle regexes and the `Channel` dataclass near the top, under `_ENV_RE`:
```python
# A channel key is a URL-safe slug (it becomes a query-param value and an
# OpenBB option value). A handle is a YouTube @handle: the leading @ then the
# characters YouTube allows in a handle. Both are validated at load so a
# malformed config fails at boot -- the same loud-failure contract the four
# operational keys already have -- rather than surfacing as a 404 or a
# mis-built /@<handle>/live URL at request time.
_KEY_RE = re.compile(r"[a-z0-9-]+")
_HANDLE_RE = re.compile(r"@[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Channel:
    key: str
    label: str
    handle: str
```

Extend `KNOWN_KEYS` (line 16-18) to include `live_tv`:
```python
KNOWN_KEYS = frozenset(
    {"retention_days", "default_poll_interval_s", "max_concurrent_polls", "bind_host", "live_tv"}
)
```

Add `live_tv` to the `Config` dataclass (frozen; a tuple keeps it immutable/hashable):
```python
@dataclass(frozen=True)
class Config:
    retention_days: int = 7
    default_poll_interval_s: int = 300
    max_concurrent_polls: int = 8
    bind_host: str = "0.0.0.0"
    live_tv: tuple[Channel, ...] = ()
```

Add the channel parser (place it after `_positive_int`):
```python
_CHANNEL_FIELDS = ("key", "label", "handle")


def _channels(raw: dict) -> tuple[Channel, ...]:
    """Parse and validate the optional live_tv list.

    Absent or empty means the feature is off, so this returns (). Every other
    shape is checked and any problem raises ConfigError, naming the offending
    value -- validating here (not at request time) means a bad handle can never
    become a fetch against a mis-built URL, and a duplicate key can never
    silently shadow an earlier channel.
    """
    value = raw.get("live_tv")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"live_tv must be a list of channels, got {type(value).__name__}")
    channels: list[Channel] = []
    seen: set[str] = set()
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError(f"live_tv[{i}] must be a mapping with key, label, handle")
        unknown = sorted(str(k) for k in item if k not in _CHANNEL_FIELDS)
        if unknown:
            raise ConfigError(f"live_tv[{i}] has unknown fields: {', '.join(unknown)}")
        for field in _CHANNEL_FIELDS:
            if not isinstance(item.get(field), str) or not item[field]:
                raise ConfigError(f"live_tv[{i}] {field} must be a non-empty string")
        key, label, handle = item["key"], item["label"], item["handle"]
        if not _KEY_RE.fullmatch(key):
            raise ConfigError(f"live_tv[{i}] key {key!r} must match [a-z0-9-]+")
        if not _HANDLE_RE.fullmatch(handle):
            raise ConfigError(f"live_tv[{i}] handle {handle!r} must look like @name")
        if key in seen:
            raise ConfigError(f"live_tv has a duplicate key: {key}")
        seen.add(key)
        channels.append(Channel(key=key, label=label, handle=handle))
    return channels
```

In `load_config`, build channels from the raw mapping **before** env-walking is applied to it (the channel fields are literals, not `${ENV}`), and pass them into `Config`. The unknown-top-level-key check at line 94 already runs before this, so `live_tv` being in `KNOWN_KEYS` is what lets it through. Add to the `return Config(...)` call:
```python
    return Config(
        retention_days=_positive_int(raw, "retention_days", 7),
        default_poll_interval_s=_positive_int(raw, "default_poll_interval_s", 300),
        max_concurrent_polls=_positive_int(raw, "max_concurrent_polls", 8),
        bind_host=bind_host,
        live_tv=_channels(raw),
    )
```
Note: `raw` here is the env-expanded mapping (after `raw = _walk(raw, env)` at line 103). `_walk` leaves non-`${...}` strings untouched, so `@markets` and the labels pass through verbatim; parsing the expanded `raw` is correct and needs no extra handling.

- [ ] **Step 4: Run the config tests to verify they pass**

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS (all, including the pre-existing ones).

- [ ] **Step 5: Commit**

```bash
git add src/rss_ticker/config.py tests/test_config.py
git commit -m "feat(config): optional live_tv channel list

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 3: Live lookup — `live.py` (parser + cache)

**Files:**
- Create: `src/rss_ticker/live.py`
- Create: `tests/fixtures/youtube_live.html`
- Create: `tests/fixtures/youtube_upcoming.html`
- Create: `tests/fixtures/youtube_offline.html`
- Create: `tests/fixtures/youtube_unparseable.html`
- Create: `tests/fixtures/youtube_malformed_id.html`
- Test: `tests/test_live.py`

**Interfaces:**
- Consumes: `Channel` from `rss_ticker.config`; `TIMEOUT_S` from `rss_ticker.fetch`.
- Produces:
  - `parse_live_video_id(html: str) -> str | None` — the 11-char video id iff the page is live now, else `None`.
  - `LiveTV(channels: tuple[Channel, ...], client_getter: Callable[[], httpx.AsyncClient | None], clock: Callable[[], float] = time.monotonic)`.
    - `LiveTV.channels: tuple[Channel, ...]` (config order).
    - `key in live` (`__contains__`) — membership by channel key.
    - `async LiveTV.video_id(key: str) -> str | None` — cached live video id for a known key (caller must check membership first; unknown key raises `KeyError`).
  - Module constants `LIVE_TTL_S = 300`, `STALE_TTL_S = 1800`.

**Decision — lazy lookup, not a poller.** The spec says "polled source" but the existing `Poller` is `Store`-backed and feed-specific; reusing it would mean inventing a parallel store for channels. A lazy fetch-on-request cache meets every hard requirement more simply: ≤1 request per channel per 5 minutes (the freshness check), shared fetch under a per-channel `asyncio.Lock`, and *zero* work when the feature is off or no one opens the card. That is the lower rung of the ladder, so it wins.

**Decision — clean "not live" clears the last-good id.** The 30-minute stale-on-error window exists to ride out network blips. A page that parses cleanly as offline is a definitive "the stream ended" signal, so on that answer we drop `good_id`; otherwise a fetch error 4 minutes later would resurrect a dead stream. The spec's "replaces the cache with empty immediately" is honored, and this closes the resurrection hole.

**Decision — reuse the shared client and its `rss-ticker/...` User-Agent.** No new client, no browser-UA spoofing. If live verification later shows YouTube serving a consent interstitial to that UA (the page would have no `ytInitialPlayerResponse`, so the parser returns `None` and the card shows off-air), the fix is a browser `User-Agent` + `Accept-Language` header on this one request — a follow-up, not built now. Flagged in Review Focus. `# ponytail:` mark this in the code.

- [ ] **Step 1: Write the fixtures**

`tests/fixtures/youtube_live.html` — live now (`isLive: true`, status OK):
```html
<!doctype html><html><head><title>Live</title></head><body>
<script>var ytInitialPlayerResponse = {"playabilityStatus":{"status":"OK"},"videoDetails":{"videoId":"livevid0001","isLive":true,"isLiveContent":true}};</script>
</body></html>
```

`tests/fixtures/youtube_upcoming.html` — scheduled, not started (no `isLive`):
```html
<!doctype html><html><head><title>Upcoming</title></head><body>
<script>var ytInitialPlayerResponse = {"playabilityStatus":{"status":"LIVE_STREAM_OFFLINE"},"videoDetails":{"videoId":"upcomevid01","isUpcoming":true,"isLiveContent":true}};</script>
</body></html>
```

`tests/fixtures/youtube_offline.html` — ended / off-air:
```html
<!doctype html><html><head><title>Offline</title></head><body>
<script>var ytInitialPlayerResponse = {"playabilityStatus":{"status":"LIVE_STREAM_OFFLINE"},"videoDetails":{"videoId":"endedvid001","isLiveContent":true}};</script>
</body></html>
```

`tests/fixtures/youtube_unparseable.html` — no player data at all (a consent/interstitial stand-in):
```html
<!doctype html><html><head><title>Before you continue</title></head><body>
<form>No ytInitialPlayerResponse anywhere on this page.</form>
</body></html>
```

`tests/fixtures/youtube_malformed_id.html` — live markers but a bad id (not 11 chars):
```html
<!doctype html><html><head><title>Live</title></head><body>
<script>var ytInitialPlayerResponse = {"playabilityStatus":{"status":"OK"},"videoDetails":{"videoId":"tooshort","isLive":true}};</script>
</body></html>
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_live.py`:
```python
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
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/test_live.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'rss_ticker.live'`.

- [ ] **Step 4: Implement `live.py`**

Create `src/rss_ticker/live.py`:
```python
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from .config import Channel
from .fetch import TIMEOUT_S

log = logging.getLogger(__name__)

# YouTube embeds the player state as a JSON object assigned to
# `ytInitialPlayerResponse` in an inline <script>. We read the current video id
# from videoDetails.videoId and only trust it when the page says the stream is
# live *now* -- a channel's /@handle/live URL also serves upcoming and ended
# streams (CNBC's did on 2026-09-29), and framing one of those would show a
# countdown or a dead player instead of live TV.
_MARKER = "ytInitialPlayerResponse"
# An id is exactly 11 URL-safe base64 characters. Anything else is a parser
# artifact, not a real id, so it is rejected rather than framed.
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")

# Cache windows. A fresh entry is served without refetching for 5 minutes
# (the rate ceiling: <=1 outbound request per channel per 5 minutes). On a
# fetch/parse ERROR the last confirmed-live id is served for up to 30 minutes
# after it was obtained, so a brief YouTube blip doesn't blank the card.
LIVE_TTL_S = 300
STALE_TTL_S = 1800


def parse_live_video_id(html: str) -> str | None:
    """The current video id iff `html` is a YouTube page that is live now.

    Returns None for an upcoming stream, an ended/offline stream, a page with
    no player data (e.g. a consent interstitial), or any parse failure. Never
    raises: hostile or truncated input is a None, not an exception.
    """
    idx = html.find(_MARKER)
    if idx == -1:
        return None
    brace = html.find("{", idx)
    if brace == -1:
        return None
    try:
        # raw_decode parses one JSON object starting at `brace` and ignores the
        # trailing `;</script>...`, so we never have to guess where the object
        # ends by counting braces ourselves.
        data, _ = json.JSONDecoder().raw_decode(html, brace)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    details = data.get("videoDetails")
    if not isinstance(details, dict):
        return None
    vid = details.get("videoId")
    if not isinstance(vid, str) or not _VIDEO_ID_RE.fullmatch(vid):
        return None
    # Live now is the tight signal: a currently-live watch page carries
    # videoDetails.isLive == true. Upcoming and ended pages don't, and an ended
    # stream additionally reports playabilityStatus.status LIVE_STREAM_OFFLINE.
    # ponytail: heuristic on YouTube's private JSON shape; if YouTube renames a
    # marker the card shows off-air (safe), and the fix is to adjust these keys.
    if details.get("isLive") is not True:
        return None
    if details.get("isUpcoming") is True:
        return None
    status = (data.get("playabilityStatus") or {}).get("status")
    if status == "LIVE_STREAM_OFFLINE":
        return None
    return vid


@dataclass
class _Entry:
    # `cached` is the answer currently served; `cached_at` anchors the 5-minute
    # freshness window. `good_id`/`good_at` remember the last confirmed-live id
    # and when it was obtained, which is what the 30-minute stale-on-error
    # window is measured against (from obtain time, not from the error).
    cached: str | None = None
    cached_at: float = float("-inf")
    good_id: str | None = None
    good_at: float = float("-inf")


class LiveTV:
    """Per-channel live-video-id cache backed by on-demand fetches.

    Lazy by design (see the plan): a channel is fetched only when asked and at
    most once per 5 minutes. A per-channel lock makes concurrent requests share
    a single fetch -- the waiters re-check freshness after acquiring the lock
    and return the just-cached answer without a second outbound request.
    """

    def __init__(
        self,
        channels: tuple[Channel, ...],
        client_getter: Callable[[], httpx.AsyncClient | None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.channels = tuple(channels)
        self._by_key = {c.key: c for c in self.channels}
        self._client_getter = client_getter
        self._clock = clock
        self._entries = {c.key: _Entry() for c in self.channels}
        # Locks are created once at construction (all keys are known upfront),
        # so there is no race to create a lock for a key.
        self._locks = {c.key: asyncio.Lock() for c in self.channels}

    def __contains__(self, key: str) -> bool:
        return key in self._by_key

    async def video_id(self, key: str) -> str | None:
        entry = self._entries[key]
        async with self._locks[key]:
            now = self._clock()
            if now - entry.cached_at < LIVE_TTL_S:
                # Fresh -- also the path a waiter that lost the race takes, so
                # the shared fetch is genuinely shared (one outbound request).
                return entry.cached
            try:
                vid = await self._fetch(self._by_key[key].handle)
            except Exception:
                # Network/HTTP error: keep serving the last good id while it is
                # inside the 30-minute window, otherwise empty. good_id/good_at
                # are left untouched so the window keeps measuring from obtain.
                log.warning("Live lookup for channel %s failed", key)
                if entry.good_id is not None and now - entry.good_at < STALE_TTL_S:
                    entry.cached = entry.good_id
                else:
                    entry.cached = None
                entry.cached_at = now
                return entry.cached
            if vid is not None:
                entry.good_id = vid
                entry.good_at = now
                entry.cached = vid
            else:
                # A clean "not live" page: the stream ended. Serve empty now and
                # forget the good id, so a later error can't resurrect it.
                entry.good_id = None
                entry.cached = None
            entry.cached_at = now
            return entry.cached

    async def _fetch(self, handle: str) -> str | None:
        client = self._client_getter()
        if client is None:
            # No client yet (called outside the app lifespan). Treated as an
            # error by the caller, which is the safe direction.
            raise RuntimeError("no http client available")
        # ponytail: reuses the shared client's rss-ticker User-Agent. If YouTube
        # starts serving a consent interstitial to it, add a browser UA +
        # Accept-Language here (see plan Task 3 decision).
        resp = await client.get(
            f"https://www.youtube.com/@{handle.lstrip('@')}/live",
            timeout=TIMEOUT_S,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return parse_live_video_id(resp.text)
```

- [ ] **Step 5: Run the live tests to verify they pass**

Run: `uv run pytest tests/test_live.py -q`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add src/rss_ticker/live.py tests/test_live.py tests/fixtures/youtube_*.html
git commit -m "feat(live): live-now video-id parser and per-channel cache

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 4: Endpoints and wiring

**Files:**
- Modify: `src/rss_ticker/api.py` (add `live` param to `create_app`, three routes, manifest builder)
- Modify: `src/rss_ticker/main.py` (construct `LiveTV`, pass into `create_app`)
- Test: `tests/test_api_rest.py`

**Interfaces:**
- Consumes: `LiveTV` from `rss_ticker.live`; `Channel` from `rss_ticker.config`.
- Produces:
  - `create_app(..., live: LiveTV | None = None)` — new trailing keyword param, default `None` (feature off).
  - `live_widgets_manifest(channels: tuple[Channel, ...]) -> dict` (module-level in `api.py`).
  - Routes: `GET /widgets.json`, `GET /api/live/channels`, `GET /api/live/video?channel=<key>`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_api_rest.py`, add imports at the top:
```python
import httpx

from rss_ticker.config import Channel
from rss_ticker.live import LiveTV
```

Add a fixtures-path helper and a builder near the top (after the existing `client` fixture):
```python
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

CHANNELS = (
    Channel("bloomberg-tv", "Bloomberg TV", "@markets"),
    Channel("yahoo-finance", "Yahoo Finance", "@YahooFinance"),
)


def live_client(store, broadcaster, mode="live"):
    def handler(request):
        if mode == "error":
            raise httpx.ConnectError("refused")
        name = "youtube_live.html" if mode == "live" else "youtube_offline.html"
        return httpx.Response(200, text=(FIXTURES / name).read_text())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    live = LiveTV(CHANNELS, client_getter=lambda: http, clock=lambda: 0.0)
    app = create_app(Config(live_tv=CHANNELS), store, broadcaster, live=live)
    return TestClient(app)
```

Fix the existing `test_retired_routes_are_gone` — `/widgets.json` is no longer a 404, it is a valid empty manifest when the feature is off. Replace that test with:
```python
def test_widget_singular_route_is_gone_and_feeds_is_read_only(client):
    # The old iframe widget page stays gone.
    assert client.get("/widget").status_code == 404
    # /api/feeds still exists, read-only: a write to it is method-not-allowed.
    assert client.post("/api/feeds", json={}).status_code == 405
    # The per-feed delete route is gone outright, so the path itself is unknown.
    assert client.delete("/api/feeds/1").status_code == 404


def test_feature_off_widgets_is_empty_and_live_routes_404(client):
    # Default Config() has no live_tv, so create_app got live=None.
    r = client.get("/widgets.json")
    assert r.status_code == 200
    assert r.json() == {}
    assert client.get("/api/live/channels").status_code == 404
    assert client.get("/api/live/video", params={"channel": "bloomberg-tv"}).status_code == 404
```

Add the configured-feature tests:
```python
def test_widgets_manifest_shape(store, broadcaster):
    c = live_client(store, broadcaster)
    manifest = c.get("/widgets.json").json()
    assert set(manifest) == {"live_tv"}
    w = manifest["live_tv"]
    assert w["name"] == "Live TV"
    assert w["type"] == "youtube"
    assert w["endpoint"] == "/api/live/video"
    assert w["gridData"] == {"w": 20, "h": 12}
    assert len(w["params"]) == 1
    p = w["params"][0]
    assert p["paramName"] == "channel"
    assert p["type"] == "endpoint"
    assert p["label"] == "Channel"
    assert p["optionsEndpoint"] == "/api/live/channels"
    assert p["value"] == "bloomberg-tv"  # the first channel is the default


def test_channels_are_options_in_config_order(store, broadcaster):
    c = live_client(store, broadcaster)
    assert c.get("/api/live/channels").json() == [
        {"label": "Bloomberg TV", "value": "bloomberg-tv"},
        {"label": "Yahoo Finance", "value": "yahoo-finance"},
    ]


def test_video_unknown_channel_is_404(store, broadcaster):
    c = live_client(store, broadcaster)
    assert c.get("/api/live/video", params={"channel": "nope"}).status_code == 404


def test_video_missing_channel_param_is_422(store, broadcaster):
    c = live_client(store, broadcaster)
    assert c.get("/api/live/video").status_code == 422


def test_video_live_returns_watch_url_as_text(store, broadcaster):
    c = live_client(store, broadcaster, mode="live")
    r = c.get("/api/live/video", params={"channel": "bloomberg-tv"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == "https://www.youtube.com/watch?v=livevid0001"


def test_video_off_air_is_empty_200(store, broadcaster):
    c = live_client(store, broadcaster, mode="offline")
    r = c.get("/api/live/video", params={"channel": "bloomberg-tv"})
    assert r.status_code == 200
    assert r.text == ""
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_api_rest.py -q`
Expected: FAIL — `create_app() got an unexpected keyword argument 'live'` and missing routes.

- [ ] **Step 3: Implement the manifest builder and routes in `api.py`**

Add the import near the top of `src/rss_ticker/api.py` (alongside the other `from .` imports):
```python
from .live import LiveTV
```

Add the manifest builder at module level (after `feed_record`):
```python
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
```

Change the `create_app` signature (line 82-89) to add the `live` parameter:
```python
def create_app(
    config: Config,
    store: Store,
    broadcaster: Broadcaster,
    lifespan=None,
    health_strict: bool = False,
    on_feed_added: OnFeedAdded | None = None,
    live: LiveTV | None = None,
) -> FastAPI:
```

Store it on `app.state` next to the others (after line 101):
```python
    app.state.live = live
```

Add the three routes (place them after the `/api/health` route, before `resolve_subscription`):
```python
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
```

- [ ] **Step 4: Wire `LiveTV` into `main.build()`**

In `src/rss_ticker/main.py`, add the import:
```python
from .live import LiveTV
```

In `build()`, after `broadcaster = Broadcaster(store)` and the `holder` is declared (the `holder` dict already exists for the favicon client), construct the `LiveTV` using the same holder-based client getter and pass it to `create_app`. The `holder` is defined at line 46; add after the `background` set (line 50) is declared:
```python
    # Live TV shares the lifespan httpx client (reached through the same holder
    # the favicon path uses) and the wall clock. None when no channels are
    # configured, which makes /widgets.json {} and /api/live/* 404.
    live = LiveTV(config.live_tv, client_getter=lambda: holder.get("client")) if config.live_tv else None
```

Then pass it in the `create_app(...)` call (line 97-104):
```python
    app = create_app(
        config,
        store,
        broadcaster,
        lifespan=lifespan,
        health_strict=health_strict,
        on_feed_added=on_feed_added,
        live=live,
    )
```
Note: `LiveTV`'s default `clock` is `time.monotonic`, which is correct for production (relative TTL math, immune to wall-clock jumps).

- [ ] **Step 5: Run the API tests to verify they pass**

Run: `uv run pytest tests/test_api_rest.py -q`
Expected: PASS (all, including the rewritten retired-routes test).

- [ ] **Step 6: Run the whole suite to confirm nothing else regressed**

Run: `uv run pytest -q`
Expected: PASS. (Watch `tests/test_main.py` — it exercises `build()`; the new `live` wiring must not break it.)

- [ ] **Step 7: Commit**

```bash
git add src/rss_ticker/api.py src/rss_ticker/main.py tests/test_api_rest.py
git commit -m "feat(api): Live TV widget manifest and /api/live routes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 5: Docs, example config, and full verification

**Files:**
- Modify: `config.example.yaml`
- Modify: `README.md`
- Verify: `scripts/scrub-check.sh`, `make lint`, `uv run pytest -q`

**Interfaces:**
- Consumes: the config keys defined in Task 2. Produces no code.

- [ ] **Step 1: Add the `live_tv` block to `config.example.yaml`**

Append to `config.example.yaml`:
```yaml

# Optional: a Live TV widget for OpenBB Workspace / bdobb. Each channel is a
# YouTube @handle whose /live page is checked on demand (cached 5 minutes) for
# a currently-live stream. Omit this section, or leave it empty, and the
# feature is off: no widget appears in widgets.json and /api/live/* return 404.
# The first channel is the default shown in the widget's Channel picker.
live_tv:
  - key: bloomberg-tv
    label: Bloomberg TV
    handle: "@markets"          # Bloomberg Television's markets stream (default)
  - key: yahoo-finance
    label: Yahoo Finance
    handle: "@YahooFinance"
  - key: cnbc-television
    label: CNBC Television
    handle: "@CNBCtelevision"   # events only -- off-air (empty) between shows
```

- [ ] **Step 2: Add a Live TV section to `README.md`**

Add a `## Live TV` section (after the `## Configuration` section). Do **not** name any private gateway/tailnet host:
```markdown
## Live TV

An optional Live TV widget for OpenBB Workspace. Configure a short list of
YouTube channels under `live_tv` in `config.yaml` (each `{key, label, handle}`,
`handle` like `@markets`); the first is the default. With the section absent or
empty the feature is off.

    GET  /widgets.json                one "Live TV" youtube widget (or {} when off)
    GET  /api/live/channels           [{label, value}] for the Channel picker
    GET  /api/live/video?channel=KEY  text/plain watch URL when live, empty when off-air

On each `/api/live/video` request the server fetches the channel's
`https://www.youtube.com/@<handle>/live`, reads the current video id from the
page's player data, and returns `https://www.youtube.com/watch?v=<id>` only
when the stream is live now — an upcoming or ended stream reads as off-air (an
empty body). The id is cached 5 minutes per channel; a fetch error keeps
serving the last good id for up to 30 minutes. Unknown channel keys are 404, so
the endpoint can never be driven to fetch an arbitrary URL.

BDOBB's YouTube card consumes this: it takes the watch URL and frames the video
through a small public wrapper page, because YouTube refuses an embed whose
request carries no usable `Referer`. That wrapper lives outside this repo.
```

- [ ] **Step 3: Run the scrub check**

Run: `bash scripts/scrub-check.sh`
Expected: `Scrub check passed.` (No private host, IP, tailnet name, NAS path, or auth header was introduced. `www.youtube.com` and `@markets`-style handles are public and match no scrub pattern.)

- [ ] **Step 4: Run lint and the full test suite**

Run: `uv run ruff check src tests && uv run pytest -q`
Expected: lint clean; all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add config.example.yaml README.md
git commit -m "docs: Live TV config example and README section

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage (feedhandler rows only):**
- Piece 1 "Live TV widget" — `widgets.json` with one `youtube` widget, `channel` param from `/api/live/channels`, data from `/api/live/video?channel=<key>` → Task 4 (manifest builder + routes). ✅
- Piece 2 "Live lookup" — `/@<handle>/live` fetched, current live id cached → Task 3. ✅
- "Channels are a fixed list in config.yaml, each `{key, label, handle}`; example ships Bloomberg (@markets default), Yahoo, CNBC (events only)" → Task 2 (parse/validate) + Task 5 (example). ✅
- "read the id from player data; accept only when live now" → Task 3 `parse_live_video_id`. ✅
- "Cache 5 min; on error keep last good id up to 30 min then empty" → Task 3 `LiveTV.video_id`. ✅
- "Only configured keys accepted (unknown → 404)" → Task 4 `live_video`. ✅
- "Repo is public: no private hostnames; scrub check enforces it" → Global Constraints + Task 5 Step 3. ✅
- Release "rss-feedhandler 8.1.0" → Task 1. ✅
- Testing bullet (parser on saved pages live/upcoming/ended/unparseable; cache with injected clock 5/30 min; widgets.json shape; channels from config; video unknown 404 / off-air empty / live watch URL) → Tasks 2–4. ✅ Added `malformed id` fixture beyond the spec's list (the `^[A-Za-z0-9_-]{11}$` rule needs a negative case).

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to Task N". Every code step carries real code. ✅

**3. Type consistency:** `Channel(key,label,handle)` identical in config.py, live.py, tests. `LiveTV(channels, client_getter, clock)` and `video_id(key)->str|None` used identically in main.py and tests. `create_app(..., live=None)` matches all call sites (existing tests pass no `live`; Task 4 tests pass `live=`). `live_widgets_manifest(channels)` returns `{}` for empty — matches `test_feature_off_widgets_is_empty`. `LIVE_TTL_S`/`STALE_TTL_S` imported by name in tests. ✅

**4. Review Focus coverage:** missing `channel` param → `test_video_missing_channel_param_is_422` (Task 4); duplicate keys → `test_live_tv_rejects_duplicate_keys` (Task 2); concurrent share-one-fetch → `test_concurrent_requests_share_one_fetch` (Task 3); clean-not-live-then-error stays empty → `test_clean_not_live_clears_and_stays_empty_across_a_later_error` (Task 3); no-player-data page → `test_parser_rejects_a_page_with_no_player_data` (Task 3). ✅
