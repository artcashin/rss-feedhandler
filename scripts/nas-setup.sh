#!/bin/sh
# rss-ticker — NAS setup (run over SSH).
#
# Prepares the tailnet deployment in docker-compose.nas.yml: a Tailscale Serve
# sidecar, no published ports. This server has no users, keys or tokens --
# every endpoint is open, so Tailscale Serve being the only way in is the
# whole access control.
#
# The LAN mode (a port published to the LAN, with per-user tokens and an
# OpenBB Workspace manifest key) is gone: with no authentication a published
# port would hand the server to anything on the LAN. So is the TAILNET=1
# switch -- the tailnet deployment is the only mode.
#
# Must run as root. On most NAS builds that means sudo, and because sudo resets the
# environment, pass any overrides through `env`:
#   sudo env BASE=/path/to/rss-ticker sh nas-setup.sh
#
# Idempotent: safe to re-run. It creates the folders, writes serve.json, a
# ts.env placeholder and a config.yaml (ONCE — never overwrites an existing
# config.yaml or ts.env, so your settings and auth key survive a re-run), and
# fixes the config's permissions for the container's uid 10001. It does not
# start anything: copy docker-compose.nas.yml into BASE and `docker compose up`.
set -eu

BASE="${BASE:-/share/Container/rss-ticker}"

echo "==> rss-ticker NAS setup"
echo "    base dir : $BASE"
echo

# --- 0. must be root (NAS builds need sudo for docker + share writes) ----------
if [ "$(id -u)" -ne 0 ]; then
  echo "!! not root — writes under /share will fail on this NAS." >&2
  echo "   re-run with:" >&2
  echo "     sudo env BASE=$BASE sh $0" >&2
  exit 1
fi

# --- 1. folders ------------------------------------------------------------
if [ ! -d /share ]; then
  echo "!! /share does not exist — set BASE to a real path on your NAS." >&2
  exit 1
fi
mkdir -p "$BASE/ts-state" "$BASE/ts-config" "$BASE/config" "$BASE/data"
echo "==> folders ready"

# --- 2. Tailscale Serve: tailnet HTTPS -> the ticker on loopback -----------
cat > "$BASE/ts-config/serve.json" <<'JSON'
{
  "TCP": { "443": { "HTTPS": true } },
  "Web": {
    "${TS_CERT_DOMAIN}:443": {
      "Handlers": { "/": { "Proxy": "http://127.0.0.1:8088" } }
    }
  }
}
JSON

if [ ! -f "$BASE/ts.env" ]; then
  umask 077
  printf 'TS_AUTHKEY=\n' > "$BASE/ts.env"
  echo "WROTE $BASE/ts.env -- put your Tailscale auth key in it before starting."
fi
chmod 600 "$BASE/ts.env"

# --- 3. config.yaml (only if absent — never clobber live settings) ---------
if [ -f "$BASE/config/config.yaml" ]; then
  echo "==> $BASE/config/config.yaml already exists — keeping it"
else
  cat > "$BASE/config/config.yaml" <<'YAML'
# rss-ticker: a user-agnostic shared feed pool. Clients subscribe feeds over
# the websocket; nothing here names a feed, a user or a key. The server
# refuses to start on any key but these four.

# Articles a feed no longer lists are kept this long after they were last seen.
retention_days: 7

# Every feed polls at this interval. Be judicious: publishers monitor, and an
# over-eager poller gets throttled or disconnected. 90 suits a pool of wires;
# 300 suits blogs and newsletters.
default_poll_interval_s: 300

max_concurrent_polls: 8

# Loopback: the ticker shares the Tailscale sidecar's network namespace, so
# Serve is the only way in. With 0.0.0.0 every tailnet peer reaches the port
# directly -- and this server has no authentication.
bind_host: 127.0.0.1
YAML
  echo "WROTE $BASE/config/config.yaml"
fi
# The container runs as uid 10001 and mounts /config read-only, so nothing
# can fix this at runtime -- if umask 077 above left it at 0600, the
# container can never read it and restart-loops on first boot.
chmod 644 "$BASE/config/config.yaml"

echo
echo "==> Done. Copy docker-compose.nas.yml into $BASE, put your Tailscale"
echo "    auth key in $BASE/ts.env, then:"
echo "    cd $BASE && docker compose -f docker-compose.nas.yml up -d"
echo "    Point a bdobb-v2 News widget's Ticker URL at https://<ts-hostname>.<your-tailnet>.ts.net"
