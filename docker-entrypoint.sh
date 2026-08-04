#!/bin/sh
# Generic self-provisioning entrypoint.
#
# Makes the image portable across hosts (NAS container managers, plain Docker,
# compose) with ZERO host pre-setup: you can bind-mount empty or not-yet-created
# paths and the container brings its own directory structure up to spec on boot.
#
# On NAS container managers, dockerd auto-creates a missing bind-mount SOURCE dir as
# root:root. This script then (1) creates any nested structure the app needs and
# (2) for a non-root image, takes ownership of those dirs and drops back to the
# app user -- which is what lets a non-root container write to a fresh, root-owned
# /share/Container/<app>/... mount without a manual `chmod 777` on the NAS.
#
# Configure via env (set in the Dockerfile):
#   APP_DIRS  space-separated dirs to `mkdir -p` (e.g. "/data /var/lib/app")
#   APP_USER  if set AND we are root: chown APP_DIRS to it, then exec as it
#             (needs gosu or su-exec in the image; falls back to plain exec).
# Read-only mounts (e.g. /config:ro) must NOT be listed in APP_DIRS -- chown
# would fail on them; leave them out and the app just reads them.
set -eu

for d in ${APP_DIRS:-}; do
  mkdir -p "$d"
done

if [ -n "${APP_USER:-}" ] && [ "$(id -u)" = "0" ]; then
  for d in ${APP_DIRS:-}; do
    chown -R "$APP_USER" "$d" 2>/dev/null || true
  done
  if command -v gosu >/dev/null 2>&1; then
    exec gosu "$APP_USER" "$@"
  elif command -v su-exec >/dev/null 2>&1; then
    exec su-exec "$APP_USER" "$@"
  fi
  # No privilege-drop tool available: fall through and run as root rather than fail.
fi

exec "$@"
