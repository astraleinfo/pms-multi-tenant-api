"""Support requests: a clinic asking the desk, and the worker emailing it.

The Celery hop is not exercised here — `enqueue_support_request` is replaced
with a recorder, and the worker half (`support.deliver`) is
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


async def _row(tenant_id: str, request_id: str) -> SupportRequest:
    async with tenant_session(uuid.UUID(tenant_id)) as session:
        return await session.get(SupportRequest, uuid.UUID(request_id))


# ------------------------------------------------------------------ asking


async def test_asking_for_a_walkthrough_is_stored_audited_and_queued(northgate, queued):
    response = await northgate.post(
        "/v1/tenant/support-requests",
        json={"kind": "walkthrough", "message": "Show the front desk the lab bench."},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["kind"] == "walkthrough"
    assert "feature_key" not in body
    assert "admin@northgate.clinic" in body["requested_by"]

    # Handed to the worker, not sent from the request.
    assert (uuid.UUID(northgate.tenant_id), uuid.UUID(body["id"])) in queued

    trail = await audit_lines(northgate.tenant_id, action="support.request_walkthrough")
    assert any(line["entity_id"] == body["id"] for line in trail)

    listed = (await northgate.get("/v1/tenant/support-requests")).json()
    assert body["id"] in {r["id"] for r in listed}


async def test_a_walkthrough_needs_no_message(northgate, queued):
    response = await northgate.post("/v1/tenant/support-requests", json={"kind": "walkthrough"})
    assert response.status_code == 201, response.text


async def test_the_same_ask_twice_returns_the_first_and_queues_nothing(
    northgate, queued, monkeypatch
):
    """Deduplicated on who asked and what kind of ask it is."""
    monkeypatch.setattr(settings, "support_request_dedupe_minutes", 10)
    ask = {"kind": "walkthrough"}

    first = await northgate.post("/v1/tenant/support-requests", json=ask)
    queued.clear()
    second = await northgate.post("/v1/tenant/support-requests", json=ask)

    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert queued == []

    # A different kind of ask from the same person is a new request.
    other = await northgate.post(
        "/v1/tenant/support-requests", json={"kind": "general", "message": "Something else."}
    )
    assert other.status_code == 201


async def test_a_general_ask_that_says_something_new_is_not_a_repeat(northgate, queued, monkeypatch):
    monkeypatch.setattr(settings, "support_request_dedupe_minutes", 10)
    first = await northgate.post(
        "/v1/tenant/support-requests", json={"kind": "general", "message": "First question."}
    )
    second = await northgate.post(
        "/v1/tenant/support-requests", json={"kind": "general", "message": "Second question."}
    )
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


async def test_any_member_of_staff_may_ask(northgate_doctor, queued):
    response = await northgate_doctor.post(
        "/v1/tenant/support-requests",
        json={"kind": "general", "message": "The printer layout cuts off the footer."},
    )
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "general"},  # asking for nothing
        {"kind": "general", "message": "   "},
        # there is nothing left to activate, and no module to name
        {"kind": "activation", "message": "Switch the lab on"},
        {"kind": "walkthrough", "feature_key": "lab"},
        {"kind": "general", "message": "Hi", "tenant_id": str(uuid.uuid4())},
    ],
)
async def test_malformed_asks_are_refused(northgate, queued, payload):
    response = await northgate.post("/v1/tenant/support-requests", json=payload)
    assert response.status_code == 422, response.text


# --------------------------------------------------------------- isolation


async def test_a_clinic_sees_only_its_own_asks_and_the_console_sees_each(
    northgate, clearview, console, queued
):
    mine = (
        await northgate.post(
            "/v1/tenant/support-requests", json={"kind": "walkthrough", "message": "Mine."}
        )
    ).json()

    theirs = (await clearview.get("/v1/tenant/support-requests")).json()
    assert mine["id"] not in {r["id"] for r in theirs}

    seen = await console.get(f"/v1/platform/tenants/{northgate.tenant_id}/support-requests")
    assert seen.status_code == 200, seen.text
    assert mine["id"] in {r["id"] for r in seen.json()}


# ------------------------------------------------------------------ worker


async def test_the_worker_emails_the_desk_once(northgate, queued, outbox):
    body = (
        await northgate.post(
            "/v1/tenant/support-requests",
            json={"kind": "walkthrough", "message": "Please & thanks"},
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
    assert email.subject == "Walkthrough for Northgate Dental Studio"
    assert body["id"] in email.text
    # nothing about modules or what they cost
    assert "Module" not in email.text and "per month" not in email.text
    assert "Please &amp; thanks" in (email.html or "")

    row = await _row(northgate.tenant_id, body["id"])
    assert row.status == "sent" and row.sent_at is not None and row.attempts == 1

    trail = await audit_lines(northgate.tenant_id, action="support.request_email")
    assert any(line["entity_id"] == body["id"] and line["outcome"] == "ok" for line in trail)


async def test_a_mail_failure_fails_at_once_and_the_desk_retries_it(
    northgate, console, queued, monkeypatch
):
    """No backoff and no sweep: one attempt, then it waits for the console."""
    async def broken(_email):
        raise mailer.MailError("connection refused")

    monkeypatch.setattr(mailer, "send", broken)
    body = (
        await northgate.post(
            "/v1/tenant/support-requests", json={"kind": "walkthrough", "message": "Retry me."}
        )
    ).json()
    tenant_id, request_id = uuid.UUID(northgate.tenant_id), uuid.UUID(body["id"])

    assert await support.deliver(session_factory, tenant_id, request_id) == "failed"
    row = await _row(northgate.tenant_id, body["id"])
    assert row.status == "failed" and row.attempts == 1
    assert "connection refused" in row.last_error
    trail = await audit_lines(northgate.tenant_id, action="support.request_email")
    assert any(line["entity_id"] == body["id"] and line["outcome"] == "failed" for line in trail)

    listed = (await console.get(f"/v1/platform/tenants/{northgate.tenant_id}/support-requests")).json()
    assert next(r for r in listed if r["id"] == body["id"])["retryable"] is True

    queued.clear()
    retried = await console.post(
        f"/v1/platform/tenants/{northgate.tenant_id}/support-requests/{body['id']}/retry"
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "queued" and retried.json()["retryable"] is False
    assert queued == [(tenant_id, request_id)]
    trail = await audit_lines(northgate.tenant_id, action="platform.support_request_retry")
    assert any(line["entity_id"] == body["id"] for line in trail)

    sent: list[mailer.Email] = []

    async def capture(email: mailer.Email) -> None:
        sent.append(email)

    monkeypatch.setattr(mailer, "send", capture)
    assert await support.deliver(session_factory, tenant_id, request_id) == "sent"
    assert len(sent) == 1
    row = await _row(northgate.tenant_id, body["id"])
    assert row.status == "sent" and row.attempts == 2

    # Sent is final: nothing to retry.
    again = await console.post(
        f"/v1/platform/tenants/{northgate.tenant_id}/support-requests/{body['id']}/retry"
    )
    assert again.status_code == 409, again.text


async def test_a_lost_hand_off_turns_retryable_after_a_while(northgate, console, queued):
    """The broker never got it; the row stays queued, and only once it has sat
    there a while does the console offer to send it — not while the hand-off
    may still be on its way."""
    body = (
        await northgate.post(
            "/v1/tenant/support-requests", json={"kind": "general", "message": "Lost me."}
        )
    ).json()
    assert body["status"] == "queued" and body["retryable"] is False
    url = f"/v1/platform/tenants/{northgate.tenant_id}/support-requests/{body['id']}/retry"
    assert (await console.post(url)).status_code == 409

    tenant_id = uuid.UUID(northgate.tenant_id)
    async with tenant_session(tenant_id) as session:
        row = await session.get(SupportRequest, uuid.UUID(body["id"]))
        row.created_at = row.updated_at = row.created_at - support.STUCK_AFTER * 2

    listed = (await console.get(f"/v1/platform/tenants/{northgate.tenant_id}/support-requests")).json()
    assert next(r for r in listed if r["id"] == body["id"])["retryable"] is True
    retried = await console.post(url)
    assert retried.status_code == 200, retried.text
    assert (tenant_id, uuid.UUID(body["id"])) in queued


async def test_another_clinics_request_is_not_found(northgate, clearview, console, queued):
    body = (
        await northgate.post("/v1/tenant/support-requests", json={"kind": "walkthrough"})
    ).json()
    response = await console.post(
        f"/v1/platform/tenants/{clearview.tenant_id}/support-requests/{body['id']}/retry"
    )
    assert response.status_code == 404, response.text
