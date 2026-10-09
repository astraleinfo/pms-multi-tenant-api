"""One error vocabulary for the whole API.

Two rules carry the security weight:
  * a resource that belongs to another tenant is reported as *not found*,
    never as *forbidden* — a 403 would confirm the row exists;
  * internal exception text never reaches a client.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_context

log = logging.getLogger("pms.error")


class AppError(Exception):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AuthError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"


class AccountLockedError(AppError):
    status_code = status.HTTP_423_LOCKED
    code = "account_locked"


class PermissionDenied(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class NotFound(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class Conflict(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class AccountOnHold(AppError):
    """The clinic is on hold for an unpaid subscription bill.

    402 rather than 403: nothing is wrong with who is asking, and the way out
    is to pay. The client shows the bill instead of the screen it asked for.
    """

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "account_on_hold"


class RateLimited(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


class TenantIsolationViolation(Exception):
    """Raised by the ORM guard when a query would cross a tenant boundary.

    This is a bug in the API, not a client error: it becomes a 500 and is logged
    at CRITICAL so it is impossible to miss.
    """


def _payload(code: str, message: str, request: Request, details: dict[str, Any] | None = None) -> dict:
    body = {"error": {"code": code, "message": message}, "request_id": _request_id(request)}
    if details:
        body["error"]["details"] = details
    return body


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or str(uuid.uuid4())


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if isinstance(exc, AuthError) else None
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(exc.code, exc.message, request, exc.details),
            headers=headers,
        )

    @app.exception_handler(TenantIsolationViolation)
    async def _isolation(request: Request, exc: TenantIsolationViolation) -> JSONResponse:
        log.critical(
            "tenant isolation guard tripped: %s", exc, exc_info=exc, extra=request_context(request),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_payload("internal_error", "The request was refused.", request),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"field": ".".join(str(p) for p in err["loc"][1:]), "message": err["msg"]}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_payload("validation_failed", "Some fields need attention.", request, {"fields": fields}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {401: "unauthenticated", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(codes.get(exc.status_code, "error"), str(exc.detail), request),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError) -> JSONResponse:
        log.warning("integrity error on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_payload("conflict", "That change conflicts with existing data.", request),
        )

    @app.exception_handler(SQLAlchemyError)
    async def _sqlalchemy(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        log.exception("database error on %s", request.url.path, exc_info=exc, extra=request_context(request))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_payload("internal_error", "Something went wrong.", request),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s", request.url.path, exc_info=exc, extra=request_context(request))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_payload("internal_error", "Something went wrong.", request),
        )
