# rss-ticker

*Companion code for Adventures in OpenBB, Ep. 9: "All the News That Fits, We Print."*

Real-time RSS news ticker server with an OpenBB Workspace widget.

Polls RSS feeds, pushes new articles over WebSocket, caches history in SQLite for
cursor-paged scrollback, and serves a Bloomberg-style news window that OpenBB
Workspace embeds as an iframe widget.

## Quick start

    cp config.example.yaml config.yaml   # edit feeds and public_base_url
    gen() { python -c "import secrets; print(secrets.token_urlsafe(32))"; }
    export TICKER_ADMIN_KEY=$(gen)       # writes; operator only
    export TICKER_MANIFEST_KEY=$(gen)    # widgets.json; goes into OpenBB
    export TICKER_TOKEN_ART=$(gen)       # one per user id in config.yaml
    docker compose up -d

Three kinds of secret, and they are not interchangeable:

| Secret | Opens | Who holds it |
|---|---|---|
| `admin_key` | writes on `/api/feeds`, the per-feed detail on `/api/health` | you |
| `manifest_key` | `/widgets.json`, and nothing else | pasted into OpenBB Workspace |
| per-user `token` | that user's reads | published in the widget's iframe URL |

`manifest_key` is separate from `admin_key` on purpose: it is the value that leaves your
control and lives in a third-party UI, so leaking it must not confer write access.
Startup fails if the two are equal.

**This build assumes a single trust domain — treat it as single-user (or a group that
mutually trusts each other).** This concern is about token mode; under `tailscale_auth`
`/widgets.json` publishes no tokens at all (see Tailscale mode below). In token mode,
`/widgets.json` embeds *every* configured user's `token`
inside the endpoint URLs it publishes, and everyone who adds the backend to OpenBB
Workspace must hold `manifest_key` to fetch that manifest. So anyone with `manifest_key`
can read the manifest and extract every user's token — the per-user tokens isolate a
user's reads from an outside attacker, not from each other. Genuine per-user isolation
across mutually-distrusting users would need per-user manifests filtered by the presented
key, which this build does not do. Configure one user, or only users who may see each
other's feeds.

In token mode, every user in `config.yaml` needs a `token` of at least 32 characters. A
user without one is a startup error, not an open account — the container exits and
`docker compose logs` names the user. Under `tailscale_auth` this is scoped differently:
a user needs a `token`, a `tailscale_login`, or both, and only a user with neither is a
startup error (see Tailscale mode below).

`public_base_url` must be the URL OpenBB Workspace can reach this server at. It is baked
into `widgets.json` at startup; if it is wrong the widget renders a blank frame.
`GET /api/health` echoes the value in use, without a credential.

## Deployment requirements

This server authenticates with bearer tokens carried in the URL. That is forced by the
embedding — OpenBB attaches no auth to an iframe document load, browsers cannot set
headers on a WebSocket, and third-party cookies are blocked in cross-site iframes. See
the design spec's amendment for the full reasoning.

Three consequences are operational, not architectural, and are **your** responsibility:

- **TLS is mandatory.** Terminate it at a reverse proxy in front of the container and set
  `public_base_url` to the `https://` URL so `widgets.json` publishes `https`/`wss`
  endpoints. Over plain HTTP every token is readable by anyone on the path.
  `docker-compose.yml` binds the plaintext port to `127.0.0.1` only, so the proxy is the
  only way in — it is not optional, whatever a client's own settings say. If you need this
  server reachable directly from other machines on your LAN without standing up a proxy,
  put the host on a private overlay network instead (Tailscale, WireGuard) and set
  `bind_host` in `config.yaml` to that interface's address, never to `0.0.0.0`.
- **A private network is strongly recommended.** Tailscale, WireGuard, or an equivalent
  overlay makes the token a second layer rather than the only one.
- **Request logging is the proxy's job, and it must omit query strings.** This server's
  uvicorn access log is off — the request line contains tokens, and a log filter that
  silently stopped matching after a dependency bump would fail open. Configure the proxy
  to log the path only (nginx: `$uri` rather than `$request`; Caddy: strip `uri.query`).
  A proxy left on its default combined-log format writes every user token to disk on
  every request. This is about the *access* log only: the container's application log
  (poll activity, reconciliation, errors) still writes to stdout as usual, with feed
  URLs reduced to scheme and host wherever they appear, the same as `/api/feeds`.

Not built, and deliberately: a login page, server-side sessions, mTLS, and rate limiting.
Rate limiting is noted as future hardening.

## Tailscale mode (no tokens in URLs)

Behind Tailscale Serve the ticker can authenticate you by the identity Serve
injects, so no per-user token appears in any URL — not in `widgets.json`, not
in the iframe address, not in a proxy log.

    tailscale_auth: true
    bind_host: 127.0.0.1
    users:
      - id: art
        tailscale_login: you@github

`docker-compose.nas.yml` runs this: a `tailscale/tailscale` sidecar owns the
network namespace, the ticker joins it and publishes **no ports**, and Serve
terminates TLS with a real Let's Encrypt certificate.

**Why the loopback bind is mandatory.** Trusting a proxy header is safe only
while that proxy is the only way in. Sharing the sidecar's namespace, a
`0.0.0.0` bind would expose the port to every tailnet peer *around* Serve,
where the header is trivially forged. The app refuses to start in that
combination.

**Two caveats.** Every device that views the dashboard must be on your tailnet.
And Tailscale omits identity headers for **tagged** devices, so tagging a
viewing device silently removes its access.

Token mode remains the default and is unchanged for non-Tailscale deployments.

## OpenBB setup

Add a custom backend in OpenBB Workspace pointing at `<public_base_url>`, and set its API
key to the value of **`TICKER_MANIFEST_KEY`** — not the admin key. Workspace sends it as
`X-API-KEY`, which `/widgets.json` requires. Then drop the "News window" or "News rail"
widget onto a dashboard; the per-user token travels inside the widget's published
endpoint URL. Under `tailscale_auth` (see Tailscale mode above) leave the API key blank —
the caller's Serve identity substitutes for it — and no token appears in the endpoint URL.

`manifest_key` unlocks the whole manifest, which carries every user's token, so hand it
only to Workspace accounts that are allowed to see every configured user's feeds — see the
single-trust-domain note above.

## Endpoints

| Path | Credential |
|---|---|
| `GET /` | none |
| `GET /widgets.json` | `X-API-KEY: <manifest_key>` |
| `GET /widget?user=&token=` | user token, or admin key |
| `GET /api/news?user=&token=&limit=&before=&after=` | user token, or admin key |
| `WS /ws/news?user=&token=` | user token |
| `GET /api/feeds?user=&token=` | user token → host-only URLs; admin key → full URLs |
| `POST`, `DELETE /api/feeds` | `X-Admin-Key` |
| `GET /api/health` | none → status, version, base URL; `X-Admin-Key` → feed detail |

Under `tailscale_auth` (see Tailscale mode above) a matching Tailscale Serve identity is
accepted in place of the user token on the read endpoints and in place of `X-API-KEY` on
`/widgets.json`; writes still require `X-Admin-Key`.

The token may be sent as `Authorization: Bearer <token>` instead of the `token` query
param anywhere the query param is accepted. The WebSocket and the iframe document load
have no such option, which is why the query param exists at all.

A rejected read is always `401 {"detail": "Invalid credentials"}` — the same response for
an unknown user as for a wrong token, so user ids cannot be enumerated. A request with no
`user` param at all is `422`.

## Upgrading an existing deployment

The database migrates itself: a `token` column is added to `users`, NULL for every
existing row, and a NULL token authenticates nothing. `config.yaml` does **not** migrate
itself, and the server will not start until it is complete.

1. Generate the secrets:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"` — one `manifest_key`,
   one token per user.
2. Add `manifest_key: ${TICKER_MANIFEST_KEY}` at the top level and
   `token: ${TICKER_TOKEN_<USER>}` under each user in `config.yaml`, then export the
   variables. Skipping either means the container fails to start with
   `manifest_key is required` or `user 'art' has no token`. Those failures are
   intentional and are the only thing standing between an upgrade and an open server.
3. `docker compose up -d`. Boot reconciliation writes each configured token onto the
   existing user row. Removing a user from `config.yaml` and restarting revokes their
   token on that restart — their row, feeds, subscriptions, and articles are kept (this
   server never deletes data), but every read they had closes: `/api/news`, `/api/feeds`,
   `/widget`, and `/ws/news` all start rejecting their old token, and the revocation is
   logged by user id. Re-adding them to `config.yaml` and restarting restores access. A
   `config.yaml` with no users at all is not treated as "delete everyone" — it revokes
   nobody, on the assumption that an empty user list is a misread file, not an
   instruction to lock out the whole database.
4. In OpenBB Workspace, set the backend's API key to `TICKER_MANIFEST_KEY` and **re-add
   the widget** — its endpoint URL changed and the old saved widget has no token in it.

Rotating a token is the same loop: change the variable, restart, re-add the widget. The
previous token stops working on restart — including when a user switches to
`tailscale_auth` and drops their `token:` line entirely: reconcile clears the stored
token explicitly rather than leaving the old one live. Rotating `manifest_key` means
rotating every user token too, since the manifest is what published them.

**Removing a feed from `config.yaml` does not stop polling it.** Reconciliation is
additive: the feed keeps its subscription and stays enabled, and the same applies to
per-feed values (`title_format`, `poll_interval_s`, `group`) — deleting a line keeps
the stored value. Boot logs a warning naming every enabled feed absent from config.
To actually stop a feed, unsubscribe it through the admin API:

    curl -X DELETE -H "X-Admin-Key: $TICKER_ADMIN_KEY" \
      "http://localhost:8088/api/feeds/<id>?user=<user>"

(the id is in the boot warning, or in `/api/health` with the admin key). The feed's
row and cached articles are kept — only the polling stops.

## Error notifications (NAS container manager)

Container-level failures — the container exits, restart-loops, or goes
`unhealthy` — are surfaced by your NAS container manager's own event
notifications; enable a rule in its notification center. A missing
config mount now exits with one clean line (not a traceback), so it reads as a
clean "container exited" event.

By default a *degraded* feed state (one feed failing repeatedly while others are
fine) keeps the container **healthy** and `/api/health` returns 200 — a transient
feed blip must not restart the container. Set `HEALTH_STRICT=1` to flip that: when
degraded, `/api/health` returns 503, the container's `HEALTHCHECK` trips, and
the container manager reports it `unhealthy`. The response body is unchanged;
only the status code moves. Leave it off unless you specifically want feed
outages to page you through your NAS's notifications.

## Multi-arch builds

`make buildx` builds and pushes a `linux/amd64,linux/arm64` image. Multi-platform
builds require a `docker-container`-driver builder (the default `docker` driver
does not support them). The target auto-creates one named `rss-ticker-builder`
on first use, so no manual setup is needed.

## Development

    uv venv --python 3.12 && uv pip install -e ".[dev]"
    make test
    make lint

Docs: [design spec](docs/superpowers/specs/2026-07-21-rss-news-ticker-design.md) ·
[implementation plan](docs/superpowers/plans/2026-07-21-rss-news-ticker.md)
