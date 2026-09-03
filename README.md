# rss-ticker

*Companion code for Adventures in OpenBB, Ep. 8: "All the News That Fits, We Print."*

A user-agnostic RSS feed pool with a live wire. Clients tell it which feeds
they want over a websocket; it polls the union, pushes every new article to
every connected client tagged by `feed_id`, and keeps cursor-paged history in
SQLite. The client filters. There are no users, no keys and no configured
feeds — the consumer is bdobb-v2's built-in **News** widget.

## Quick start

    cp config.example.yaml config.yaml   # four operational settings
    docker compose up -d

The default compose publishes `127.0.0.1:8088` and nothing else. **Every endpoint
is open**: whoever can reach the port can make this server poll a URL, read
every article and list every feed. That includes a read amplification: a
subscribe frame makes the server GET any http(s) URL it can reach — loopback
inside its own network namespace, LAN hosts, a cloud metadata address — and
anything feedparser turns into entries becomes readable by every peer through
`/api/news`. Placement is the access control — keep it on a private overlay
and never expose it to the public internet. On the NAS compose (a Tailscale
sidecar in kernel networking mode) set `bind_host: 127.0.0.1` in
`config.yaml` so Serve is the only way in; with `0.0.0.0` any tailnet peer
reaches the port directly.

## Protocol

    WS   /ws/news          first frame subscribes; later frames replace the set
    GET  /api/news?limit=&before=&after=
    GET  /api/feeds
    GET  /api/health
    GET  /

**Subscribe** (client → server):

    {"subscribe": [{"url": "https://feeds.example/markets.rss", "name": "Markets"}, ...]}

`url` must be http(s) and at most 2048 characters; at most 200 entries. A
frame that is not that closes the socket with code 4400. `name` is used only
when the feed is new to the pool. Feeds are deduplicated by canonical URL:
scheme and host lowercased, one trailing slash stripped (unless a query or
fragment follows the path), nothing cleverer — the same rule bdobb-v2's
`canonicalFeedUrl` applies client-side.

**Reply** (server → client, once per subscribe frame, in the frame's order):

    {"feeds": [{"id": 3, "url": "https://feeds.example/markets.rss", "title": "Markets", "favicon": "data:image/png;base64,..."}]}

`favicon` is the publisher's icon resolved against the feed's own host (each
Substack newsletter gets its own), or `null` until it resolves.

**Articles** (server → client, every other frame; the same shape on `/api/news`):

    {"id": 91, "feed_id": 3, "cursor": "...", "title": "...", "link": "...", "summary": "...",
     "source": "Markets", "author": null, "published_at": 1756800000, "sort_at": 1756800000}

Timestamps are epoch seconds. `sort_at` is clamped to the poll time so a
future-dated item cannot pin itself to the top or poison a reconnect gap
fill; `published_at` is the feed's own value.

**Paging:** `before` (and no cursor) walks newest-first; `after` walks
oldest-first and exists for a reconnect gap fill. `limit` is 1–200, default
50.

## Feed lifecycle

A feed is polled while at least one open socket names it. The count is in
memory: a socket closing releases its feeds, a feed at zero subscribers stops
polling at once, and the hourly retention sweep deletes feeds still at zero —
with their state and articles. Every feed starts disabled at boot; a client
reconnecting re-enables its own within seconds. The first successful poll of
a feed new to the pool caches its back catalogue without broadcasting it, so
adding a feed never floods the wire.

## Configuration

`config.yaml` holds exactly four keys — `retention_days`,
`default_poll_interval_s`, `max_concurrent_polls`, `bind_host` — and the
server refuses to start on any other key, naming it. A config from the
user-and-key era fails loudly rather than booting an empty pool. `${ENV}`
expansion is still honoured but nothing requires it.

Poll cadence is global: choose `default_poll_interval_s` for the pool as a
whole (90 s suits wires; 300 s suits blogs).

## Health and logging

`GET /api/health` returns `{status, version, sessions, feeds}` with the full
per-feed detail. `status` is `degraded` when an enabled feed has failed three
polls in a row; set `HEALTH_STRICT=1` to make that a 503 so the container's
`HEALTHCHECK` trips. uvicorn's access log is on; the poller's own log lines
reduce feed URLs to scheme and host.

## Upgrading from the three-key 8.0.0 build

The database migrates itself: the `users`, `subscriptions` and `filter_rules`
tables are dropped, the per-feed `group`, `title_format` and
`poll_interval_s` columns go, `articles.author` is added, and feed URLs are
canonicalised. Articles keep their history. The config does **not** migrate:
delete every key but the four above (the server names the stale ones), drop
`rss-ticker.env`, and pull `ghcr.io/artcashin/rss-feedhandler:8.0.0`. The
OpenBB Workspace widgets are gone; the consumer is bdobb-v2 v8.0.0's News
widget.

## Multi-arch builds

`make buildx` builds and pushes a `linux/amd64,linux/arm64` image to
`ghcr.io/artcashin/rss-feedhandler`. Multi-platform builds require a
`docker-container`-driver builder; the target auto-creates one named
`rss-ticker-builder` on first use.

## Development

    uv venv --python 3.12 && uv pip install -e ".[dev]"
    make test
    make lint

Docs: [base design](docs/superpowers/specs/2026-07-21-rss-news-ticker-design.md) ·
[user-agnostic rework](docs/superpowers/specs/2026-09-01-user-agnostic-rework-design.md) ·
[implementation plan](docs/superpowers/plans/2026-09-03-user-agnostic-rework.md)
