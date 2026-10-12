.PHONY: help install db redis up down migrate seed run worker beat flower test lint fmt reset check

VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## create the virtualenv and install everything
	python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e ".[dev]"

db:  ## start PostgreSQL (docker) and create both roles
	docker compose -f docker-compose.dev.yml up -d db

redis:  ## start Redis (docker) — the Celery broker and the outbox nudge
	docker compose -f docker-compose.dev.yml up -d redis

migrate:  ## apply migrations
	$(VENV)/bin/alembic upgrade head

seed:  ## load the demo clinics
	$(PY) scripts/seed.py

run:  ## serve on http://localhost:8000
	$(VENV)/bin/uvicorn app.main:app --reload --port 8000

# The three background processes, for when the API is run on the host rather
# than in a container. `docker compose -f docker-compose.dev.yml up` starts all three itself.
worker: redis  ## run a Celery worker (-E feeds the monitor)
	$(VENV)/bin/celery -A app.worker.celery_app worker -Q pms --loglevel=INFO -E

beat: redis  ## run the scheduler — exactly one of these, ever
	$(VENV)/bin/celery -A app.worker.celery_app beat --loglevel=INFO

# Bound to the loopback, like the database and the broker: the console reaches
# it through the API, and nothing else has any business reaching it at all. Set
# FLOWER_URL=http://127.0.0.1:5555 and a matching FLOWER_BASIC_AUTH in .env, or
# the API will keep looking for the container by name. Needs `.[monitor]`.
flower: redis  ## run the job monitor on 127.0.0.1:5555
	$(VENV)/bin/celery -A app.worker.celery_app flower \
	  --address=127.0.0.1 --port=5555 \
	  --url_prefix=api/v1/platform/jobs/flower \
	  --basic_auth=$${FLOWER_BASIC_AUTH:-pms-console:dev-only-flower-password} \
	  --max_tasks=10000

test:  ## run the suite against the test database
	$(PY) -m pytest -q

lint:  ## ruff
	$(VENV)/bin/ruff check .

fmt:  ## ruff --fix
	$(VENV)/bin/ruff check --fix .

check: lint test  ## everything CI runs

reset: migrate seed  ## rebuild the demo database from scratch

up: db migrate seed run  ## the whole thing, from nothing
