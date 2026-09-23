.PHONY: help install db redis up down migrate seed run worker beat flower test lint fmt reset check

VENV := .venv

ifeq ($(OS),Windows_NT)
	PY      := $(VENV)/Scripts/python.exe
	PIP     := $(VENV)/Scripts/pip.exe
	ALEMBIC := $(VENV)/Scripts/alembic.exe
	UVICORN := $(VENV)/Scripts/uvicorn.exe
	CELERY  := $(VENV)/Scripts/celery.exe
	RUFF    := $(VENV)/Scripts/ruff.exe
	# The `py` launcher, not `python` — an activated venv puts its own
	# Scripts dir on PATH, so `python -m venv .venv` would resolve to the
	# very python.exe it is trying to overwrite, and Windows refuses to
	# rewrite a running executable (permission denied). `py` lives outside
	# any venv, so `make install` works whether or not .venv is active.
	PYTHON  := py -3
else
	PY      := $(VENV)/bin/python
	PIP     := $(VENV)/bin/pip
	ALEMBIC := $(VENV)/bin/alembic
	UVICORN := $(VENV)/bin/uvicorn
	CELERY  := $(VENV)/bin/celery
	RUFF    := $(VENV)/bin/ruff
	PYTHON  := python3
endif

# Resolved by make, not the shell, so it works the same whether the recipe
# below runs under sh.exe or (Windows without sh.exe on PATH) cmd.exe — a
# shell-level ${VAR:-default} only means what it looks like under sh.
FLOWER_BASIC_AUTH ?= pms-console:dev-only-flower-password

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## create the virtualenv and install everything
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e ".[dev]"

db:  ## start PostgreSQL (docker) and create both roles
	docker compose up -d db

redis:  ## start Redis (docker) — the Celery broker and the outbox nudge
	docker compose up -d redis

migrate:  ## apply migrations
	$(ALEMBIC) upgrade head

seed:  ## load the demo clinics
	$(PY) scripts/seed.py

run:  ## serve on http://localhost:8000
	$(UVICORN) app.main:app --reload --port 8000

# The three background processes, for when the API is run on the host rather
# than in a container. `docker compose up` starts all three itself.
worker: redis  ## run a Celery worker (-E feeds the monitor)
	$(CELERY) -A app.worker.celery_app worker -Q pms --loglevel=INFO -E

beat: redis  ## run the scheduler — exactly one of these, ever
	$(CELERY) -A app.worker.celery_app beat --loglevel=INFO

# Bound to the loopback, like the database and the broker: the console reaches
# it through the API, and nothing else has any business reaching it at all. Set
# FLOWER_URL=http://127.0.0.1:5555 and a matching FLOWER_BASIC_AUTH in .env, or
# the API will keep looking for the container by name. Needs `.[monitor]`.
flower: redis  ## run the job monitor on 127.0.0.1:5555
	$(CELERY) -A app.worker.celery_app flower \
	  --address=127.0.0.1 --port=5555 \
	  --url_prefix=api/v1/platform/jobs/flower \
	  --basic_auth=$(FLOWER_BASIC_AUTH) \
	  --max_tasks=10000

test:  ## run the suite against the test database
	$(PY) -m pytest -q

lint:  ## ruff
	$(RUFF) check .

fmt:  ## ruff --fix
	$(RUFF) check --fix .

check: lint test  ## everything CI runs

reset: migrate seed  ## rebuild the demo database from scratch

up: db migrate seed run  ## the whole thing, from nothing
