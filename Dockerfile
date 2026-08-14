# mikeos-builderapps-cloud control plane.
# Needs the docker CLI + compose plugin + git so the deployer can shell out
# `docker compose build/up` against the mounted host docker socket, plus openssl.
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git openssl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# The codegen agent's syntax gate runs `node --check` on every .js it writes or edits
# (phase 24 — a syntactically broken result is REJECTED, never written). Copy just the node
# binary, from the same major version the generated apps run on (their images are
# node:20-alpine), so a check is one ~30 ms subprocess instead of a container start per edit.
# python:3.12-slim is bookworm and already ships libstdc++6, which is all node needs.
# server/harness/syntax.py falls back to `docker run --rm -i node:20-alpine node --check` if
# this binary is ever missing, so an older image degrades in speed, not in correctness.
COPY --from=node:20-bookworm-slim /usr/local/bin/node /usr/local/bin/node

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./server/
COPY migrations/ ./migrations/
COPY tests/ ./tests/

EXPOSE 8000
# ONE worker, deliberately. Pipeline runs are now durable in-process background tasks
# (server.runner); their per-project workspace lock, event broker and run registry are all
# per-process, so with 2 workers a run started in worker A is invisible to worker B and an
# SSE re-attach or a concurrent update could land on the wrong process and stomp the same git
# checkout. Nothing in the request path is CPU-bound (every subprocess/HTTP call is awaited),
# so one event loop still serves health/list while a build is in flight.
# --timeout-graceful-shutdown lets an interrupted run release its DB ownership on SIGTERM so
# the next boot's sweep resumes it instantly instead of waiting out the staleness window.
CMD ["uvicorn", "server.http_server:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--timeout-graceful-shutdown", "20"]
