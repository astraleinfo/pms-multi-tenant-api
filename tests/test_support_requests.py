"""Support requests: a clinic asking the desk, and the worker emailing it.

The Celery hop is not exercised here — `enqueue_support_request` is replaced
with a recorder, and the worker half (`support.deliver`, `support.due`) is
awaited directly, the way the message tests drive `drain`. Nothing reaches a
mail server: `mailer.send` is replaced wherever a send would happen.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.db.session import session_factory, tenant_session
from app.models.support import SupportRequest
from app.services import mailer, support
from tests.conftest import audit_lines


@pytest.fixture(autouse=True)
def fresh_asks(monkeypatch):
    """Every test asks as if for the first time, and never hits the budget."""
    monkeypatch.setattr(settings, "support_request_dedupe_minutes", 0)
    monkeypatch.setattr(settings, "support_request_rate_limit", "10000/minute")


@pytest.fixture
def queued(monkeypatch) -> list[tuple[uuid.UUID, uuid.UUID]]:
    from app.worker import tasks

    calls: list[tuple[uuid.UUID, uuid.UUID]] = []
    monkeypatch.setattr(
        tasks, "enqueue_support_request", lambda t, r: calls.append((t, r)) or True
    )
    return calls


@pytest.fixture
def outbox(monkeypatch) -> list[mailer.Email]:
    sent: list[mailer.Email] = []

    async def capture(email: mailer.Email) -> None:
        sent.append(email)

    monkeypatch.setattr(mailer, "send", capture)
    return sent


async def _locked_module(signed) -> dict:
    """A paid module this clinic does not have switched on."""
    catalog = (await signed.get("/v1/tenant/feature-catalog")).json()
    active = {
        f["feature_key"]
        for f in (await signed.get("/v1/tenant/features")).json()
        if f["status"] == "active"
    }
    for feature in catalog:
        if not feature["is_core"] and feature["key"] not in active:
            return feature
    pytest.skip("every paid module is already active for this clinic")


async def _row(tenant_id: str, request_id: str) -> SupportRequest:
    async with tenant_session(uuid.UUID(tenant_id)) as session:
        return await session.get(SupportRequest, uuid.UUID(request_id))


# ------------------------------------------------------------------ asking


async def test_asking_for_a_module_is_stored_audited_and_queued(northgate, queued):
    module = await _locked_module(northgate)
    response = await northgate.post(
        "/v1/tenant/support-requests",
        json={"kind": "activation", "feature_key": module["key"], "message": "From next month."},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["feature_key"] == module["key"]
    assert "admin@northgate.clinic" in body["requested_by"]

    # Handed to the worker, not sent from the request.
    assert (uuid.UUID(northgate.tenant_id), uuid.UUID(body["id"])) in queued

    trail = await audit_lines(northgate.tenant_id, action="support.request_activation")
    assert any(line["entity_id"] == body["id"] for line in trail)

    listed = (await northgate.get("/v1/tenant/support-requests")).json()
    assert body["id"] in {r["id"] for r in listed}


async def test_the_same_ask_twice_returns_the_first_and_queues_nothing(
    northgate, queued, monkeypatch
):
    monkeypatch.setattr(settings, "support_request_dedupe_minutes", 10)
    module = await _locked_module(northgate)
    ask = {"kind": "walkthrough", "feature_key": module["key"]}

    first = await northgate.post("/v1/tenant/support-requests", json=ask)
    queued.clear()
    second = await northgate.post("/v1/tenant/support-requests", json=ask)

    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert queued == []


async def test_any_member_of_staff_may_ask(northgate_doctor, queued):
    response = await northgate_doctor.post(
        "/v1/tenant/support-requests",
        json={"kind": "general", "message": "The printer layout cuts off the footer."},
    )
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "activation"},  # which module?
        {"kind": "general"},  # asking for nothing
        {"kind": "activation", "feature_key": "pharmacy", "tenant_id": str(uuid.uuid4())},
    ],
)
async def test_malformed_asks_are_refused(northgate, queued, payload):
    response = await northgate.post("/v1/tenant/support-requests", json=payload)
    assert response.status_code == 422, response.text


async def test_an_unknown_module_is_not_found(northgate, queued):
    response = await northgate.post(
        "/v1/tenant/support-requests", json={"kind": "activation", "feature_key": "teleportation"}
    )
    assert response.status_code == 404


async def test_asking_for_what_the_clinic_already_has_is_a_conflict(northgate, queued):
    catalog = (await northgate.get("/v1/tenant/feature-catalog")).json()
    core = next(f for f in catalog if f["is_core"])
    response = await northgate.post(
        "/v1/tenant/support-requests", json={"kind": "activation", "feature_key": core["key"]}
    )
    assert response.status_code == 409


# --------------------------------------------------------------- isolation


async def test_a_clinic_sees_only_its_own_asks_and_the_console_sees_each(
    northgate, clearview, console, queued
):
    module = await _locked_module(northgate)
    mine = (
        await northgate.post(
            "/v1/tenant/support-requests", json={"kind": "walkthrough", "feature_key": module["key"]}
        )
    ).json()

    theirs = (await clearview.get("/v1/tenant/support-requests")).json()
    assert mine["id"] not in {r["id"] for r in theirs}

    seen = await console.get(f"/v1/platform/tenants/{northgate.tenant_id}/support-requests")
    assert seen.status_code == 200, seen.text
    assert mine["id"] in {r["id"] for r in seen.json()}


# ------------------------------------------------------------------ worker


async def test_the_worker_emails_the_desk_once(northgate, queued, outbox):
    module = await _locked_module(northgate)
    body = (
        await northgate.post(
            "/v1/tenant/support-requests",
            json={"kind": "activation", "feature_key": module["key"], "message": "Please & thanks"},
        )
    ).json()
    tenant_id, request_id = uuid.UUID(northgate.tenant_id), uuid.UUID(body["id"])

    assert await support.deliver(session_factory, tenant_id, request_id) == "sent"
    # A redelivered job — the worker was restarted after sending — does nothing.
    assert await support.deliver(session_factory, tenant_id, request_id) == "skipped"

    assert len(outbox) == 1
    email = outbox[0]
    assert email.to == settings.support_email
    assert email.reply_to == "admin@northgate.clinic"
    assert module["name"] in email.subject
    assert body["id"] in email.text
    assert "Please &amp; thanks" in (email.html or "")

    row = await _row(northgate.tenant_id, body["id"])
    assert row.status == "sent" and row.sent_at is not None and row.attempts == 1

    trail = await audit_lines(northgate.tenant_id, action="support.request_email")
    assert any(line["entity_id"] == body["id"] and line["outcome"] == "ok" for line in trail)


async def test_a_mail_failure_backs_off_then_parks(northgate, queued, monkeypatch):
    async def broken(_email):
        raise mailer.MailError("connection refused")

    monkeypatch.setattr(mailer, "send", broken)
    monkeypatch.setattr(settings, "support_request_max_attempts", 2)

    module = await _locked_module(northgate)
    body = (
        await northgate.post(
            "/v1/tenant/support-requests", json={"kind": "walkthrough", "feature_key": module["key"]}
        )
    ).json()
    tenant_id, request_id = uuid.UUID(northgate.tenant_id), uuid.UUID(body["id"])

    assert await support.deliver(session_factory, tenant_id, request_id) == "retry"
    row = await _row(northgate.tenant_id, body["id"])
    assert row.status == "queued" and row.attempts == 1
    assert "connection refused" in row.last_error

    # Not due until the backoff passes — neither a stray job nor the sweep sends it early.
    assert await support.deliver(session_factory, tenant_id, request_id) == "skipped"
    assert (tenant_id, request_id) not in await support.due(session_factory)

    async with tenant_session(tenant_id) as session:
        (await session.get(SupportRequest, request_id)).next_attempt_at = row.created_at

    assert (tenant_id, request_id) in await support.due(session_factory)
    assert await support.deliver(session_factory, tenant_id, request_id) == "failed"

    row = await _row(northgate.tenant_id, body["id"])
    assert row.status == "failed" and row.attempts == 2
    trail = await audit_lines(northgate.tenant_id, action="support.request_email")
    assert any(line["entity_id"] == body["id"] and line["outcome"] == "failed" for line in trail)


async def test_the_sweep_finds_what_the_broker_never_got(northgate, monkeypatch, outbox):
    """The enqueue fails (Redis down); the row is still there for the sweep."""
    from app.worker import tasks

    monkeypatch.setattr(tasks, "enqueue_support_request", lambda t, r: False)
    response = await northgate.post(
        "/v1/tenant/support-requests", json={"kind": "general", "message": "Sweep me."}
    )
    assert response.status_code == 201
    pending = await support.due(session_factory)
    assert (uuid.UUID(northgate.tenant_id), uuid.UUID(response.json()["id"])) in pending
