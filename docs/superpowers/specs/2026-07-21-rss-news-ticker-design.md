# RSS news ticker server — design

**Date:** 2026-07-21
**Status:** approved (design), pending implementation plan
**Partially superseded (2026-09-01):** the user model, filter rules, widget
module, OpenBB integration, and the entire "Amendment: untrusted-network
authentication" below are superseded by
`2026-09-01-user-agnostic-rework-design.md` — no users, no keys,
subscriber-counted feeds. The poller, store, broadcast, and packaging
sections stand except as amended there.

**Implemented 2026-09-03** by `docs/superpowers/plans/2026-09-03-user-agnostic-rework.md` (version 9.0.0).

## Purpose

A self-hosted server that polls a set of RSS feeds, pushes newly discovered articles to
connected clients in real time, and caches article history so clients can scroll back
through headlines. The primary consumer is an OpenBB Workspace widget that renders a
Bloomberg-style news window.

The server runs in a Docker container so it can be placed on hardware other than the
user's workstation.

## Decisions

These were settled during brainstorming. Each records the alternative rejected, so a
later reader can tell a deliberate choice from an accident.

| # | Decision | Rejected alternative |
|---|---|---|
| 1 | WebSocket push **and** REST paging over one shared cache | WS-only (awkward paging); REST-only (wasteful polling) |
| 2 | Multi-user, config-only: each user has their own feed list | Single-tenant; per-user read/unread state |
| 3 | Config file bootstraps, admin REST API mutates, SQLite persists | Static file only; API-only |
| 4 | SQLite-backed cache with a time-bounded retention window | In-memory ring buffer; unbounded archive |
| 5 | Widget is a frontend hosted by this server, embedded via OpenBB `type: "iframe"` | Server renders OpenBB-native widget types; separate widget backend |
| 6 | Dedup on GUID/link identity only | Cross-feed title dedup; content clustering |
| 7 | Multi-arch image (`linux/amd64` + `linux/arm64`) | Single-arch |
| 8 | Article carries title, source, timestamp, link, summary; per-user keyword filters | Minimal record; no filters |

Stack: Python 3.12, FastAPI + uvicorn, `httpx`, `feedparser`, stdlib `sqlite3`. No ORM.

## Architecture

One container, one process, one SQLite file. Six modules:

| Module | Responsibility | Depends on |
|---|---|---|
| `config` | Parse YAML, reconcile into SQLite on boot | — |
| `store` | All SQL: dedup, paging, retention, filters | sqlite3 |
| `poller` | Scheduled fetch, parse, normalize, insert | store, httpx, feedparser |
| `broadcast` | In-process pub/sub; per-user filtering; fan-out | store |
| `api` | FastAPI REST + WebSocket | store, broadcast |
| `widget` | Static ticker UI; renders `widgets.json` at startup | — |

The load-bearing seam: **`poller` and `api` never reference each other.** They meet only
at `store` and `broadcast`. Consequences that matter for testing — the poller can be
exercised against canned fixtures with no server running, and the API against a seeded
database with no network.

### Data flow

- **Ingest:** feed → poller (conditional GET) → feedparser → normalize → `store.insert_if_new` → newly-inserted rows only → broadcast
- **Live:** broadcast → per-user filter → JSON frame → that user's WebSocket clients
- **Scrollback:** widget → `GET /api/news?before=<cursor>` → store → paged JSON

### Endpoints

```
GET  /                                             root info (public)
GET  /widgets.json                                 manifest, rendered from public_base_url
                                                   — requires X-API-KEY: <manifest_key>
GET  /widget?user=&token=                          ticker UI (iframe target)
GET  /api/news?user=&token=&limit=&before=&after=  paged, cursor-based
WS   /ws/news?user=&token=                         live push
GET  /api/feeds?user=&token=                       list (URLs redacted unless admin)
POST /api/feeds                                    subscribe   (requires admin key)
DEL  /api/feeds/{id}?user=                         unsubscribe (requires admin key)
GET  /api/health                                   liveness; feed detail requires admin key
```

`before` and `after` are mutually exclusive; passing both is `400`. `before` (and the
no-cursor default) walks backward, newest-first; `after` walks forward, oldest-first, and
exists for the widget's reconnect gap fill.

The `token` query param is one of two ways to present a user's token — see
"Amendment: untrusted-network authentication (2026-07-21)" for the full rules.

`user` is **required** on `/api/news`, `/ws/news`, `/api/feeds`, and `/widget`. A
structurally missing `user` is `422`, never an implicit default — an unrecognized user
silently falling back to someone else's feed list would be worse than an error.

~~An unknown user is `400`.~~ **Superseded** by the amendment below: on every
token-protected endpoint an unknown user and a wrong token both return `401` with an
identical body, so user ids cannot be enumerated. The `400` survives only on the
admin-gated write endpoints, whose caller already knows every user.

`limit` defaults to 50, capped at 200.

Because feeds are global and users subscribe to them (see "Data model"), the two write
endpoints operate on **subscriptions**, not on feeds directly:

- `POST /api/feeds` with `{user, url, name?, poll_interval_s?}` upserts the feed row by
  URL and creates the subscription. If the feed already exists because another user
  subscribes to it, no second feed row and no second poll schedule is created.
- `DELETE /api/feeds/{id}?user=` removes **only that user's subscription**. The feed row
  and its cached articles survive as long as any other user subscribes. A feed whose
  last subscription is removed is marked `enabled = 0` and stops being polled; its
  articles age out through normal retention rather than being deleted immediately, so an
  accidental unsubscribe followed by a re-subscribe does not lose history.

**Auth:** ~~admin key (`X-Admin-Key`) required on writes only. Reads are unauthenticated —
the container sits on a trusted network, and an iframe-embedded widget cannot hold a
secret.~~ **Superseded** by "Amendment: untrusted-network authentication (2026-07-21)":
reads now require a per-user token. The admin key still gates writes. See "OpenBB
integration" for why iframe auth is not available from OpenBB itself.

**Paging is cursor-based, not offset-based.** Offsets shift under concurrent inserts,
which for a live feed produces duplicated and skipped rows mid-scroll.

**CORS:** explicit origin list — `https://pro.openbb.co`, `https://pro.openbb.dev`,
`https://excel.openbb.co`, `http://localhost:1420`. Wildcard origins are rejected by
browsers when `allow_credentials=True`.

## Data model

Feeds are **global and deduplicated by URL**; users subscribe to them. Two users
subscribing to the same URL results in one poll and one stored copy.

```sql
users(id TEXT PRIMARY KEY, name TEXT, created_at INTEGER)

feeds(id INTEGER PRIMARY KEY,
      url TEXT NOT NULL UNIQUE,
      name TEXT,
      poll_interval_s INTEGER,          -- NULL = global default
      enabled INTEGER NOT NULL DEFAULT 1)

subscriptions(user_id TEXT, feed_id INTEGER, PRIMARY KEY(user_id, feed_id))

articles(id INTEGER PRIMARY KEY,
         feed_id INTEGER NOT NULL,
         guid TEXT NOT NULL,
         title TEXT NOT NULL,
         link TEXT,
         summary TEXT,
         published_at INTEGER,          -- from feed; may be NULL or wrong
         fetched_at INTEGER NOT NULL,   -- when we saw it; never NULL
         sort_at INTEGER NOT NULL,      -- COALESCE(published_at, fetched_at)
         UNIQUE(feed_id, guid))

feed_state(feed_id INTEGER PRIMARY KEY,
           etag TEXT, last_modified TEXT,
           last_polled_at INTEGER, last_success_at INTEGER,
           consecutive_failures INTEGER NOT NULL DEFAULT 0,
           last_error TEXT, next_poll_at INTEGER NOT NULL)

filter_rules(id INTEGER PRIMARY KEY, user_id TEXT NOT NULL,
             pattern TEXT NOT NULL,
             action TEXT NOT NULL CHECK(action IN ('include','highlight')),
             enabled INTEGER NOT NULL DEFAULT 1)
```

Indexes: `articles(sort_at DESC, id DESC)`, `subscriptions(user_id)`. The
`UNIQUE(feed_id, guid)` constraint provides the dedup index.

**Timestamps are INTEGER epoch seconds, UTC.** RSS date formats are parsed once at the
ingest boundary and never again.

**`sort_at` is stored, not computed.** Feeds routinely publish wrong, missing, or
future-dated timestamps, and republish old items with today's date. Persisting the
resolved sort key keeps yesterday's scroll order stable and lets the index be used.

**Cursor:** base64 of `(sort_at, id)`.
Query: `WHERE (sort_at, id) < (?, ?) ORDER BY sort_at DESC, id DESC LIMIT ?`.
Row-value comparison requires SQLite 3.15+ (2016); the `python:3.12-slim` base image is
far newer, but the store should assert the version at startup rather than fail obscurely
on an unexpected platform.

**Retention:** hourly sweep, `DELETE FROM articles WHERE fetched_at < now - retention_days*86400`.
Keyed on `fetched_at`, not `sort_at` — otherwise a feed carrying bogus old dates would
have its articles deleted the moment they arrive. Weekly `VACUUM`.

### Filter semantics

Deliberately minimal, specified here so it cannot drift during implementation:

- A rule is a **case-insensitive substring** match against `title + " " + summary`.
- No regex, no boolean operators, no precedence rules.
- `action: include` — if a user has **one or more** `include` rules, only articles
  matching at least one are delivered to that user. If a user has **zero** `include`
  rules, all articles pass.
- `action: highlight` — matching articles are flagged `highlighted: true` in the payload
  and rendered in an accent color. Highlighting never affects inclusion.
- Filters apply to both the WebSocket stream and REST paging, so live and scrollback
  always agree.

## Polling and ingest

**Scheduler:** a single loop wakes every second, selects `enabled` feeds where
`next_poll_at <= now`, and dispatches them through an `asyncio.Semaphore`
(`max_concurrent_polls`, default 8). Table-driven rather than one task per feed, so
feeds added or removed via the admin API are picked up on the next tick with no task
lifecycle to manage.

**Per-poll sequence:**

1. `GET` with `If-None-Match` / `If-Modified-Since` from `feed_state`; 15s timeout;
   5 MB response cap; User-Agent `rss-ticker/<version> (+<public_base_url>)`.
2. `304` → success; no parse, no writes beyond `next_poll_at`. This is the common case
   and stays cheap.
3. `200` → `feedparser.parse()`; store the new ETag / Last-Modified.
4. Normalize: `guid = entry.id or entry.link or sha256(title + "\x00" + str(published or ""))`,
   where `published` is the parsed epoch value. The final fallback matters — some feeds
   supply neither id nor link. The null separator prevents two different title/date
   pairs from concatenating to the same string, and the `or ""` makes a missing date
   produce a stable hash rather than a crash. An entry with no id, no link, and no
   usable title is dropped and counted, not stored.
5. `INSERT ... ON CONFLICT(feed_id, guid) DO NOTHING ... RETURNING`. **Only returned
   rows are broadcast.** Dedup and change-detection are the same database operation
   rather than application bookkeeping that can drift out of sync.
6. `next_poll_at = now + interval ± 10% jitter`, so feeds added together do not stay
   permanently synchronized.

**Cold-start suppression:** the first successful poll of a newly added feed (detected via
`feed_state.last_success_at IS NULL`) inserts every item but broadcasts none. Without
this, adding a feed dumps its entire back catalogue into the live window, which reads as
a malfunction. The articles are cached and immediately scrollable.

**Failure handling:**

| Condition | Response |
|---|---|
| Timeout / connection error | `consecutive_failures++`; backoff `interval × 2^n`, capped at 1 hour |
| `429` / `503` with `Retry-After` | Honor the header exactly; it overrides our backoff |
| `5xx` without `Retry-After` | As timeout |
| `404` / `410` | Back off to the 1-hour cap; **do not auto-disable** |
| Parse failure, or `bozo` with zero entries | Treated as failure; back off |
| `bozo` **with** entries | Accept them — much real-world RSS is technically malformed |

Every failure records `last_error`, surfaced via `/api/health`. Success resets
`consecutive_failures` to 0.

Two principles: **a failing feed is contained** — it is one row in one table, never an
exception that escapes the scheduler and stalls other feeds. And **nothing disables
itself** — a feed returning 404 through someone else's bad deploy should recover without
manual re-enabling; silent self-disabling is a failure discovered weeks late.

**Backpressure:** a WebSocket client whose send queue exceeds 1000 frames is dropped
rather than awaited. A stalled reader must never back up ingest.

## Widget

A Bloomberg-style **news window**: a vertical list, newest at top, new articles pushing
older ones down, scrollbar on the right. Row height is fixed, so the number of visible
rows follows the widget's height through normal overflow — no layout arithmetic in JS,
and it reflows when the OpenBB grid cell is resized.

Each row: local time (full timestamp on hover), source, headline. Highlighted articles
render in an accent color. The header shows a live/stale connection dot and feed count,
so a dead poller is visibly distinct from a quiet news day.

**Load:** fetch first page → render → open WebSocket.
**Live:** prepend new articles.
**Scrollback:** approaching the bottom fetches the next cursor page — infinite scroll
across cached history, as deep as retention allows.

Three behaviors specified because they are the common failure points:

1. **Scroll anchoring.** When scrolled to the top, new articles simply appear. When
   scrolled away from the top, they are still inserted, but `scrollTop` is compensated
   by the inserted height so the row being read stays put, and a sticky "N new
   headlines ↑" indicator appears.
2. **Links open in a new tab** (`target="_blank" rel="noopener"`). The widget is an
   iframe; ordinary navigation would replace the ticker with the article and leave no
   way back.
3. **Reconnect fills gaps.** On socket reopen the widget requests everything newer than
   the newest ID it holds, rather than assuming nothing was missed during the outage.

Times are rendered in the browser's local timezone.

## OpenBB integration

The server publishes `/widgets.json`, registered in OpenBB Workspace as a custom
backend. The ticker is declared as `type: "iframe"`, whose `endpoint` must be an
**absolute URL** that Workspace loads directly.

```json
{
  "news_ticker": {
    "name": "News ticker",
    "description": "Live RSS headlines, newest first",
    "category": "News",
    "type": "iframe",
    "endpoint": "<public_base_url>/widget?user=art",
    "gridData": {"w": 40, "h": 8},
    "source": "RSS"
  }
}
```

A second entry with `gridData: {"h": 2}` registers the same URL as a short bottom rail,
so either footprint can be dropped onto a dashboard.

**Why iframe rather than `type: "html"`.** With `html`, Workspace injects the returned
markup into its own page, so the widget's JavaScript runs on `pro.openbb.co`'s origin and
every call back to this server is cross-origin. With `iframe`, the page is served by this
server on its own origin, making the REST backfill and the WebSocket same-origin.

That choice also sidesteps two documented gaps in OpenBB's own contract (see
"Unverified"): whether Workspace's configured auth header reaches a WebSocket handshake,
and whether iframe widgets receive any auth at all. Neither can affect a same-origin
socket we define both ends of.

**`public_base_url` is required config.** The absolute URL is unknown at image-build
time, so `widgets.json` is rendered at startup rather than shipped static. A wrong value
produces a blank frame with no other symptom, so `/api/health` echoes the URL it is
publishing.

We do **not** use OpenBB's `live_grid` widget type. It renders a table rather than a news
window, and its WebSocket message envelope has no prose specification — it is only
inferable from OpenBB's example code.

## Config and packaging

```yaml
public_base_url: https://ticker.example.net
admin_key: ${TICKER_ADMIN_KEY}        # env expansion; secret not stored in the file
manifest_key: ${TICKER_MANIFEST_KEY}  # added by the amendment; required
retention_days: 7
default_poll_interval_s: 300
max_concurrent_polls: 8

users:
  - id: art
    name: Art
    token: ${TICKER_TOKEN_ART}        # added by the amendment; required
    feeds:
      - {url: "https://feeds.reuters.com/reuters/businessNews", name: Reuters Business}
      - {url: "https://www.ft.com/rss/home", name: FT, poll_interval_s: 600}
    filters:
      - {pattern: nvidia, action: highlight}
```

**Boot reconciliation is additive only.** It inserts users, feeds, subscriptions, and
filters that do not exist, and updates names and intervals. It never deletes. Otherwise
a restart would silently discard feeds added through the admin API — data loss that
would go unnoticed for a long time.

**Image:** `python:3.12-slim`, non-root user, `docker buildx build --platform
linux/amd64,linux/arm64`, published to `ghcr.io/artcashin/rss-ticker`. SQLite on a named
volume at `/data`. `HEALTHCHECK` against `/api/health`. Multi-arch is safe here because
every dependency is pure Python — there are no compiled wheels of the kind that forced
`openbb-docker` to amd64-only.

## Testing

| Layer | Approach |
|---|---|
| `store` | Real in-memory SQLite. Dedup, cursor paging stability under concurrent insert, retention keyed on `fetched_at`, filter evaluation including the zero-`include`-rules case |
| `poller` | `httpx.MockTransport` with canned RSS fixtures: 304, 429 with `Retry-After`, malformed XML, `bozo`-with-entries, missing guid and link, cold-start suppression |
| `api` | FastAPI `TestClient` against a seeded DB: paging correctness, admin-key enforcement on writes, WebSocket broadcast, slow-client drop |
| integration | `docker compose up` with a fixture feed served locally; assert an article reaches a WebSocket client end-to-end |

### Acceptance criteria

1. A new article in a served fixture feed reaches a connected WebSocket client within one
   poll interval.
2. Restarting the container loses no cached articles; scrollback still reaches retention
   depth.
3. A feed returning 500 for an hour neither delays nor drops polls of any other feed.
4. Adding a feed through the admin API broadcasts nothing, but its articles are
   immediately scrollable.
5. `widgets.json` validates and the widget renders in OpenBB Workspace at both `h: 2` and
   `h: 8`. **Manual** — requires OpenBB Workspace.
6. The image runs on both `linux/amd64` and `linux/arm64`.

Criteria 1–4 and 6 are automatable. Criterion 5 is the only one requiring a human.

## Out of scope

Explicitly excluded from this version, listed so they are recognizable as decisions
rather than omissions:

- Read/unread state or unread counts
- Cross-feed deduplication of the same story, and topic clustering
- Full-article fetching, sentiment scoring, thumbnail extraction
- Regex or boolean filter expressions
- A UI for editing feeds (the admin API is `curl`-level)
- ~~Authentication on read endpoints~~ — **superseded**, see the amendment below

Items 1, 2, and 4 are additive later: the schema and module boundaries do not preclude
them, and retention means historical data will exist to tune cross-feed dedup against.

## Unverified

Established by research against OpenBB's docs and example repository, but **not**
confirmed against a running Workspace instance. Each should be checked during
implementation rather than assumed:

1. Whether Workspace applies any authentication to iframe document loads. Current
   evidence says no; the design does not depend on it either way.
2. `type: "iframe"` requiring an absolute `endpoint` is stated only in OpenBB's example
   source code, not in the reference documentation.
3. The exact behavior of an iframe widget when the OpenBB grid cell is resized — the
   widget's responsive reflow assumes the iframe viewport resizes with the cell.
4. Whether `widgets.json` tolerates two entries pointing at the same `endpoint` with
   different `gridData`.

None of these blocks implementation; each is cheap to check once the container is
running and registered.

---

## Amendment: untrusted-network authentication (2026-07-21)

**Status:** approved (design), pending implementation
**Amends:** "Endpoints" (auth paragraph and endpoint table), "Config and packaging",
"Out of scope"

### The threat model changed

The original design assumed the container sits on a trusted network, so read endpoints
were open. That assumption is withdrawn. **Assume an untrusted network:** anyone who can
route a packet to the port is a potential attacker, and reads are no longer open.

What specifically motivated it is not a hypothetical — it is a chain that works today
with nothing but `curl` and the base URL:

1. `GET /widgets.json` is unauthenticated and enumerates every user id (the manifest keys
   are `news_window_<user>` and every `endpoint` carries `?user=<id>`).
2. `GET /api/feeds?user=<id>` is unauthenticated and returns that user's **full feed
   URLs**.
3. Feed URLs commonly embed API tokens — `https://api.vendor.com/rss?apikey=…`,
   `https://user:token@host/feed`, a signed path segment. Handing them out is handing out
   the credentials to somebody else's paid data service.
4. `GET /api/news?user=<id>` then returns that user's headlines, which are themselves a
   disclosure: a curated feed list is a statement about what its owner is watching.

`GET /api/health` compounds it by publishing every feed URL in the deployment, including
feeds belonging to users the caller never had to guess.

The step that turns a guess into a walk is step 1. `/widgets.json` was the directory.

### Three verified constraints

These are stated here, with their consequences, so that a later reader does not
"simplify" the design back into something that cannot work in this embedding.

1. **OpenBB's configured auth does not reach an iframe widget.** Workspace attaches its
   configured header or query param to endpoints *it* fetches. For `type: "iframe"` the
   browser loads the `endpoint` URL directly as a document; no OpenBB auth reaches that
   document load, and none reaches anything the loaded page subsequently calls.
   Consequence: whatever credential `/widget` needs must already be in the URL that
   `widgets.json` published.
2. **Browsers cannot set headers on a `WebSocket` constructor.** The `WebSocket` API
   takes a URL and an optional subprotocol list, and nothing else. Consequence:
   `/ws/news` can only carry a credential in the URL.
3. **Third-party cookies are blocked in cross-site iframes** by Safari, and increasingly
   by Chrome. A cookie session — the normally correct answer for a browser-facing app,
   and the one that keeps the secret out of URLs and logs — is exactly the mechanism this
   embedding breaks. Consequence: **the token lives in the URL.** This is not a shortcut
   taken over a cookie session; it is what remains after the cookie session is removed as
   an option.

### Design

**Per-user tokens.** Each user gets a secret `token`, generated with
`secrets.token_urlsafe(32)`. Tokens are compared with `hmac.compare_digest` **on bytes**
— `compare_digest` raises `TypeError` on `str` operands containing non-ASCII characters,
and the credential is caller-controlled, so a `str` comparison is a remote crash. The
existing `admin_key_ok` helper in `api.py` already encodes before comparing; that helper
is generalized into one `secret_ok(provided, expected)` primitive and every check goes
through it. An empty or missing expected value never matches, so a user row with no token
is closed, not open.

**Tokens are configured, not generated at runtime.** They live in `config.yaml` under
each user, written as `${ENV_VAR}` so the secret is in the environment and not in the
file — the same `_expand` mechanism `admin_key` already uses. Boot reconciliation
persists them into the `users` table, which is what the request path reads.

```yaml
admin_key: ${TICKER_ADMIN_KEY}        # writes, and the health detail
manifest_key: ${TICKER_MANIFEST_KEY}  # widgets.json; this is what OpenBB holds

users:
  - id: art
    name: Art
    token: ${TICKER_TOKEN_ART}
```

**A user without a configured token is a startup error.** `load_config` raises
`ConfigError`. It must not be a silently-open account, and it must not be a silently
*closed* one either — a server that boots and then 401s every request looks like a bug in
the token, not a hole in the config. Tokens must be at least 32 characters: a value
arriving through an environment variable can be a placeholder or a truncated paste, and
that failure is otherwise invisible. Duplicate tokens across users are likewise a startup
error — one user reading another's feed through a copy-pasted secret is not a failure that
announces itself.

**Three server secrets, and `manifest_key` is separate from `admin_key` on purpose.**

| Secret | Gates | Who holds it |
|---|---|---|
| `admin_key` | writes on `/api/feeds`, the per-feed detail on `/api/health` | the operator |
| `manifest_key` | `/widgets.json` | pasted into OpenBB Workspace |
| per-user `token` | that user's reads | published in the iframe URL |

`manifest_key` is the value a human types into a third-party web UI, where it is stored
by that UI, synced to that UI's account, and visible to anyone who can open that UI's
settings pane. If it were the admin key, handing OpenBB Workspace a read credential would
also be handing it the ability to add and remove feeds for every user. They are separate
so that the blast radius of the credential that leaves the operator's control is limited
to the document it is supposed to fetch. Both are required config; both are
env-expandable; neither is ever logged.

**`/widgets.json` requires the key OpenBB Workspace is configured to attach.** The
manifest contains every user's token inside the published `endpoint` URLs, so it is the
most sensitive document the server serves. It requires header `X-API-KEY` — OpenBB's
documented convention for a custom backend's API key — carrying `manifest_key` as its
value. It must not be reachable unauthenticated. Nothing else changes about it: Workspace
fetches it itself (constraint 1 applies only to the iframe document load), so the header
does arrive.

**Protected read endpoints.** `/widget`, `/api/news`, `/api/feeds` (GET), and `/ws/news`
require a valid `(user, token)` pair. The token is accepted two ways:

- `Authorization: Bearer <token>` — the correct channel, used by anything that can set
  headers.
- `?token=<token>` — because the WebSocket handshake and the iframe document load have
  no other option (constraints 1 and 2). The header wins when both are present.

**An unknown user and a wrong token are indistinguishable.** Both return **401** with the
identical body `{"detail": "Invalid credentials"}`. The original design's **400** for an
unknown user is withdrawn on these endpoints: under the new threat model it is a user-id
oracle, and enumerating user ids is step 1 of the attack chain this amendment exists to
break. The **422** for a structurally missing `user` param stays — a request with no
`user` at all reveals nothing about which users exist. The **400** also stays on the
admin-gated write endpoints, whose caller can already list every user.

Indistinguishable means in timing as well as in body. A naive implementation returns
early when the user does not exist and only reaches `compare_digest` when they do, which
is a measurable difference and therefore the same oracle wearing a stopwatch. The token
check must perform one comparison either way, against a per-process random decoy when
there is no stored token, and discard the result.

The WebSocket closes with **4401** and the same reason string for both cases, and **must
reject before registering a subscriber** — a socket closed after `broadcaster.subscribe`
leaks a subscription and can receive a frame in the race window.

**The admin key is a master credential.** This is a decision, not an accident: a request
bearing a valid admin key satisfies any auth check on any endpoint, including the
per-user read endpoints. It grants no new authority — the admin key already authorizes
every write and already reads the per-feed detail. The alternative, requiring the admin
key *and* the target user's token on write endpoints, would mean an operator cannot add a
feed without first looking up that user's secret. Note the direction of the containment:
the admin key subsumes the user tokens, but it does **not** subsume `manifest_key` in
reverse — holding `manifest_key` confers nothing beyond `/widgets.json`.

**Feed URL redaction.** `/api/feeds` returns `url` reduced to scheme and host for
token-authenticated callers; full URLs only with the admin key. Redaction uses the parsed
**hostname**, never the raw netloc, because `https://user:apikey@host/feed` carries the
secret in the netloc's userinfo. The widget only ever displayed the host anyway.

**`/api/health` shrinks.** The public response is `{"status": "ok"|"degraded", "version":
…, "public_base_url": …}`. `public_base_url` stays public deliberately: it is not a
secret — anyone who can call the endpoint already knows the host — and it is the original
design's only symptom for a misconfigured base URL, which otherwise presents as a blank
iframe with nothing else to go on. What moves behind the admin key is the per-feed
detail: URLs, `last_error`, `consecutive_failures`, and poll timings.

`status` is `degraded` when any enabled feed has `consecutive_failures >=
DEGRADED_AFTER_FAILURES`, a named constant set to **3**. Flipping on the first failure
would mark the deployment unhealthy for every transient network blip; with `restart:
unless-stopped` in front of it, that is a restart loop caused by someone else's flaky
feed. Three consecutive failures is a feed that is actually down. The status code stays
200 in both states, so Docker's `HEALTHCHECK` — which calls this from inside the
container and only tests for HTTP 200 — needs no credential and never restarts the
container over feed health.

**Access logging is turned off in the application.** `uvicorn.run(..., access_log=False)`.
Tokens appear in the query string of every `/widget`, `/api/news`, `/api/feeds`, and
`/ws/news` request, and the request line is what uvicorn logs. A redacting filter on
`uvicorn.access` was considered and rejected: it has to reach into the shape of uvicorn's
log record arguments, which is an internal detail, and a filter that silently stops
matching after a dependency bump fails open — writing tokens to disk, forever, with no
signal. Turning the logger off cannot fail that way. **Request logging belongs at the
reverse proxy**, which is terminating TLS in front of this service anyway, and must be
configured there to log the path without the query string. This is stated as an
operational requirement below rather than left as a preference.

**Never log a token.** Not at any level, not in an exception message, not in a config
error. `ConfigError` text names the *user*, never the value.

### Endpoint auth matrix

| Endpoint | Credential | Rejection |
|---|---|---|
| `GET /` | none — service name, version, manifest URL | — |
| `GET /widgets.json` | `X-API-KEY: <manifest_key>` | 401 |
| `GET /widget` | `(user, token)`, or admin key | 401, 422 with no `user` |
| `GET /api/news` | `(user, token)`, or admin key | 401, 422 with no `user` |
| `GET /api/feeds` | `(user, token)` → redacted URLs; admin key → full URLs | 401, 422 with no `user` |
| `POST /api/feeds` | admin key (`X-Admin-Key`) | 401; 400 for an unknown user |
| `DELETE /api/feeds/{id}` | admin key (`X-Admin-Key`) | 401; 400 for an unknown user |
| `WS /ws/news` | `(user, token)` in the URL | close 4401 |
| `GET /api/health` | none → status, version, `public_base_url`; admin key → feed detail | — |

Every 401 in that table carries the same body, `{"detail": "Invalid credentials"}`,
except `/widgets.json`, whose caller is a configured backend rather than a user and which
returns `{"detail": "API key required"}`.

### Honest costs

This design is weaker than a cookie session, and the weakness is the price of the iframe
embedding rather than an oversight:

- **The token is visible in the iframe URL.** It is in `widgets.json`, in the OpenBB
  dashboard's saved widget config, in the browser's DevTools network pane, and readable
  by anyone with access to the Workspace account or the machine. A cookie with
  `HttpOnly` would be invisible to page scripts and absent from URLs; constraint 3
  removes that option.
- **The token would appear in any access log that records the request line.** This server
  writes none — `access_log=False`. The cost is real and is paid deliberately: there is
  no application-level request log at all, so latency, status-code, and traffic
  visibility have to come from the reverse proxy. That is the correct place for it, but
  it does mean a deployment without a proxy has no request visibility whatsoever.
- **The token appears in `Referer` if the widget leaks one.** Mitigated by
  `Referrer-Policy: no-referrer` on the `/widget` response and `rel="noreferrer"` on
  outbound headline links, which the widget already sets.
- **Response-body and timing uniformity is a discipline, not a mechanism.** Nothing
  structurally prevents a future endpoint from returning a distinguishable 401. The
  uniform body and the decoy comparison are pinned by tests for that reason.
- **Tokens do not expire and there is no revocation list.** Rotation is: change the
  environment variable, restart, re-register the backend in Workspace. Boot
  reconciliation overwrites the stored token, so the old one stops working immediately.
- **`manifest_key` lives inside a third-party UI.** Separating it from `admin_key` bounds
  what its disclosure costs — the manifest, and therefore every user token in it — but it
  does not make that disclosure cheap. Rotating it means rotating every user token too,
  since the manifest is what published them.
- **A token is a bearer credential with no binding to a browser, IP, or session.** Anyone
  who obtains it is that user until it is rotated.

### Deliberately out of scope

Excluded so they read as decisions rather than omissions:

- **A login page or server-side sessions.** Constraint 3 is the reason: the session
  cookie a login page would set is the thing cross-site iframes block.
- **mTLS.** Client certificates cannot be presented by an iframe the browser loads on the
  user's behalf without a certificate-selection prompt, and OpenBB Workspace has no place
  to configure one.
- **Rate limiting.** Noted as **future hardening**, not built now. Token guessing against
  a 43-character `token_urlsafe(32)` value is not a realistic attack, so a limiter would
  be defending the wrong thing at this stage. It becomes worth having if the service is
  ever exposed directly to the public internet.

### Required deployment context

These are **deployment** concerns. They are documented here and in the README because the
design is only sound with them in place, and they are deliberately **not** built into the
application — an app that terminates its own TLS or embeds its own VPN is an app with two
jobs.

- **TLS is mandatory.** The token is in the URL and the URL is in the request line. Over
  plain HTTP, every token in this design is readable by anyone on the path. Terminate TLS
  at a reverse proxy in front of the container and set `public_base_url` to the `https://`
  URL, so `widgets.json` publishes `https`/`wss` endpoints. Serving this on `http://` on
  an untrusted network is a misconfiguration, not a degraded mode.
- **A private network is strongly recommended.** Tailscale, WireGuard, or an equivalent
  overlay in front of the service means the token is a second layer rather than the only
  one. It is the difference between an exposed service with a bearer credential and a
  service that is not reachable at all without one.
- **Request logging is the proxy's job, and it must omit query strings.** The application
  logs no requests at all. Configure the proxy to log the path only (nginx:
  `$uri` rather than `$request`; Caddy: strip `uri.query`). A proxy left on its default
  combined-log format writes every user token to disk on every request, which is the
  single most likely way this design leaks in practice.
- **`X-API-KEY` is what Workspace sends.** Register the custom backend in OpenBB
  Workspace with **`manifest_key`** — not the admin key — as the backend's API key, so
  `/widgets.json` resolves and a compromise of the Workspace account does not confer
  write access.

### Amended acceptance criteria

Added to the six in "Testing":

7. Every protected endpoint returns `401` with no token and `200` with a valid one, and
   `/ws/news` closes `4401` on a bad token without registering a subscriber.
8. An unknown user and a known user with a wrong token produce byte-identical `401`
   responses on every protected endpoint, and a missing `user` param is still `422`.
9. `/widgets.json` returns `401` without `X-API-KEY`, `401` when given the *admin* key,
   and the manifest when given `manifest_key`.
10. `/api/feeds` returns scheme-and-host URLs to a token caller and full URLs to an admin
    caller, including for a feed URL containing userinfo.
11. Public `/api/health` contains `public_base_url` but no feed URLs; `status` is
    `degraded` only at `DEGRADED_AFTER_FAILURES` consecutive failures, and the code stays
    200.
12. Booting against a `config.yaml` missing `manifest_key`, or missing any user's
    `token`, fails with a `ConfigError` naming exactly what is absent — never a
    half-open start.
13. The widget renders inside OpenBB Workspace with the token supplied only by the
    published `endpoint` URL, and its WebSocket connects. **Manual** — requires OpenBB
    Workspace, and is the only real test of constraint 1.

Criteria 7–12 are automatable. Criterion 13 requires a human, as criterion 5 does.
