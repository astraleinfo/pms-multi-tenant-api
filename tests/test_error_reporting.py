"""Error reporting.

The contract is the log: a line at ERROR or above is a report, carrying the
request's identifiers and nothing else. The bridge is tested against a
recording reporter, so these hold whichever service is configured; the Sentry
adapter is tested against an in-memory transport, so nothing leaves the
machine — including that patient data never would.
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
import sentry_sdk
from fastapi import BackgroundTasks, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sentry_sdk.transport import Transport

from app.core import error_reporting
from app.core.config import Settings, settings
from app.core.error_reporting import Report, ReportingHandler
from app.core.error_reporting.sentry import SentryReporter
from app.core.errors import install_error_handlers
from app.core.logging import RequestContextMiddleware, configure_logging


class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.reports: list[Report] = []

    def report(self, report: Report) -> None:
        self.reports.append(report)

    def flush(self, timeout: float) -> None:
        pass


@pytest.fixture
def reports():
    recorder = Recorder()
    previous = error_reporting.current()
    error_reporting.install(recorder)
    handler = ReportingHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield recorder.reports
    finally:
        root.removeHandler(handler)
        error_reporting.install(previous)


# --------------------------------------------------------------- the bridge


def test_an_error_line_is_a_report_with_its_context(reports):
    log = logging.getLogger("pms.test")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception(
            "sweep failed for %s", 41,
            extra={"request_id": "req-1", "tenant_id": "tenant-1", "status": 500},
        )

    [report] = reports
    assert report.message == "sweep failed for 41"
    assert report.template == "sweep failed for %s"
    assert report.level == "error"
    assert report.logger == "pms.test"
    assert report.exc_info is not None and report.exc_info[0] is ValueError
    # Context fields only — `status` is a log field, not a report tag.
    assert report.tags == {"request_id": "req-1", "tenant_id": "tenant-1"}


def test_only_error_and_above_are_reports(reports):
    log = logging.getLogger("pms.test")
    log.info("fine")
    log.warning("odd, but handled")
    assert reports == []

    log.critical("the isolation guard tripped")
    [report] = reports
    assert report.level == "critical"
    assert report.exc_info is None


def test_the_reporters_own_complaints_are_never_reports(reports):
    logging.getLogger("pms.error_reporting").error("could not reach the service")
    logging.getLogger("sentry_sdk.errors").error("transport failed")
    assert reports == []


def test_a_broken_reporter_does_not_break_the_caller(reports, monkeypatch):
    class Broken(Recorder):
        def report(self, report: Report) -> None:
            raise ConnectionError("service is down")

    monkeypatch.setattr(logging, "raiseExceptions", False)
    error_reporting.install(Broken())
    logging.getLogger("pms.test").error("this must still return")


def test_configure_logging_installs_the_bridge():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        configure_logging()
        assert any(isinstance(h, ReportingHandler) for h in root.handlers)
    finally:
        root.handlers, root.level = handlers, level


async def test_an_unhandled_route_error_is_reported_with_the_request(reports):
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/explode")
    async def explode(request: Request):
        request.state.request_id = "req-9"
        request.state.tenant_id = "tenant-1"
        request.state.actor_id = "user-1"
        raise RuntimeError("kaboom")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/explode")

    assert response.status_code == 500
    assert "kaboom" not in response.text
    [report] = [r for r in reports if r.logger == "pms.error"]
    assert report.exc_info is not None and report.exc_info[0] is RuntimeError
    assert report.tags == {
        "request_id": "req-9",
        "tenant_id": "tenant-1",
        "actor_id": "user-1",
        "path": "/explode",
        "method": "GET",
    }


def test_one_failure_is_one_report_however_often_it_is_logged(reports):
    # The 500 handler logs it, then Starlette re-raises and uvicorn logs it.
    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        logging.getLogger("pms.error").exception("unhandled error on /x")
        logging.getLogger("pms.test.server").exception("Exception in ASGI application")
    assert len(reports) == 1


def test_what_escapes_the_app_is_reported_though_uvicorn_does_not_propagate(reports):
    root = logging.getLogger()
    uvicorn_error = logging.getLogger("uvicorn.error")
    saved = (list(root.handlers), root.level, list(uvicorn_error.handlers), uvicorn_error.propagate)
    try:
        configure_logging()
        uvicorn_error.propagate = False  # as uvicorn's own logging config sets it
        try:
            raise RuntimeError("the queue socket fell over")
        except RuntimeError:
            uvicorn_error.exception("Exception in ASGI application")
    finally:
        root.handlers, root.level = saved[0], saved[1]
        uvicorn_error.handlers, uvicorn_error.propagate = saved[2], saved[3]

    [report] = reports
    assert report.logger == "uvicorn.error"
    assert report.exc_info is not None and report.exc_info[0] is RuntimeError


def test_a_celery_task_that_raises_is_reported(reports):
    from app.worker.celery_app import celery_app

    @celery_app.task(name="pms.test.explode")
    def explode():
        raise RuntimeError("task bug")

    assert explode.apply().state == "FAILURE"
    [report] = [r for r in reports if r.logger == "celery.app.trace"]
    assert report.exc_info is not None and report.exc_info[0] is RuntimeError


def test_a_message_that_runs_out_of_attempts_is_reported(reports, monkeypatch):
    from app.models.enums import MessageStatus
    from app.models.messaging import Message
    from app.services.messaging import outbox

    monkeypatch.setattr(settings, "message_max_attempts", 3)
    message = Message(attempts=1)
    outbox.mark_retry(message, "carrier timed out")
    assert reports == []

    outbox.mark_retry(message, "carrier timed out")
    assert message.status == MessageStatus.FAILED
    [report] = reports
    assert report.template == "message %s parked after %s attempts"


def test_credentials_the_key_no_longer_opens_are_reported(reports):
    from app.services.messaging.crypto import decrypt_secret

    assert decrypt_secret("gAAAAAB-not-a-token-this-key-made") == ""
    [report] = reports
    assert report.logger == "pms.messaging"


def test_a_logo_the_pdf_cannot_draw_is_reported(reports):
    from app.services.pdf import _logo_flowable

    assert _logo_flowable(b"\x89PNG but not really") is None
    [report] = reports
    assert report.logger == "pms.pdf"
    assert report.exc_info is not None


def _api() -> FastAPI:
    """The real request middleware and error handlers around test routes."""
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    return app


async def _call(app: FastAPI, path: str, **headers: str):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, headers=headers)


async def test_an_error_logged_beneath_a_route_carries_its_request(reports):
    app = _api()

    @app.post("/appointments")
    async def book(request: Request):
        # What the auth dependencies set — after the middleware has bound.
        request.state.tenant_id = "tenant-1"
        request.state.actor_id = "user-1"
        try:
            raise ConnectionError("broker down")
        except ConnectionError:
            # A service swallowing a failure so the booking still succeeds.
            logging.getLogger("pms.scheduling").exception("could not queue confirmation")
        return {"ok": True}

    response = await _call(app, "/appointments", **{"x-request-id": "req-42"})

    assert response.status_code == 200
    [report] = reports
    assert report.tags == {
        "request_id": "req-42",
        "tenant_id": "tenant-1",
        "actor_id": "user-1",
        "path": "/appointments",
        "method": "POST",
    }

    # Unbound once the request is over.
    logging.getLogger("pms.test").error("between requests")
    assert reports[1].tags == {}


async def test_a_background_task_that_raises_is_reported_once_with_its_request(reports):
    app = _api()

    def announce() -> None:
        raise RuntimeError("could not publish to the queue screen")

    @app.post("/visits")
    async def check_in(request: Request, tasks: BackgroundTasks):
        request.state.tenant_id = "tenant-1"
        tasks.add_task(announce)
        return {"ok": True}

    await _call(app, "/visits", **{"x-request-id": "req-7"})

    [report] = reports
    assert report.exc_info is not None and report.exc_info[0] is RuntimeError
    assert report.tags["request_id"] == "req-7"
    assert report.tags["tenant_id"] == "tenant-1"
    assert report.tags["path"] == "/visits"


# ------------------------------------------------------------ configuration


def test_production_refuses_sentry_without_a_dsn():
    problems = Settings(environment="production", error_reporter="sentry", sentry_dsn="").validate_for_production()
    assert "ERROR_REPORTER=sentry needs SENTRY_DSN" in problems


def test_init_refuses_sentry_without_a_dsn(monkeypatch):
    monkeypatch.setattr(settings, "error_reporter", "sentry")
    monkeypatch.setattr(settings, "sentry_dsn", "")
    previous = error_reporting.current()
    with pytest.raises(RuntimeError, match="SENTRY_DSN"):
        error_reporting.init("api")
    assert error_reporting.current() is previous


def test_none_reports_nothing(monkeypatch):
    monkeypatch.setattr(settings, "error_reporter", "none")
    previous = error_reporting.current()
    try:
        assert error_reporting.init("api").name == "none"
    finally:
        error_reporting.install(previous)


# ---------------------------------------------------------- the Sentry adapter


class Captured(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict] = []

    def capture_envelope(self, envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(event)

    def flush(self, timeout, callback=None) -> None:
        pass

    def kill(self) -> None:
        pass


@pytest.fixture
def sentry():
    transport = Captured()
    reporter = SentryReporter(
        dsn="https://public@sentry.invalid/1",
        environment="test",
        release="abc123",
        component="worker",
        transport=transport,
    )
    try:
        yield reporter, transport.events
    finally:
        sentry_sdk.get_global_scope().remove_tag("component")
        sentry_sdk.init(dsn="")  # an inert client; never SENTRY_DSN from the environment


def _report(**overrides) -> Report:
    fields = {
        "message": "message 7 raised unexpectedly",
        "template": "message %s raised unexpectedly",
        "level": "error",
        "logger": "pms.messaging",
        "tags": {"request_id": "req-1", "tenant_id": "tenant-1", "actor_id": "user-1"},
    }
    return Report(**{**fields, **overrides})


def test_sentry_sends_the_exception_with_its_context(sentry):
    reporter, events = sentry
    try:
        raise ValueError("adapter bug")
    except ValueError as exc:
        reporter.report(_report(exc_info=(type(exc), exc, exc.__traceback__)))

    [event] = events
    assert event["level"] == "error"
    assert event["logger"] == "pms.messaging"
    assert event["logentry"]["message"] == "message %s raised unexpectedly"
    assert event["environment"] == "test"
    assert event["release"] == "abc123"
    assert event["tags"]["component"] == "worker"
    assert event["tags"]["request_id"] == "req-1"
    assert event["tags"]["tenant_id"] == "tenant-1"
    assert event["user"] == {"id": "user-1"}
    [exception] = event["exception"]["values"]
    assert exception["type"] == "ValueError"
    assert exception["mechanism"]["handled"] is True


def test_sentry_maps_critical_to_fatal(sentry):
    reporter, events = sentry
    reporter.report(_report(level="critical"))
    [event] = events
    assert event["level"] == "fatal"
    assert "exception" not in event


def test_sentry_never_sees_patient_data(sentry, caplog):
    reporter, events = sentry
    # Made at runtime, so the only ways into the event are the ones under test
    # — not the source lines Sentry shows around the failing frame.
    phone, name = f"+91{uuid.uuid4().int % 10**10:010d}", uuid.uuid4().hex
    caplog.set_level(logging.INFO, logger="pms.messaging")
    # The console messaging provider logs the body it would have sent.
    logging.getLogger("pms.messaging").info(
        "console provider would send", extra={"to": phone, "body": f"{name}, your results are in"}
    )
    try:
        patient_name = name  # a local in the failing frame
        raise ValueError(f"could not process record {id(patient_name)}")
    except ValueError as exc:
        reporter.report(_report(exc_info=(type(exc), exc, exc.__traceback__)))

    [event] = events
    sent = json.dumps(event, default=str)
    assert name not in sent
    assert phone not in sent
    crumbs = event["breadcrumbs"]["values"]
    assert any(c.get("message") == "console provider would send" for c in crumbs)
    assert all("data" not in c for c in crumbs if c.get("type") == "log")
