"""The application.

Wiring only: configuration, middleware, error handlers, routes. Everything with
a decision in it lives in `app/services`, and everything that enforces tenancy
lives in `app/db` and `app/api/deps.py`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1 import api_router
from app.core import error_reporting
from app.core.config import settings
from app.core.errors import install_error_handlers
from app.core.logging import (
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    configure_logging,
)
from app.db import guard  # noqa: F401 — importing installs the ORM isolation listeners
from app.db.session import dispose_engine, engine
from app.services import jobs as jobs_service

log = logging.getLogger("pms")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()

    problems = settings.validate_for_production()
    if problems:
        raise RuntimeError("Refusing to start: " + "; ".join(problems))
    error_reporting.init("api")

    async with engine.connect() as conn:
        # Fail fast, and prove the app role is the unprivileged one.
        row = await conn.execute(
            text(
                "select current_user, "
                "(select rolbypassrls from pg_roles where rolname = current_user) as bypasses_rls"
            )
        )
        user, bypasses = row.one()
        if bypasses:
            message = (
                f"the database role {user!r} has BYPASSRLS — tenant isolation would be "
                "unenforced at the database layer"
            )
            if settings.is_production:
                raise RuntimeError("Refusing to start: " + message)
            log.warning("SECURITY: %s", message)
        log.info("connected to postgres as %s", user)

    yield
    await jobs_service.close()
    await dispose_engine()
    error_reporting.flush()


app = FastAPI(
    title="Astrale PMS API",
    version="1.0.0",
    description=(
        "Multi-tenant patient management. Every row belongs to exactly one clinic, "
        "the clinic comes from the host and the session token agreeing, and PostgreSQL "
        "row-level security enforces it below the application."
    ),
    lifespan=lifespan,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url=None,
    openapi_url="/openapi.json" if not settings.is_production else None,
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware, rate=settings.api_rate_limit)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Tenant-Slug"],
    expose_headers=["X-Request-ID"],
    max_age=600,
)

install_error_handlers(app)
app.include_router(api_router)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok"}


@app.get("/health/db", tags=["meta"])
async def health_db() -> dict:
    async with engine.connect() as conn:
        await conn.execute(text("select 1"))
    return {"status": "ok", "database": "reachable"}
