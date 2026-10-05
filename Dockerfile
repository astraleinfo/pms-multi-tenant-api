# syntax=docker/dockerfile:1.7
#
# One image runs the API, the Celery worker, beat and Flower; which of them a
# container is decides on its command line (see docker-compose.prod.yml).
#
#   docker build -t pms-api:$(git rev-parse --short HEAD) .

ARG PYTHON_VERSION=3.12

# ----------------------------------------------------------------- builder
# Compilers and pip live here and nowhere else. The runtime stage copies the
# finished virtualenv across and never sees a build tool.
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Dependencies first, against an empty `app` package, so this layer is rebuilt
# when pyproject.toml changes and not on every edit to the source. The stub is
# uninstalled straight after: the runtime imports `app` from /srv, not from
# site-packages. `[monitor]` brings Flower.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir app && touch app/__init__.py \
 && pip install ".[monitor]" \
 && pip uninstall -y pms-multi-tenant-api

# ----------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="pms-multi-tenant-api" \
      org.opencontainers.image.description="Multi-tenant patient management API" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/srv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    # Read by uvicorn itself. Trusting every hop is safe only because nothing
    # but the reverse proxy can reach port 8000, and the proxy overwrites
    # X-Forwarded-For rather than appending to it. Narrow it (an IP or CIDR) if
    # the API is ever reachable any other way: rate limiting and the audit
    # trail both key on the address this yields.
    FORWARDED_ALLOW_IPS="*"

# tini is PID 1: it forwards SIGTERM so uvicorn and Celery shut down warm, and
# reaps the worker's child processes.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --user-group --home-dir /srv --no-create-home pms

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
# Owned by root and read-only to the process: the API writes nothing to disk.
# Beat and Flower keep their state under /tmp.
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
COPY app ./app

USER pms

EXPOSE 8000

# Meaningful for the API container only; the worker, beat and Flower override
# it in compose. No curl in the image — the interpreter is already here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

ENTRYPOINT ["tini", "--"]

# Exactly one worker. The queue's event broker (app/core/events.py) and the
# rate limiter are both in-process, so a second worker would split clinics'
# live queues across processes. Scale out only after moving both to Redis.
#
# --no-access-log: RequestContextMiddleware already logs every request, with
# tenant and actor, by path — uvicorn's own line would add the query string,
# and the queue socket carries its access token there.
#
# --timeout-keep-alive outlives nginx's upstream keepalive_timeout (60s), so it
# is always nginx that closes an idle connection, never uvicorn mid-reuse.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", \
     "--no-access-log", \
     "--no-server-header", \
     "--timeout-keep-alive", "75", \
     "--timeout-graceful-shutdown", "30"]
