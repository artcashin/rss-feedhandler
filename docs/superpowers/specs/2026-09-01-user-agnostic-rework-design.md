# The user-agnostic rework — no users, no keys, subscriber-counted feeds

**Status:** approved (design), pending implementation plan
**Date:** 2026-09-01
**Supersedes:** the whole of
`2026-07-21-rss-news-ticker-design.md`'s "Amendment: untrusted-network
authentication", the whole of `2026-07-27-tailscale-identity-auth-design.md`,
and the base design's user model, filter rules, widget module, and OpenBB
integration. The poller, store, broadcast, and packaging sections of the base
design stand except where amended below.
**Pairs with:** bdobb-v2's
`docs/specs/v8.0.0-addendum-user-agnostic-ticker.md`, which is the client
side of this same design and the source of record for the widget behavior.

## Why

The reason for the change is to remove any security requirement on the
server. A server that only relays public RSS data protects nothing: there
are no per-user feed lists to disclose, no credentials worth stealing, no
config worth hijacking. Every secret in the current design — admin key,
manifest key, per-user tokens — exists to guard things this design deletes,
so the secrets go with them. The whole auth amendment, and the Tailscale
identity-auth design that softened it, dissolve.

What replaces the user model is one piece of per-client state: **a
subscriber count per feed**. The server polls a feed while at least one
connected client wants it and drops it when the count reaches zero.

## Decisions

| # | Decision | Rejected alternative / reversal |
|---|---|---|
| 1 | No users, no auth, every endpoint open | Reverses base decision 2 (multi-user) and the entire auth amendment |
| 2 | Subscription = the websocket session's first frame | `POST /api/feeds` admin writes; a standing subscriptions table |
| 3 | Feed lifecycle by live subscriber count; drop at zero | Idle-expiry window; keep-forever with admin pruning |
| 4 | Clients filter; the wire carries everything, tagged by `feed_id` | Server-side per-client filtering (needs users back) |
| 5 | No served widget, no `widgets.json` — bdobb-v2's built-in is the only consumer | Reverses base decision 5 (iframe widget hosted here) |
| 6 | Protection is deployment placement (tailnet-only), not code | Any key or auth mode |

## Protocol

```
GET  /                      root info
GET  /api/news?limit=&before=&after=   paged, cursor-based, all feeds, no user param
WS   /ws/news               first frame subscribes; then live push, all subscribed activity
GET  /api/feeds             feed records for the whole pool (id, url, title, favicon)
GET  /api/health            full detail, no key — nothing is redacted anymore
```

**Subscribing rides the websocket.** The client's first frame is its
subscription:

```json
{"subscribe": [{"url": "https://…/feed", "name": "FT"}, …]}
```

An unknown URL is added to the pool and polling starts; a URL another
session already named is deduplicated — never a second feed row, never a
second poll schedule (this is the base design's global-feeds model, kept).
Dedup is by canonical feed URL: scheme lowercased, host lowercased,
trailing slash stripped — nothing cleverer. `name` is used only if the feed
is new to the pool. The server replies with the matching feed records
`{id, url, title, favicon}` so the client can map stream items back to its
feeds, then streams. Every article frame carries its `feed_id`; filtering
to what a given client subscribed is the client's job, by design.

Tying the subscription to the socket is what lets a server with no users
count subscribers: the count is simply how many open sockets asked for the
feed. A session may send a new `subscribe` frame at any time; it replaces
that session's set (counts adjust by the difference).

**Removed endpoints and machinery:** `/widgets.json`, `/widget`, the
`widget` module and its static UI, `POST /api/feeds`, `DELETE /api/feeds`,
`X-Admin-Key`, `X-API-KEY`, `?user=`, `?token=`, Bearer handling, the
401/4401/422 auth matrix, the decoy comparison, URL redaction, and the
`tailscale_auth` identity mode. The 422-on-missing-user rule is void — there
is no `user` param to miss. CORS shrinks to bdobb-v2's origins
(`http://localhost:1420` and the Tauri origin); the OpenBB origins go.

## Feed lifecycle

- Subscriber counts are **in-memory**, derived from open sockets. A socket
  closing decrements every feed it subscribed.
- At **zero subscribers a feed stops being polled immediately** (the base
  design's `enabled = 0`, repurposed — no longer settable by any API).
- The **existing hourly retention sweep** deletes feeds still at zero, and
  their articles age out under normal retention. Reusing the sweep gives a
  free grace window: a server restart or client blip re-subscribes within
  seconds (bdobb-v2 reconnects on a 3000 ms cycle and re-sends its list on
  every connect) and finds its feeds re-enabled with history intact,
  instead of thrashing drop-and-re-add.
- Nothing is polled while nobody is connected. On reconnect a dropped feed
  re-adds and seeds from whatever the publisher currently lists — "what
  happened while I was away" is bounded by the feed's live back catalogue.
  The base design's cold-start suppression applies to the re-add, so the
  back catalogue is cached and scrollable but never broadcast as breaking
  news.

## Data model changes

- **Dropped tables:** `users`, `subscriptions`, `filter_rules`. Highlight
  and include semantics move to the client (bdobb-v2 widget config).
- **Kept:** `feeds` (with `enabled` as above), `articles`, `feed_state`,
  and every ingest rule — guid fallback hashing, `sort_at` stored,
  cursor paging, retention keyed on `fetched_at`, backoff table,
  bozo-with-entries acceptance, backpressure drop at 1000 frames.
- `broadcast` loses per-user filtering and becomes a plain fan-out of every
  inserted article to every connected socket.

## Config and packaging

```yaml
retention_days: 7
default_poll_interval_s: 300
max_concurrent_polls: 8
```

That is the whole file. Gone: `public_base_url` (existed only for the
manifest and widget), `admin_key`, `manifest_key`, `users:` (and with it
every `TICKER_*` env var and the env-expansion requirement — openbb-docker
deletes `rss-ticker.env*`). The poll User-Agent, which embedded
`public_base_url`, becomes `rss-ticker/<version> (+https://github.com/artcashin/rss-feedhandler)`.
`uvicorn` access logging may be turned back on: the only reason it was off
was tokens in request lines, and there are none.

## Honest costs

Open by design means open to anyone with network reach:

- **Anyone who can route a packet can make this server poll arbitrary
  URLs** (subscribe is unauthenticated), read every article, and list every
  feed URL. Accepted because deployment placement is the entire protection:
  loopback bind, tailnet-only behind the Serve sidecar, and openbb-docker's
  "never funnel this" warning — now load-bearing as the *only* wall, not as
  a second layer.
- **Do not subscribe credentialed feed URLs.** The old design redacted feed
  URLs precisely because they can embed vendor API keys; this design
  redacts nothing. Public publisher feeds only — the moment a feed URL is
  itself a secret, this server is the wrong place for it, and that feature
  must bring its own security case with it.
- **Feed lifetime is only as durable as its readers.** A feed nobody keeps
  a socket open for gets dropped at the next sweep and loses its cached
  history to retention. That is the intended semantics, not a leak.

## Testing deltas

Superseded criteria 7–13 (the auth matrix) are removed. Added:

1. A first-frame subscribe naming a new URL starts polling it and returns
   its feed record; naming an existing URL returns the same `feed_id` with
   no second feed row or poll schedule.
2. Two sockets subscribing the same feed, then one closing, keeps the feed
   polled; the second closing stops polling immediately, and the sweep
   removes the feed only if still at zero.
3. A re-subscribe before the sweep re-enables the feed with its cached
   articles intact (scrollback still works).
4. An article frame carries `feed_id`, and `GET /api/news` serves all
   feeds with no `user` param accepted or required.

---

## Addendum (2026-09-03): the contract as the client fixed it, and implementation decisions

**Status:** approved in chat 2026-09-03; implemented by
`docs/superpowers/plans/2026-09-03-user-agnostic-rework.md`.
**Pairs with:** bdobb-v2 v8.0.0, whose News widget was built first against
the contract in its `docs/superpowers/specs/2026-09-03-v8.0.0-news-widget-design.md`.
Where this design was silent, that document fixed the wire; this addendum
adopts those points and settles what neither said.

### Wire contract, exactly

- **Subscribe frame** (client → server, first frame, and any later frame):
  `{"subscribe": [{"url": "https://…", "name": "optional"}, …]}`. Each `url`
  must be `http`/`https` and at most 2048 characters; at most 200 entries.
  A frame that is not valid JSON, not an object with a `subscribe` list, or
  that violates those limits closes the socket with code **4400** and a
  reason; the client's ordinary reconnect cycle applies. A later `subscribe`
  frame replaces the session's set (counts adjust by the difference).
- **Reply frame** (server → client, once per subscribe frame):
  `{"feeds": [{"id": int, "url": str, "title": str|null, "favicon": str|null}]}`
  — one record per distinct canonical URL in the frame, in the frame's order.
  `url` is the canonical form the server stored; `title` is the feed's stored
  name (the client's `name` when the feed was new to the pool, else whatever
  it already had); `favicon` is a `data:image/…;base64,` URI or null. A frame
  carrying a `feeds` key is always the reply; every other server frame is an
  article.
- **Article frame / REST row:**
  `{id, feed_id, cursor, title, link, summary, source, author, published_at, sort_at}`.
  `author` is new (feedparser's `author`, `dc:creator` folded; null when the
  entry has none). `highlighted` is gone with the filter rules. `source` is
  the feed's stored name. Timestamps are epoch seconds.
- **REST:** `GET /api/news?limit=&before=&after=` (all feeds, `limit` 1–200,
  default 50), `GET /api/feeds` → `{"feeds": [{id, url, title, favicon,
  subscribers, enabled}]}`, `GET /api/health` → `{status, version, feeds: […]}`
  with the full per-feed detail (nothing is redacted), `GET /` →
  `{service, version}`.
- **Canonical URL:** scheme and host lowercased, one trailing slash stripped,
  nothing else. `feeds.url` stores the canonical form; a subscribe URL is
  canonicalised before lookup, so `HTTPS://Host/feed/` and `https://host/feed`
  are one feed. Existing rows are canonicalised on migration; a row whose
  canonical form collides with another's is left as-is and, being
  unreachable by lookup, is dropped by the sweep once at zero subscribers.

### Implementation decisions

| # | Decision | Rejected |
|---|---|---|
| A | Subscriber counts live in the `Broadcaster`, in memory, keyed by feed id; the store's `enabled` flag mirrors "count > 0" so the poller's `due_feeds` query is unchanged | A subscriptions table |
| B | **Every feed starts disabled at boot** (`disable_all_feeds`); the first subscribe re-enables it. Nothing is polled while nobody is connected, from the first second | Trusting `enabled` as persisted |
| C | The hourly sweep also **drops feeds still at zero subscribers** (feed, `feed_state`, and its articles), after the article retention delete | Immediate deletion at zero |
| D | A feed **new to the pool** gets its favicon resolved in a background task at subscribe time (the shared `httpx` client), so a reconnect within seconds sees it; the startup refresh pass stays | Waiting for the next restart |
| E | `users`, `subscriptions` and `filter_rules` tables are dropped by migration; `feeds.group` and `feeds.title_format` columns are dropped too (SQLite ≥ 3.35, which the store already requires); `articles.author TEXT` is added | Leaving dead columns |
| F | Per-feed `poll_interval_s` is gone with the config feeds: every feed polls at `default_poll_interval_s`. The operator sets that value for the deployment (90 s for a wire-heavy pool) | A `poll_interval_s` on the subscribe entry (not in the client's contract; can be added later) |
| G | CORS: `http://localhost:1420`, `http://localhost:4173`, `tauri://localhost`, `http://tauri.localhost`; no credentials | The OpenBB origins |
| H | Config accepts exactly `retention_days`, `default_poll_interval_s`, `max_concurrent_polls`, `bind_host` (default `0.0.0.0`); **any other top-level key is a `ConfigError`** naming it, so a v8 config fails loudly instead of silently ignoring `users:` | Ignoring unknown keys |
| I | `${ENV}` expansion in the config stays (three lines, harmless); nothing requires an environment variable any more | Removing expansion |
| J | Version **9.0.0**, image `ghcr.io/artcashin/rss-feedhandler` (the name the live deployment already pulls), User-Agent `rss-ticker/<version> (+https://github.com/artcashin/rss-feedhandler)`; uvicorn access log on | 8.1.0; the old `rss-ticker` image name |
| K | Removed outright: `widgets.py`, `static/`, `filters.py`, `reconcile.py`, the widget JS harness and its Node CI step, `docker-compose.nas.yml`'s and `docker-compose.yml`'s secrets | Keeping any of it behind a flag |

### Honest costs, added

- **A subscribe frame is an unauthenticated instruction to poll a URL.** The
  scheme check and the size limits bound the damage, not the intent.
  Placement (tailnet-only) remains the whole protection.
- **Poll cadence is global.** A pool mixing a 30-second wire and a weekly
  blog polls both at the same interval.
- **A restart drops nothing but polls nothing** until a client reconnects;
  the client reconnects every 3 s, so in practice the gap is seconds.
