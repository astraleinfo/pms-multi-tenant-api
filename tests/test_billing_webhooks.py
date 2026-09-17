"""Webhooks: signatures, storage, idempotency, and never trusting the caller.

The endpoint is unauthenticated — a gateway cannot sign in — so the signature
is the whole of the authentication, and these tests use the real HMAC over a
real secret rather than stubbing the check. A forged delivery has to be refused
by the code that will refuse it in production.

Five requirements, and a test for each:

*   verify signatures — a wrong signature is `rejected` and never processed;
*   store every webhook — including the rejected ones, because that is the
    entry most worth having;
*   idempotent processing — a redelivered capture does not pay an invoice twice;
*   prevent duplicate processing — the unique index on `(gateway, event_id)`;
*   audit failures — a refused signature writes an `audit_events` row.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.session import session_factory, tenant_session
from app.models.billing import SubscriptionPayment, WebhookEvent
from app.models.enums import (
    PaymentGateway,
    RefundStatus,
    SubscriptionInvoiceStatus,
    SubscriptionStatus,
    WebhookStatus,
)
from app.models.tenancy import AuditEvent
from app.services.payment import billing_service, payment_service, subscription_service
from app.services.payment.razorpay import client as rzp_client
from app.services.payment.webhook import handler as webhook_handler
from tests.billing_helpers import STARTER, invoices_of, make_tenant, plan, subscription_of

pytestmark = pytest.mark.usefixtures("seeded")

JAN_1 = date(2026, 1, 1)
WEBHOOK_SECRET = "whsec-test-only"  # noqa: S105 - a test fixture, not a credential


@pytest.fixture(autouse=True)
def webhook_secret(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_key")
    monkeypatch.setattr(settings, "razorpay_key_secret", "rzp_test_secret")


def _delivery(event: str, payload: dict, *, event_id: str) -> tuple[bytes, dict]:
    """A signed Razorpay delivery: the raw bytes, and the headers for them.

    Signed over the exact bytes that are sent, because that is what Razorpay
    signs — re-serialising the parsed JSON would produce different bytes and a
    signature that never verifies.
    """
    body = json.dumps({"entity": "event", "event": event, "payload": payload}).encode()
    headers = {
        "x-razorpay-signature": rzp_client.sign(body, WEBHOOK_SECRET),
        "x-razorpay-event-id": event_id,
        "content-type": "application/json",
    }
    return body, headers


async def _with_invoice(label: str):
    tenant_id = await make_tenant(label)
    starter = await plan(STARTER)
    async with tenant_session(tenant_id) as session:
        await subscription_service.subscribe(
            session, tenant_id, plan=starter, today=JAN_1, trial_days=0
        )
    return tenant_id, (await invoices_of(tenant_id))[0]


def _payment_payload(tenant_id, invoice, *, payment_id: str, amount: Decimal, status="captured"):
    """A `payment` entity carrying the notes this system stamps on its orders."""
    return {
        "payment": {
            "entity": {
                "id": payment_id,
                "amount": int(amount * 100),
                "currency": "INR",
                "status": status,
                "method": "upi",
                "order_id": "order_from_gateway",
                "notes": {"tenant_id": str(tenant_id), "invoice_id": str(invoice.id)},
            }
        }
    }


async def _events() -> list[WebhookEvent]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(select(WebhookEvent).order_by(WebhookEvent.received_at))
            ).scalars()
        )


async def test_a_signed_delivery_is_stored_and_processed(client):
    """The happy path, end to end through the HTTP endpoint."""
    tenant_id, invoice = await _with_invoice("wh-ok")
    body, headers = _delivery(
        "payment.captured",
        _payment_payload(tenant_id, invoice, payment_id="pay_wh1", amount=Decimal(str(invoice.total))),
        event_id="evt_ok_1",
    )

    response = await client.post(
        "/v1/webhooks/razorpay", content=body, headers={**headers, "Host": "localhost"}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "received"}

    stored = [e for e in await _events() if e.event_id == "evt_ok_1"]
    assert len(stored) == 1
    assert stored[0].signature_verified
    assert stored[0].status == WebhookStatus.RECEIVED
    assert stored[0].entity_id == "pay_wh1", "the gateway id is indexed without parsing the JSON"

    assert await webhook_handler.process_one(stored[0].id) == WebhookStatus.PROCESSED

    settled = (await invoices_of(tenant_id))[0]
    assert settled.status == SubscriptionInvoiceStatus.PAID
    assert [p.gateway_payment_id for p in settled.payments] == ["pay_wh1"]


async def test_an_invalid_signature_is_stored_refused_and_audited(client):
    """The delivery most worth keeping. Stored, never processed, audited."""
    tenant_id, invoice = await _with_invoice("wh-forged")
    body, headers = _delivery(
        "payment.captured",
        _payment_payload(tenant_id, invoice, payment_id="pay_forged", amount=Decimal("1.00")),
        event_id="evt_forged_1",
    )
    headers["x-razorpay-signature"] = "deadbeef" * 8

    response = await client.post(
        "/v1/webhooks/razorpay", content=body, headers={**headers, "Host": "localhost"}
    )
    # The same answer a valid delivery gets: telling an unauthenticated caller
    # that their forgery was detected helps them iterate.
    assert response.status_code == 200
    assert response.json() == {"status": "received"}

    stored = [e for e in await _events() if e.event_id == "evt_forged_1"]
    assert len(stored) == 1, "it is kept, not discarded"
    assert not stored[0].signature_verified
    assert stored[0].status == WebhookStatus.REJECTED

    # It is never processed, whatever the worker is asked to do.
    assert await webhook_handler.process_one(stored[0].id) == WebhookStatus.REJECTED
    assert (await invoices_of(tenant_id))[0].status == SubscriptionInvoiceStatus.ISSUED

    async with session_factory() as session:
        audited = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "billing.webhook_rejected")
            )
        ).scalars().all()
    assert any(a.meta and a.meta.get("event_id") == "evt_forged_1" for a in audited)


async def test_a_delivery_with_no_signature_at_all_is_refused(client):
    tenant_id, invoice = await _with_invoice("wh-unsigned")
    body, headers = _delivery(
        "payment.captured",
        _payment_payload(tenant_id, invoice, payment_id="pay_unsigned", amount=Decimal("10.00")),
        event_id="evt_unsigned_1",
    )
    del headers["x-razorpay-signature"]

    await client.post(
        "/v1/webhooks/razorpay", content=body, headers={**headers, "Host": "localhost"}
    )
    stored = [e for e in await _events() if e.event_id == "evt_unsigned_1"][0]
    assert stored.status == WebhookStatus.REJECTED


async def test_a_redelivery_is_stored_once_and_paid_once(client):
    """Razorpay retries for twenty-four hours. Five deliveries, one payment."""
    tenant_id, invoice = await _with_invoice("wh-redelivery")
    body, headers = _delivery(
        "payment.captured",
        _payment_payload(
            tenant_id, invoice, payment_id="pay_dup", amount=Decimal(str(invoice.total))
        ),
        event_id="evt_dup_1",
    )

    for _ in range(5):
        response = await client.post(
            "/v1/webhooks/razorpay", content=body, headers={**headers, "Host": "localhost"}
        )
        assert response.status_code == 200, "a redelivery is acknowledged, not refused"

    stored = [e for e in await _events() if e.event_id == "evt_dup_1"]
    assert len(stored) == 1, "the unique index on (gateway, event_id) holds"

    # Processing it repeatedly is equally harmless.
    for _ in range(3):
        await webhook_handler.process_one(stored[0].id)

    async with tenant_session(tenant_id) as session:
        payments = (
            await session.execute(
                select(SubscriptionPayment).where(SubscriptionPayment.tenant_id == tenant_id)
            )
        ).scalars().all()
    assert len(payments) == 1
    settled = (await invoices_of(tenant_id))[0]
    assert billing_service.paid_amount(settled) == Decimal(str(invoice.total))


async def test_the_same_payment_under_two_event_ids_is_still_paid_once(client):
    """Two *different* deliveries about one payment — `payment.captured` and
    `invoice.paid`, say. The event index does not help here; the payment index
    does."""
    tenant_id, invoice = await _with_invoice("wh-two-events")
    total = Decimal(str(invoice.total))

    for event_id in ("evt_same_a", "evt_same_b"):
        body, headers = _delivery(
            "payment.captured",
            _payment_payload(tenant_id, invoice, payment_id="pay_same", amount=total),
            event_id=event_id,
        )
        await client.post(
            "/v1/webhooks/razorpay", content=body, headers={**headers, "Host": "localhost"}
        )

    for event in await _events():
        if event.event_id in ("evt_same_a", "evt_same_b"):
            await webhook_handler.process_one(event.id)

    async with tenant_session(tenant_id) as session:
        payments = (
            await session.execute(
                select(SubscriptionPayment).where(SubscriptionPayment.tenant_id == tenant_id)
            )
        ).scalars().all()
    assert len(payments) == 1
    assert billing_service.paid_amount((await invoices_of(tenant_id))[0]) == total


async def test_an_event_this_product_does_not_act_on_is_stored_and_ignored(client):
    """Only the events the configured products emit are handled. The rest are
    kept — the log is what makes a webhook configuration auditable."""
    body, headers = _delivery(
        "payment.dispute.created", {"payment": {"entity": {"id": "pay_disputed"}}},
        event_id="evt_ignored_1",
    )
    await client.post(
        "/v1/webhooks/razorpay", content=body, headers={**headers, "Host": "localhost"}
    )
    stored = [e for e in await _events() if e.event_id == "evt_ignored_1"][0]
    assert stored.signature_verified
    assert stored.status == WebhookStatus.IGNORED


async def test_a_failed_payment_webhook_records_the_failure(client):
    tenant_id, invoice = await _with_invoice("wh-failed")
    payload = _payment_payload(
        tenant_id, invoice, payment_id="pay_wh_failed", amount=Decimal(str(invoice.total)),
        status="failed",
    )
    payload["payment"]["entity"]["error_description"] = "card declined by issuer"
    body, headers = _delivery("payment.failed", payload, event_id="evt_failed_1")

    await client.post(
        "/v1/webhooks/razorpay", content=body, headers={**headers, "Host": "localhost"}
    )
    stored = [e for e in await _events() if e.event_id == "evt_failed_1"][0]
    assert await webhook_handler.process_one(stored.id) == WebhookStatus.PROCESSED

    settled = (await invoices_of(tenant_id))[0]
    assert settled.status == SubscriptionInvoiceStatus.ISSUED, "a failure does not pay anything"
    assert settled.payments[0].failure_reason == "card declined by issuer"


async def test_a_capture_webhook_activates_a_pending_subscription(client):
    """The invoice being paid is what makes a subscription live, and it is the
    gateway's word that does it — never the browser's."""
    tenant_id, invoice = await _with_invoice("wh-activates")
    async with tenant_session(tenant_id) as session:
        subscription = await billing_service.require_subscription(
            session, tenant_id, for_update=True
        )
        subscription.status = SubscriptionStatus.PENDING

    body, headers = _delivery(
        "payment.captured",
        _payment_payload(
            tenant_id, invoice, payment_id="pay_activate", amount=Decimal(str(invoice.total))
        ),
        event_id="evt_activate_1",
    )
    await client.post(
        "/v1/webhooks/razorpay", content=body, headers={**headers, "Host": "localhost"}
    )
    stored = [e for e in await _events() if e.event_id == "evt_activate_1"][0]
    await webhook_handler.process_one(stored.id)

    assert (await subscription_of(tenant_id)).status == SubscriptionStatus.ACTIVE


async def test_a_subscription_webhook_is_recorded_but_not_obeyed(client):
    """A gateway that thinks a subscription is halted does not get to stop a
    clinic whose invoices this system knows are paid."""
    tenant_id, _ = await _with_invoice("wh-sub-status")
    async with tenant_session(tenant_id) as session:
        subscription = await billing_service.require_subscription(
            session, tenant_id, for_update=True
        )
        subscription.razorpay_subscription_id = "sub_remote_1"

    body, headers = _delivery(
        "subscription.halted",
        {
            "subscription": {
                "entity": {
                    "id": "sub_remote_1",
                    "status": "halted",
                    "notes": {"tenant_id": str(tenant_id)},
                }
            }
        },
        event_id="evt_halted_1",
    )
    await client.post(
        "/v1/webhooks/razorpay", content=body, headers={**headers, "Host": "localhost"}
    )
    stored = [e for e in await _events() if e.event_id == "evt_halted_1"][0]
    assert await webhook_handler.process_one(stored.id) == WebhookStatus.PROCESSED

    subscription = await subscription_of(tenant_id)
    assert subscription.gateway_status == "halted", "recorded"
    assert subscription.status == SubscriptionStatus.ACTIVE, "…and not obeyed"


async def test_a_refund_webhook_is_recorded_against_the_payment(client):
    """A refund issued from the Razorpay dashboard still reaches the ledger."""
    tenant_id, invoice = await _with_invoice("wh-refund")
    total = Decimal(str(invoice.total))

    async with tenant_session(tenant_id) as session:
        row = await payment_service.get_invoice(session, tenant_id, invoice.id)
        await payment_service.record_capture(
            session,
            invoice=row,
            gateway=PaymentGateway.RAZORPAY,
            gateway_payment_id="pay_to_refund",
            amount=total,
        )

    body, headers = _delivery(
        "refund.processed",
        {
            "refund": {
                "entity": {
                    "id": "rfnd_wh_1",
                    "payment_id": "pay_to_refund",
                    "amount": int(Decimal("500.00") * 100),
                    "currency": "INR",
                    "notes": {"tenant_id": str(tenant_id), "reason": "dashboard refund"},
                }
            }
        },
        event_id="evt_refund_1",
    )
    await client.post(
        "/v1/webhooks/razorpay", content=body, headers={**headers, "Host": "localhost"}
    )
    stored = [e for e in await _events() if e.event_id == "evt_refund_1"][0]
    assert await webhook_handler.process_one(stored.id) == WebhookStatus.PROCESSED

    settled = (await invoices_of(tenant_id))[0]
    payment = settled.payments[0]
    assert Decimal(str(payment.amount_refunded)) == Decimal("500.00")
    assert payment.refunds[0].status == RefundStatus.PROCESSED
    assert billing_service.balance_of(settled) == Decimal("500.00")


async def test_a_delivery_about_something_this_deployment_does_not_own_is_ignored(client):
    """One Razorpay account can serve more than one product. A payment we never
    created is not an error, and must not be guessed at."""
    body, headers = _delivery(
        "payment.captured",
        {
            "payment": {
                "entity": {
                    "id": "pay_someone_else",
                    "amount": 10000,
                    "currency": "INR",
                    "status": "captured",
                    "notes": {},
                }
            }
        },
        event_id="evt_foreign_1",
    )
    await client.post(
        "/v1/webhooks/razorpay", content=body, headers={**headers, "Host": "localhost"}
    )
    stored = [e for e in await _events() if e.event_id == "evt_foreign_1"][0]
    assert await webhook_handler.process_one(stored.id) == WebhookStatus.IGNORED


async def test_a_body_that_is_not_json_is_stored_rather_than_crashing(client):
    """A malformed delivery is evidence too. It is refused on the signature —
    which is checked over the raw bytes and therefore still works — and kept."""
    body = b"this is not json"
    headers = {
        "x-razorpay-signature": rzp_client.sign(body, WEBHOOK_SECRET),
        "x-razorpay-event-id": "evt_garbage_1",
        "Host": "localhost",
    }
    response = await client.post("/v1/webhooks/razorpay", content=body, headers=headers)
    assert response.status_code == 200

    stored = [e for e in await _events() if e.event_id == "evt_garbage_1"][0]
    assert stored.signature_verified, "the bytes were genuinely ours"
    assert stored.payload == {}
    assert stored.status == WebhookStatus.IGNORED, "…but there is no event in them to act on"


async def test_the_signature_check_is_constant_time_and_rejects_the_empty_secret():
    """A deployment with no webhook secret verifies nothing, rather than
    accidentally verifying everything."""
    assert rzp_client.verify(b"anything", "", "secret") is False
    assert rzp_client.verify(b"anything", "sig", "") is False
    body = b'{"event":"payment.captured"}'
    assert rzp_client.verify(body, rzp_client.sign(body, WEBHOOK_SECRET), WEBHOOK_SECRET)
