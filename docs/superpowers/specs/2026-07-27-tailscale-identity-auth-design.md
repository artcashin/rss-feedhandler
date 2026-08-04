# Tailscale identity auth, and the ticker on the NAS

**Status:** approved (design), pending implementation
**Date:** 2026-07-27
**Amends:** `2026-07-21-rss-news-ticker-design.md` — specifically its
"Amendment: untrusted-network authentication (2026-07-21)", whose per-user
URL tokens this design supersedes *on a tailnet deployment* (and only there).

## Why

The ticker currently authenticates with per-user secrets carried **in the URL**
(`/widget?user=art&token=…`). The 2026-07-21 amendment adopted that reluctantly:
OpenBB attaches no auth to an iframe document load, a browser cannot set headers
on a `WebSocket`, and third-party cookies are blocked in cross-site iframes. The
token-in-URL was what remained after every better option was removed, and the
spec records its costs honestly — the token sits in `widgets.json`, in OpenBB's
saved dashboard config, in DevTools, and in any proxy log that records a request
line.

Running behind **Tailscale Serve** removes the constraint that forced it.
Serve injects a verified caller identity as a request header on *every* proxied
request — including the WebSocket upgrade — so the application can know who is
calling without a credential in the URL at all.

The existing NAS deployment path (`docker-compose.nas.yml`, `scripts/nas-setup.sh`,
image `ghcr.io/artcashin/rss-ticker:0.3.0`) publishes **plain HTTP on the LAN**
(`8088:8088`) with those tokens in the URL. Its own comment concedes it needs "a
TLS reverse proxy in front if it is ever reachable beyond" a trusted network.
This design replaces that exposure with the same pattern already proven by another sidecar-fronted service on this NAS
on this NAS: a Tailscale sidecar, TLS from Serve, nothing on the LAN.

## Verified facts (measured 2026-07-27, not assumed)

A throwaway rig — `tailscale/tailscale:v1.98.9` sidecar plus a header-echoing
container, mirroring that service's compose exactly — was run on the NAS and driven
from the Mac. Results:

| Check | Result |
|---|---|
| Plain `GET` via Serve | `Tailscale-User-Login: you@github`, `Tailscale-User-Name: Your Name`, `Tailscale-User-Profile-Pic: …` |
| **WebSocket upgrade via Serve** | **the same identity headers arrive** |
| Direct request to the node's tailnet IP, bypassing Serve | a client-supplied `Tailscale-User-Login: attacker@evil.example` arrived **verbatim**; no real identity |
| Client-forged header sent *through* Serve | **overwritten** with the real `you@github` |

Supporting details, all load-bearing:

- Serve also adds a `Tailscale-Headers-Info` marker and `X-Forwarded-For` /
  `-Host` / `-Proto`.
- Serve is **tailnet-only** here; identity headers are documented as absent on
  Funnel traffic, and this design never enables Funnel.
- **Identity headers are not populated for traffic originating from *tagged*
  devices.** Every node in this tailnet is currently owned by `artcashin@` and
  untagged. Tagging a viewing device would silently break its access.
- The login is **`you@github`** — a GitHub OIDC identity, *not* an email
  address. Config must carry that literal string.
- The first TLS request to a new Serve hostname takes ~19s while the Let's
  Encrypt certificate provisions, and may return `000` before succeeding. Retry;
  it is not an error.

### The security precondition this creates

Trusting a proxy-supplied header is safe **only** while the proxy is the sole
path to the application. Test 3 above is the proof: reachable around Serve, the
header is trivially forged, and identity auth becomes an open door rather than a
lock.

Therefore the application **must bind loopback only**. Under
`network_mode: service:tailscale` the ticker shares the sidecar's network
namespace, where the tailnet IP is a local interface — so binding `0.0.0.0`
would publish port 8088 to every tailnet peer, *around* Serve. The other sidecar deployment solves this
with `HTTP_ADDR=127.0.0.1`; the ticker currently hardcodes `host="0.0.0.0"`
(`main.py:120`) and must become configurable.

This is not advisory. The application will **refuse to start** when
`tailscale_auth` is enabled and the bind host is not loopback.

## Design

### Config surface

```yaml
public_base_url: https://rss-ticker.your-tailnet.ts.net

# Trust the identity headers Tailscale Serve injects. Only sound when the app
# is unreachable except through Serve, which is why enabling this forces a
# loopback bind (startup fails otherwise).
tailscale_auth: true
bind_host: 127.0.0.1        # default "0.0.0.0"; required loopback above

admin_key: ${TICKER_ADMIN_KEY}   # still required — gates writes
# manifest_key: optional when tailscale_auth is on

users:
  - id: art
    name: Art
    tailscale_login: you@github   # the literal Serve identity
    # token: optional when tailscale_auth is on
    feeds: [...]
```

Rules enforced in `load_config`:

- `tailscale_auth: true` **and** a non-loopback `bind_host` → `ConfigError`.
  Loopback means `127.0.0.1`, `::1`, or `localhost`.
- `tailscale_auth: true` → `manifest_key` and per-user `token` become optional.
  All existing validation (min length, uniqueness, `manifest_key != admin_key`)
  still applies to whatever *is* supplied.
- `tailscale_auth: true` and a user has neither `tailscale_login` nor `token` →
  `ConfigError` naming that user. A user reachable by nothing is a
  configuration mistake, not a silently closed account.
- Duplicate `tailscale_login` across users → `ConfigError`. Two users answering
  to one identity is ambiguous.
- `admin_key` remains **required** in all modes.
- `tailscale_auth` defaults to `false`, so every existing deployment and the
  entire current test suite are unaffected.

### Authentication resolution

`require_user_token` gains one branch. Order, first match wins:

1. **Admin key** — unchanged master credential (an admin key naming a
   nonexistent user is still `401`, not a bypass).
2. **Tailscale identity** — only when `tailscale_auth` is on: read
   `Tailscale-User-Login`, map it to a configured user, and require that user to
   equal the requested `user` param.
3. **Token** — the existing constant-work `token_ok` path, unchanged.

Every failure is the existing indistinguishable `401 {"detail": "Invalid
credentials"}`; a structurally missing `user` stays `422`. An unknown identity,
an identity that maps to a *different* user, and a wrong token are all the same
rejection. The identity lookup is an in-memory map built at startup, so it adds
no database work and cannot become a user-enumeration timing oracle.

`/ws/news` reads the header off the WebSocket handshake (proven to arrive) and
applies the same rule, still rejecting **before** `broadcaster.subscribe`.

`/widgets.json` accepts the identity when `tailscale_auth` is on, and still
accepts `manifest_key` when one is configured. It continues to reject the admin
key — that separation is deliberate and unchanged.

### Endpoint matrix under `tailscale_auth: true`

| Endpoint | Accepted credential |
|---|---|
| `GET /` | none |
| `GET /widgets.json` | Serve identity, or `X-API-KEY: <manifest_key>` if set |
| `GET /widget` | Serve identity (matching `user`), or token, or admin key |
| `GET /api/news` | same |
| `GET /api/feeds` | same; URLs still redacted for non-admin callers |
| `WS /ws/news` | Serve identity (matching `user`), or token |
| `POST`/`DELETE /api/feeds` | `X-Admin-Key` only — writes keep their second factor |
| `GET /api/health` | none; feed detail still requires the admin key |

### Manifest and widget

When `tailscale_auth` is on, `render_widgets` publishes **token-free** endpoints:

```
https://rss-ticker.your-tailnet.ts.net/widget?user=art
```

This is the substantive security win: no token in the iframe URL, in OpenBB's
saved dashboard config, in DevTools, or in any proxy access log. The widget's
own `fetch`/WebSocket calls are same-origin through Serve, so each carries the
identity automatically. The widget must omit an empty `token=` parameter rather
than sending a blank one.

The `Referrer-Policy: no-referrer` header, the `no-referrer` meta, and
`rel="noopener noreferrer"` on outbound links all stay — they cost nothing and
still matter for the token mode.

## Deployment

Mirrors the other sidecar deployments on this NAS, which is the proven pattern.

```
/path/on/nas/rss-ticker/
  docker-compose.yml     tailscale sidecar + ticker
  ts.env                 TS_AUTHKEY, chmod 600, never in git
  ts-state/              tailnet node identity, persistent
  ts-config/serve.json   TCP 443 HTTPS -> proxy 127.0.0.1:8088
  config/config.yaml     feeds, users, admin_key
  data/                  SQLite
```

- Sidecar `tailscale/tailscale:v1.98.9` (already cached on the NAS),
  `hostname: rss-ticker`, `TS_SERVE_CONFIG=/config/serve.json`,
  `/dev/net/tun` + `NET_ADMIN`/`NET_RAW`.
- Ticker joins with `network_mode: service:tailscale` and publishes **no ports
  at all** — the `8088:8088` LAN mapping is removed, not merely rebound.
- Image `ghcr.io/artcashin/rss-ticker:0.4.0`, built and pushed from the Mac with
  the existing multi-arch Makefile target. The NAS is `x86_64`; it pulls, it
  does not build. This deliberately avoids depending on that other service, which is
  currently returning 502.
- Result: **https://rss-ticker.your-tailnet.ts.net**, real Let's Encrypt
  certificate, reachable only from the tailnet.
- The auth key is a normal (non-ephemeral) key so the node identity survives
  restarts, written to `ts.env` by the operator and never echoed.

`scripts/nas-setup.sh` gains a tailnet mode that provisions this layout,
generates `admin_key`, writes a `tailscale_auth` config, and leaves `ts.env` for
the operator to fill. The existing LAN mode stays for non-Tailscale hosts.

**Not** added to the offsite backup (`the-offsite-backup-job` `jobs.conf`): the
SQLite database is re-derivable news, and `config.yaml`'s shape lives in git.
Adding it would grow the nightly upload for nothing.

## Code changes

| File | Change |
|---|---|
| `config.py` | `Config.tailscale_auth`, `Config.bind_host`, `UserConfig.tailscale_login`; the validation rules above |
| `api.py` | identity branch in `require_user_token`; identity accepted by `require_manifest_key`; identity on the `/ws/news` handshake |
| `widgets.py` | omit `&token=` when `tailscale_auth` |
| `main.py` | bind `config.bind_host` instead of the hardcoded `0.0.0.0` |
| `static/widget.html` | omit an empty `token` param |
| `docker-compose.nas.yml` | sidecar + no published ports + loopback bind |
| `scripts/nas-setup.sh` | tailnet mode |
| `config.example.yaml`, `README.md` | document the mode, its precondition, and the tagged-device caveat |

## Testing

Baseline is **381 passing**; the count only goes up, and `tailscale_auth`
defaults off so no existing test changes meaning.

- **Config:** each new validation rule, especially `tailscale_auth` + non-loopback
  `bind_host` → `ConfigError` (the fail-closed guard), and duplicate
  `tailscale_login`.
- **Identity auth:** a matching identity authenticates; an identity mapped to a
  *different* user is `401`; an unknown identity is `401`; the header is ignored
  entirely when `tailscale_auth` is off (so a non-Tailscale deployment cannot be
  spoofed by sending the header).
- **That last test is the security-critical one** and must be mutation-verified:
  it has to fail if the code ever honours the header without the flag.
- **WebSocket:** identity authenticates the socket; a mismatched identity closes
  `4401` without registering a subscriber.
- **Manifest:** `tailscale_auth` publishes token-free endpoints; `/widgets.json`
  accepts identity; the admin key still does not open it.
- **Regression:** the whole existing token-mode suite stays green untouched.

Per this codebase's standing rule, every new test must be shown to fail against
the unfixed code.

## Out of scope

- **Funnel.** Public exposure would strip identity headers and defeat the design.
- **Tailscale ACL-based authorization** (per-endpoint grants). The tailnet is the
  perimeter; the app maps identity to a user and stops there.
- **Multi-tenant identity.** One tailnet, one owner, untagged devices. The
  existing single-trust-domain note in the README continues to apply.
- **Replacing token mode.** It remains the supported path for non-Tailscale
  deployments and keeps its tests.

## Honest costs and risks

- **Any device viewing the dashboard must be on the tailnet.** This is inherent,
  not an implementation limit. A phone off the tailnet gets nothing.
- **Tagging a device silently breaks its access** — Serve omits identity headers
  for tagged originators. Documented in the README.
- **The loopback bind is the whole security model.** A future change that
  publishes a port, or a reverse proxy placed in front that does not overwrite
  `Tailscale-User-*`, reopens forgery. The startup guard defends the first case;
  the README must warn about the second.
- **A second header-injecting hop would be trusted blindly.** The app trusts the
  header because Serve is the only ingress; nothing in the app verifies that.
- **Identity is coarse.** `you@github` identifies the tailnet account, not
  a session or a device. Anyone on a device signed into that account is that
  user — which is exactly the trust boundary a personal tailnet already implies.
