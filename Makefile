.PHONY: help install db up down migrate seed run test lint fmt reset check

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
	docker compose up -d db

migrate:  ## apply migrations
	$(VENV)/bin/alembic upgrade head

seed:  ## load the demo clinics
	$(PY) scripts/seed.py

run:  ## serve on http://localhost:8000
	$(VENV)/bin/uvicorn app.main:app --reload --port 8000

test:  ## run the suite against the test database
	$(PY) -m pytest -q

lint:  ## ruff
	$(VENV)/bin/ruff check .

fmt:  ## ruff --fix
	$(VENV)/bin/ruff check --fix .

check: lint test  ## everything CI runs

reset: migrate seed  ## rebuild the demo database from scratch

up: db migrate seed run  ## the whole thing, from nothing
