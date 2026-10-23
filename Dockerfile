FROM python:3.12

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/srv \
    PIP_NO_CACHE_DIR=1 \
    FORWARDED_ALLOW_IPS="*"

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# Install dependencies first so this layer is cached between code changes
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py \
 && pip install ".[monitor]" \
 && pip uninstall -y pms-multi-tenant-api \
 && rm -rf app

COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
COPY app ./app

EXPOSE 8000

# Keep exactly one worker: the event broker and rate limiter are in-process
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--no-access-log"]

