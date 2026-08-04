#!/bin/sh
# rss-ticker — NAS setup (run over SSH).
#
# Must run as root. On most NAS builds that means sudo, and because sudo resets the
# environment, pass any overrides through `env`:
#   sudo env PUBLIC_HOST=<NAS-IP> HEALTH_STRICT=1 sh nas-setup.sh
#
# Idempotent: safe to re-run. It creates the folders, generates secrets and a
# config.yaml (ONCE — never overwrites an existing one, so your tokens survive
# a re-run), fixes permissions for the container's uid 10001, and starts the
# container. If the docker CLI isn't reachable over SSH it writes a
# docker-compose.yml and prints the container-manager steps instead.
#
# Override any of these by exporting them before running, e.g.
#   PUBLIC_HOST=192.168.1.50 PORT=9000 sh nas-setup.sh
set -eu

BASE="${BASE:-/share/Container/rss-ticker}"
IMAGE="${IMAGE:-ghcr.io/artcashin/rss-ticker:9.0.0}"
PORT="${PORT:-8088}"
NAME="${NAME:-rss-ticker}"
USER_ID="${USER_ID:-art}"
USER_NAME="${USER_NAME:-Art}"
# HEALTH_STRICT=1 makes a degraded feed state mark the container unhealthy, so
# the NAS container manager can surface it in its notification center. Off by default.
HEALTH_STRICT="${HEALTH_STRICT:-}"
# TAILNET=1 switches to the tailnet deployment (docker-compose.nas.yml): a
# Tailscale Serve sidecar, no published ports, identity-based auth instead of
# per-user tokens. Off by default -- the LAN mode below is unchanged.
TAILNET="${TAILNET:-0}"
TS_HOSTNAME="${TS_HOSTNAME:-rss-ticker}"
TS_TAILNET_DOMAIN="${TS_TAILNET_DOMAIN:-your-tailnet.ts.net}"

CONFIG_DIR="$BASE/config"
DATA_DIR="$BASE/data"
CONFIG="$CONFIG_DIR/config.yaml"

# --- secret generation (portable; no python/openssl needed) ----------------
# tr's SIGPIPE when head closes the pipe is swallowed by the pipeline exit
# status (head's 0), so this is safe under `set -e` without pipefail.
gen() { tr -dc 'A-Za-z0-9_-' < /dev/urandom 2>/dev/null | head -c 43; echo; }

# --- work out how OpenBB will reach this NAS -------------------------------
if [ -z "${PUBLIC_HOST:-}" ]; then
  PUBLIC_HOST=$(ip -4 route get 1.1.1.1 2>/dev/null \
    | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
fi
[ -z "${PUBLIC_HOST:-}" ] && PUBLIC_HOST=$(hostname -i 2>/dev/null | awk '{print $1}')
[ -z "${PUBLIC_HOST:-}" ] && PUBLIC_HOST="<NAS-IP>"

echo "==> rss-ticker NAS setup"
echo "    base dir : $BASE"
echo "    image    : $IMAGE"
echo "    reach at : http://$PUBLIC_HOST:$PORT"
echo

# --- 0. must be root (NAS builds need sudo for docker + share writes) ----------
if [ "$(id -u)" -ne 0 ]; then
  echo "!! not root — docker and writes under /share will fail on this NAS." >&2
  echo "   re-run with:" >&2
  echo "     sudo env PUBLIC_HOST=$PUBLIC_HOST${HEALTH_STRICT:+ HEALTH_STRICT=$HEALTH_STRICT} sh $0" >&2
  exit 1
fi

# --- tailnet mode: prep docker-compose.nas.yml's config and exit -----------
# (the LAN steps below publish a port and are not relevant to this mode.)
if [ "$TAILNET" = "1" ]; then
  mkdir -p "$BASE/ts-state" "$BASE/ts-config" "$BASE/config" "$BASE/data"

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

  if [ ! -f "$BASE/config/config.yaml" ]; then
    ADMIN_KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    umask 077
    cat > "$BASE/config/config.yaml" <<YAML
public_base_url: https://${TS_HOSTNAME}.${TS_TAILNET_DOMAIN}

# Trust the identity Tailscale Serve injects. Sound ONLY because the ticker
# binds loopback inside the sidecar's network namespace, so Serve is the only
# way in. Startup fails if bind_host is not loopback.
tailscale_auth: true
bind_host: 127.0.0.1

admin_key: ${ADMIN_KEY}

users:
  - id: art
    name: Art
    tailscale_login: you@github
    feeds:
      - {url: "https://feeds.bloomberg.com/markets/news.rss", name: Bloomberg Markets, group: Markets}
YAML
    echo "WROTE $BASE/config/config.yaml (admin_key generated; edit feeds to taste)"
  fi
  # The container runs as uid 10001 and mounts /config read-only, so nothing
  # can fix this at runtime -- if umask 077 above left it at 0600, the
  # container can never read it and restart-loops on first boot.
  chmod 644 "$BASE/config/config.yaml"

  echo
  echo "==> Done. Copy docker-compose.nas.yml into $BASE, put your Tailscale"
  echo "    auth key in $BASE/ts.env, then:"
  echo "    cd $BASE && docker compose -f docker-compose.nas.yml up -d"
  exit 0
fi

# --- 1. folders ------------------------------------------------------------
if [ ! -d /share ]; then
  echo "!! /share does not exist — set BASE to a real path on your NAS." >&2
  exit 1
fi
mkdir -p "$CONFIG_DIR" "$DATA_DIR"
echo "==> folders ready"

# --- 2. config.yaml (only if absent — never clobber live secrets) ----------
if [ -f "$CONFIG" ]; then
  echo "==> $CONFIG already exists — keeping it (not regenerating secrets)"
else
  ADMIN_KEY=$(gen); MANIFEST_KEY=$(gen); USER_TOKEN=$(gen)
  cat > "$CONFIG" <<YAML
public_base_url: http://$PUBLIC_HOST:$PORT

# admin_key and manifest_key MUST differ (startup fails if equal).
admin_key: $ADMIN_KEY
manifest_key: $MANIFEST_KEY

retention_days: 7
default_poll_interval_s: 300
max_concurrent_polls: 8

users:
  - id: $USER_ID
    name: $USER_NAME
    token: $USER_TOKEN
    feeds:
      - {url: "https://feeds.bloomberg.com/markets/news.rss", name: Bloomberg Markets, group: Markets, poll_interval_s: 15}
      - {url: "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", name: WSJ Markets, group: Markets, poll_interval_s: 15}
      - {url: "https://markets.businessinsider.com/rss/news", name: Business Insider, group: Markets, poll_interval_s: 15}
      - {url: "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", name: NYT Business, group: Markets, poll_interval_s: 300}
      - {url: "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", name: CNBC, group: CNBC, poll_interval_s: 30}
      - {url: "https://www.kiplinger.com/feed/all", name: Kiplinger, group: "Wealth M"}
      - url: "https://bobpisani.substack.com/feed"
        name: Bob Pisani
        group: Substack
        title_format: "{title} - {author}"
      - url: "https://roninsana.substack.com/feed"
        name: Ron Insana
        group: Substack
        title_format: "{title} - {author}"
      - url: "https://peterboockvar.substack.com/feed"
        name: Peter Boockvar
        group: Substack
        title_format: "{title} - {author}"
      - url: "https://dimartinobooth.substack.com/feed"
        name: DiMartino Booth
        group: Substack
        title_format: "{title} - {author}"
    filters:
      - {pattern: nvidia, action: highlight}
YAML
  echo "==> wrote $CONFIG with fresh secrets"
fi

# --- 3. permissions (container runs as uid 10001) --------------------------
chmod 755 "$BASE" "$CONFIG_DIR"
chmod 644 "$CONFIG"
chmod 777 "$DATA_DIR"          # SQLite must be able to write here as uid 10001
echo "==> permissions set (data writable by uid 10001)"

# --- 4. launch -------------------------------------------------------------
DOCKER=""
if command -v docker >/dev/null 2>&1; then
  DOCKER=docker
else
  for p in /usr/local/bin/docker \
           /share/*/.qpkg/container-station/usr/bin/docker \
           /share/CACHEDEV*_DATA/.qpkg/container-station/usr/bin/docker; do
    [ -x "$p" ] && { DOCKER="$p"; break; }
  done
fi

# Optional HEALTH_STRICT passthrough (empty unless the caller set it).
RUN_ENV=""
ENV_YAML=""
if [ -n "$HEALTH_STRICT" ]; then
  RUN_ENV="-e HEALTH_STRICT=$HEALTH_STRICT"
  ENV_YAML="    environment:
      - HEALTH_STRICT=$HEALTH_STRICT"
  echo "==> HEALTH_STRICT=$HEALTH_STRICT (degraded feeds will mark the container unhealthy)"
fi

COMPOSE="$BASE/docker-compose.yml"
cat > "$COMPOSE" <<YAML
services:
  rss-ticker:
    image: $IMAGE
    container_name: $NAME
    restart: unless-stopped
    ports:
      - "$PORT:8088"
$ENV_YAML
    volumes:
      - $CONFIG_DIR:/config:ro
      - $DATA_DIR:/data
YAML

if [ -n "$DOCKER" ]; then
  echo "==> found docker at: $DOCKER"
  "$DOCKER" pull "$IMAGE" || echo "   (pull failed — will use the local image if present)"
  "$DOCKER" rm -f "$NAME" >/dev/null 2>&1 || true
  # shellcheck disable=SC2086  # RUN_ENV is intentionally word-split (flag or empty)
  "$DOCKER" run -d --name "$NAME" --restart unless-stopped \
    -p "$PORT:8088" \
    $RUN_ENV \
    -v "$CONFIG_DIR:/config:ro" \
    -v "$DATA_DIR:/data" \
    "$IMAGE"
  echo "==> container '$NAME' started"
  sleep 5
  echo "==> health check:"
  if command -v curl >/dev/null 2>&1; then
    curl -fs "http://127.0.0.1:$PORT/api/health" && echo || echo "   (not healthy yet — check: $DOCKER logs $NAME)"
  else
    echo "   curl not available — check the container manager's logs for '$NAME'"
  fi
else
  echo "!! docker CLI not found over SSH."
  echo "   Wrote $COMPOSE — in your NAS container manager: create an application from it,"
  echo "   paste that file's contents, and Create."
fi

echo
echo "==> Done. For OpenBB Workspace, register the backend:"
echo "    Base URL   : http://$PUBLIC_HOST:$PORT"
echo "    API key    : the manifest_key in $CONFIG"
echo "    (widget iframe URL uses the per-user token in that same file)"
echo "    See it with: cat $CONFIG"
