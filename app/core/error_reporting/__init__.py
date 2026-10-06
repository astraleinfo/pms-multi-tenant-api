"""Error reporting — where unexpected errors go besides the log.

The rule the code keeps is the one it already kept: *log it at ERROR*. The
unhandled-500 handler, a Celery task that raised, a worker loop that caught an
adapter bug and carried on — each already says `log.exception(...)`, and
`ReportingHandler` turns every such line into a `Report` for the configured
service. No module outside this package names a vendor, so switching service
is configuration, not a hunt through the codebase.

Adding a service is three lines: write the adapter (see `base.Reporter`), add
its name to `ERROR_REPORTER` in `app/core/config.py`, and add an entry to
`_BUILDERS`.

Every process that wants reports calls `init` once at startup, naming itself:
the API in its lifespan, Celery's worker and beat from their init signals, the
outbox worker from its entry point. A process that never calls it keeps the
`NullReporter` and reports nothing — which is what the test suite gets.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from typing import Any

from app.core.config import settings
from app.core.error_reporting.base import NullReporter, Report, Reporter

log = logging.getLogger("pms.error_reporting")


def _none(_: str) -> Reporter:
    return NullReporter()


def _sentry(component: str) -> Reporter:
    from app.core.error_reporting.sentry import SentryReporter

    return SentryReporter(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        release=settings.app_release,
        component=component,
    )


_BUILDERS: dict[str, Callable[[str], Reporter]] = {
    "none": _none,
    "sentry": _sentry,
}

_reporter: Reporter = NullReporter()


def init(component: str) -> Reporter:
    """Start reporting from this process to the service `ERROR_REPORTER` names.

    `component` — "api", "worker", "beat", "outbox" — is attached to every
    report, so one project can tell the processes apart. A misconfigured
    service raises here, at startup, rather than dropping reports later.
    """
    builder = _BUILDERS.get(settings.error_reporter)
    if builder is None:  # pragma: no cover - unreachable while the Literal is the source
        raise RuntimeError(f"No error reporter is registered for {settings.error_reporter!r}.")
    reporter = install(builder(component))
    if reporter.name != "none":
        log.info("reporting errors to %s as %s", reporter.name, component)
    return reporter


def install(reporter: Reporter) -> Reporter:
    """Make `reporter` the one every report goes to. `init` uses it; so do tests."""
    global _reporter
    _reporter = reporter
    return reporter


def current() -> Reporter:
    return _reporter


def flush(timeout: float = 2.0) -> None:
    """Give in-flight reports a moment to leave before the process exits."""
    try:
        _reporter.flush(timeout)
    except Exception:  # noqa: BLE001 - shutting down must not fail on this
        log.warning("could not flush error reports", exc_info=True)


#: The request context `RequestContextMiddleware` and the error handlers put on
#: a log line. Identifiers only — the same ones the JSON log carries.
CONTEXT_FIELDS = ("request_id", "tenant_id", "actor_id", "path", "method")

ContextProvider = Callable[[], Mapping[str, Any]]

#: The request being served, as a function that reads its context *now*. Read
#: at report time rather than bind time, because the clinic and the user are
#: only known once the auth dependencies have run — after the bind.
_bound: ContextVar[ContextProvider | None] = ContextVar("pms_error_context", default=None)


def bind(provider: ContextProvider) -> Token:
    """Attach `provider`'s context to every report made until `unbind`.

    `RequestContextMiddleware` binds each request, so a line logged anywhere
    beneath a route — a service, a storage call, a mailer — is reported with
    the request it happened in, without the log call having to pass it.
    """
    return _bound.set(provider)


def unbind(token: Token) -> None:
    _bound.reset(token)


def _tags(record: logging.LogRecord) -> dict[str, str]:
    """The bound request's context, overridden by whatever the line carried."""
    tags: dict[str, Any] = {}
    provider = _bound.get()
    if provider is not None:
        try:
            tags.update(provider())
        except Exception:  # noqa: BLE001 - a report without context beats none
            log.warning("could not read the request context for a report", exc_info=True)
    for key in CONTEXT_FIELDS:
        value = getattr(record, key, None)
        if value is not None:
            tags[key] = value
    return {key: str(tags[key]) for key in CONTEXT_FIELDS if tags.get(key) is not None}


# Records from these loggers are never reports: the reporter's own complaints,
# and the SDKs it uses. Reporting a failure to report would loop.
_IGNORED = ("pms.error_reporting", "sentry_sdk")


#: Set on a record, and on its exception, once reported.
_REPORTED = "_pms_error_reported"


def _already_reported(record: logging.LogRecord, exc: BaseException | None) -> bool:
    """Mark this record and its exception as reported; say if either already was.

    One failure can reach the handler more than once: a record propagating
    through two loggers this handler is attached to, or an exception logged by
    the 500 handler and then again by uvicorn when Starlette re-raises it.
    Either way it is one report.
    """
    seen = getattr(record, _REPORTED, False) or getattr(exc, _REPORTED, False)
    setattr(record, _REPORTED, True)
    if exc is not None:
        setattr(exc, _REPORTED, True)
    return seen


class ReportingHandler(logging.Handler):
    """Forwards every ERROR and CRITICAL log line to the active reporter."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self._busy = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_IGNORED) or getattr(self._busy, "on", False):
            return
        exc_info = record.exc_info if record.exc_info and record.exc_info[1] is not None else None
        if _already_reported(record, exc_info[1] if exc_info else None):
            return
        self._busy.on = True
        try:
            _reporter.report(
                Report(
                    message=record.getMessage(),
                    template=str(record.msg),
                    level="critical" if record.levelno >= logging.CRITICAL else "error",
                    logger=record.name,
                    exc_info=exc_info,
                    tags=_tags(record),
                )
            )
        except Exception:  # noqa: BLE001 - a broken reporter must not break logging
            self.handleError(record)
        finally:
            self._busy.on = False


__all__ = [
    "CONTEXT_FIELDS",
    "NullReporter",
    "Report",
    "Reporter",
    "ReportingHandler",
    "bind",
    "current",
    "flush",
    "init",
    "install",
    "unbind",
]
