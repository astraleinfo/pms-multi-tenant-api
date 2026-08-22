"""Structured request logs.

Every line carries the request id, and the tenant when one has been resolved,
so a support question ("what did Northgate see at 14:02?") can be answered
without grepping for patient data — which is never logged.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "at": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "tenant_id", "actor_id", "path", "method", "status", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.is_production else logging.Formatter(
        "%(levelname)s %(name)s %(message)s"
    ))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    logging.getLogger("uvicorn.access").handlers = [handler]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Stamps a request id, times the request, and logs the outcome."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()

        response = await call_next(request)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        logging.getLogger("pms.access").info(
            "%s %s → %s",
            request.method,
            request.url.path,
            response.status_code,
            extra={
                "request_id": request_id,
                "tenant_id": getattr(request.state, "tenant_id", None),
                "actor_id": getattr(request.state, "actor_id", None),
                "path": request.url.path,
                "method": request.method,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response


# An API serves JSON, never markup: nothing here should ever be a document.
API_CSP = "default-src 'none'; frame-ancestors 'none'; sandbox"

# `/docs` is the one exception — Swagger UI *is* a document, and it loads its
# assets from a CDN and boots from an inline script. It is served outside
# production only (see `app/main.py`), and this relaxation is scoped to those
# paths, so a deployed API keeps `API_CSP` on every response it can return.
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect"})
DOCS_CSP = (
    "default-src 'none'; "
    "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "font-src 'self' https://cdn.jsdelivr.net; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Headers that cost nothing and remove whole classes of mistake."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()"
        )
        serving_docs = request.url.path in DOCS_PATHS and not settings.is_production
        response.headers.setdefault(
            "Content-Security-Policy", DOCS_CSP if serving_docs else API_CSP
        )
        response.headers.setdefault("Cache-Control", "no-store")
        if settings.cookie_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """A blunt per-IP ceiling on everything. Auth routes are limited far more
    tightly inside the handler."""

    def __init__(self, app, rate: str) -> None:
        super().__init__(app)
        from app.core.rate_limit import Rate

        self.rate = Rate.parse(rate)

    async def dispatch(self, request: Request, call_next) -> Response:
        from starlette.responses import JSONResponse

        from app.core.errors import RateLimited
        from app.core.rate_limit import limiter

        if request.url.path.startswith("/v1"):
            ip = request.client.host if request.client else "unknown"
            try:
                limiter.check(f"api:{ip}", self.rate)
            except RateLimited as exc:
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                        "request_id": getattr(request.state, "request_id", ""),
                    },
                )
        return await call_next(request)
