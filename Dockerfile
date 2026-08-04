FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CONFIG_PATH=/config/config.yaml \
    DB_PATH=/data/ticker.db \
    PORT=8088 \
    # Consumed by docker-entrypoint.sh: ensure /data exists and is owned by the
    # non-root app user before we drop privileges. /config is a read-only mount,
    # so it is deliberately NOT listed here.
    APP_DIRS=/data \
    APP_USER=ticker

# gosu lets the root entrypoint chown the fresh bind mount, then drop to `ticker`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 ticker \
    && mkdir -p /data /config \
    && chown -R ticker:ticker /data /config

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# NOTE: no `USER ticker` here on purpose. The entrypoint starts as root so it can
# fix ownership of a freshly-created bind mount, then execs the app as `ticker`.
EXPOSE 8088
VOLUME ["/data"]

# Non-200 (incl. the 503 that HEALTH_STRICT returns when degraded) and any
# connection error map to exit 1 = unhealthy, cleanly, without a traceback --
# 503 is an expected recurring state under HEALTH_STRICT.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python","-c","import os,sys,urllib.request as u\ntry:\n r=u.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8088')+'/api/health',timeout=4)\n sys.exit(0 if r.status==200 else 1)\nexcept Exception:\n sys.exit(1)"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["rss-ticker"]
