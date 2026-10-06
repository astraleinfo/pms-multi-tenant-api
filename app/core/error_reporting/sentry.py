"""Sentry.

Imported only when `ERROR_REPORTER=sentry`, so `sentry_sdk` is never loaded by
a deployment that does not use it.

The SDK is told to keep to the same rule as the log: identifiers, never
patient data. That is not its default, so four things are switched off:

*   `send_default_pii` — no client IPs, cookies or auth headers.
*   `include_local_variables` — a stack frame's locals are whatever the code
    was holding: a patient row, a prescription, a message body.
*   Auto-enabled framework integrations (FastAPI, Celery, SQLAlchemy, httpx,
    ...). They would attach request bodies and query strings, and they would
    capture failures on their own — a second path the next service would not
    have. Every report comes through the logging bridge instead, so switching
    service changes nothing about what is reported.
*   Logging *events*. The bridge sends those. The SDK still records log lines
    as breadcrumbs — the trail that led to an error — but with their `extra`
    dropped, because the console messaging provider puts a message body there.
"""

from __future__ import annotations

import logging
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.utils import event_from_exception

from app.core.error_reporting.base import Report

log = logging.getLogger("pms.error_reporting")

# Sentry has no "critical"; its most severe level is "fatal".
_LEVELS = {"error": "error", "critical": "fatal"}


def _strip_breadcrumb(crumb: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    if crumb.get("type") == "log":
        crumb.pop("data", None)
    return crumb


class SentryReporter:
    name = "sentry"

    def __init__(
        self,
        *,
        dsn: str,
        environment: str,
        release: str,
        component: str,
        transport: Any = None,
    ) -> None:
        if not dsn:
            raise RuntimeError("ERROR_REPORTER=sentry needs SENTRY_DSN")
        options: dict[str, Any] = {}
        if transport is not None:  # the tests' in-memory transport
            options["transport"] = transport
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=release or None,
            send_default_pii=False,
            include_local_variables=False,
            max_request_body_size="never",
            auto_enabling_integrations=False,
            integrations=[LoggingIntegration(level=logging.INFO, event_level=None)],
            before_breadcrumb=_strip_breadcrumb,
            **options,
        )
        # On the global scope, so a Celery child forked from this process
        # inherits it along with the client.
        sentry_sdk.get_global_scope().set_tag("component", component)

    def report(self, report: Report) -> None:
        try:
            if report.exc_info is not None:
                event, hint = event_from_exception(
                    report.exc_info,
                    client_options=sentry_sdk.get_client().options,
                    mechanism={"type": "logging", "handled": True},
                )
            else:
                event, hint = {}, {}
            event["level"] = _LEVELS[report.level]
            event["logger"] = report.logger
            event["logentry"] = {"message": report.template, "formatted": report.message}

            with sentry_sdk.new_scope() as scope:
                for key, value in report.tags.items():
                    scope.set_tag(key, value)
                if actor_id := report.tags.get("actor_id"):
                    # Lets Sentry count the people an issue reached. An id,
                    # nothing else: the account is looked up here, not there.
                    scope.set_user({"id": actor_id})
                sentry_sdk.capture_event(event, hint=hint)
        except Exception:  # noqa: BLE001 - reporting must never fail the caller
            log.warning("could not hand a report to Sentry", exc_info=True)

    def flush(self, timeout: float) -> None:
        sentry_sdk.flush(timeout=timeout)
