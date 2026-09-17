"""Payments, refunds, and the rule that a browser is never believed.

The gateway is replaced with a stub that records what it was asked and answers
from a script. That is the only way to test the property that matters: the
server *re-fetches* the payment and compares the amount, so a browser that
lies — a correct signature over a smaller payment, a payment belonging to
another order — must be refused even though everything it sent was
well-formed.

The signature check is real. `verify_checkout` is the production function,
signing with the real HMAC over the real key secret; the stub only replaces the
HTTP calls.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import Conflict, NotFound
from app.db.session import tenant_session
from app.models.billing import SubscriptionPayment
from app.models.enums import (
    PaymentGateway,
    RefundStatus,
    SubscriptionInvoiceStatus,
    SubscriptionPaymentStatus,
    SubscriptionStatus,
)
from app.services.payment import billing_service, payment_service, subscription_service
from app.services.payment.gateway import Money, SignatureInvalid
from app.services.payment.razorpay import client as rzp_client
from tests.billing_helpers import STARTER, invoices_of, make_tenant, plan, subscription_of

#: The billing catalogue — plans, plan features and tax rates — is seeded, and
#: the seeder truncates first. Declaring the dependency means this module gets a
#: clean database whether it is run alone or with the rest of the suite; without
#: it, a rerun would find the previous run's payments still there.
pytestmark = pytest.mark.usefixtures("seeded")

JAN_1 = date(2026, 1, 1)

KEY_ID = "rzp_test_key"
KEY_SECRET = "rzp_test_secret"  # noqa: S105 - a test fixture, not a credential


class StubPayments:
    """Stands in for `RazorpayPaymentService`. Records, and answers to script."""

    def __init__(self) -> None:
        self.orders: list[dict] = []
        self.refunds: list[dict] = []
        self.payments: dict[str, object] = {}
        self._next_order = 0

    async def create_order(self, *, amount, receipt, notes=None):
        from app.services.payment.gateway import OrderRef

        self._next_order += 1
        order_id = f"order_{self._next_order:04d}"
        self.orders.append({"id": order_id, "amount": amount, "receipt": receipt, "notes": notes})
        return OrderRef(id=order_id, amount=amount, receipt=receipt)

    async def fetch(self, payment_id: str):
        if payment_id not in self.payments:
            raise NotFound(f"no such stub payment {payment_id}")
        return self.payments[payment_id]

    def verify_checkout_signature(self, *, order_id, payment_id, signature) -> bool:
        # The real function, over the real secret — only the HTTP is stubbed.
        return rzp_client.verify_checkout(
            order_id=order_id, payment_id=payment_id, signature=signature
        )

    async def refund(self, *, payment_id, amount=None, notes=None):
        from app.services.payment.gateway import RefundRef

        refund_id = f"rfnd_{len(self.refunds) + 1:04d}"
        self.refunds.append({"payment": payment_id, "amount": amount, "notes": notes})
        return RefundRef(
            id=refund_id,
            payment_id=payment_id,
            amount=amount or Money(amount=Decimal("0"), currency="INR"),
            status="processed",
        )


class StubGateway:
    name = "razorpay"
    configured = True
    public_key = KEY_ID

    def __init__(self) -> None:
        self.payments = StubPayments()
        self.customers = None
        self.subscriptions = None
        self.invoices = None

    def verify_webhook(self, *, body, signature):  # pragma: no cover - webhook tests use the real one
        return rzp_client.verify_webhook(body=body, signature=signature)


def _captured(payment_id: str, amount: Decimal, order_id: str, currency: str = "INR"):
    from app.services.payment.gateway import PaymentRef

    return PaymentRef(
        id=payment_id,
        amount=Money(amount=amount, currency=currency),
        status="captured",
        captured=True,
        method="upi",
        order_id=order_id,
    )


@pytest.fixture
def gateway(monkeypatch):
    """A stubbed gateway with real credentials configured.

    Patched at `payment_service`'s own reference, so the seam is exercised the
    way production uses it rather than bypassed.
    """
    monkeypatch.setattr(settings, "razorpay_key_id", KEY_ID)
    monkeypatch.setattr(settings, "razorpay_key_secret", KEY_SECRET)
    stub = StubGateway()
    monkeypatch.setattr(payment_service, "get_gateway", lambda name=None: stub)
    return stub


async def _with_invoice(label: str):
    tenant_id = await make_tenant(label)
    starter = await plan(STARTER)
    async with tenant_session(tenant_id) as session:
        await subscription_service.subscribe(
            session, tenant_id, plan=starter, today=JAN_1, trial_days=0
        )
    invoice = (await invoices_of(tenant_id))[0]
    return tenant_id, invoice


def _sign(order_id: str, payment_id: str) -> str:
    return rzp_client.sign(f"{order_id}|{payment_id}", KEY_SECRET)


async def test_checkout_prices_the_order_from_the_invoice_not_the_request(gateway):
    """The amount is never an input. It is the balance this server computed."""
    tenant_id, invoice = await _with_invoice("pay-order")

    async with tenant_session(tenant_id) as session:
        context = await payment_service.create_order(session, tenant_id, invoice.id)

    assert context.amount.amount == Decimal(str(invoice.total))
    assert gateway.payments.orders[0]["amount"].amount == Decimal(str(invoice.total))
    assert gateway.payments.orders[0]["receipt"] == invoice.number
    # The notes are what lets a webhook find the clinic without a cross-tenant read.
    assert gateway.payments.orders[0]["notes"]["tenant_id"] == str(tenant_id)

    # Only the public half of the credential pair reaches the browser.
    assert context.key_id == KEY_ID
    assert KEY_SECRET not in repr(context)


async def test_a_verified_payment_settles_the_invoice(gateway):
    tenant_id, invoice = await _with_invoice("pay-verify")

    async with tenant_session(tenant_id) as session:
        context = await payment_service.create_order(session, tenant_id, invoice.id)

    gateway.payments.payments["pay_ok"] = _captured(
        "pay_ok", Decimal(str(invoice.total)), context.order_id
    )

    async with tenant_session(tenant_id) as session:
        settled = await payment_service.verify_and_capture(
            session,
            tenant_id,
            order_id=context.order_id,
            payment_id="pay_ok",
            signature=_sign(context.order_id, "pay_ok"),
        )
        assert settled.status == SubscriptionInvoiceStatus.PAID

    refreshed = (await invoices_of(tenant_id))[0]
    assert refreshed.status == SubscriptionInvoiceStatus.PAID
    assert billing_service.balance_of(refreshed) == Decimal("0.00")
    assert [p.status for p in refreshed.payments] == [SubscriptionPaymentStatus.CAPTURED]


async def test_a_forged_signature_is_refused(gateway):
    """Step one of three. Nothing is written."""
    tenant_id, invoice = await _with_invoice("pay-forged")
    async with tenant_session(tenant_id) as session:
        context = await payment_service.create_order(session, tenant_id, invoice.id)
    gateway.payments.payments["pay_forged"] = _captured(
        "pay_forged", Decimal(str(invoice.total)), context.order_id
    )

    with pytest.raises(SignatureInvalid):
        async with tenant_session(tenant_id) as session:
            await payment_service.verify_and_capture(
                session,
                tenant_id,
                order_id=context.order_id,
                payment_id="pay_forged",
                signature="0" * 64,
            )

    assert (await invoices_of(tenant_id))[0].status == SubscriptionInvoiceStatus.ISSUED


async def test_a_correctly_signed_claim_for_the_wrong_amount_is_refused(gateway):
    """Step three, and the reason step three exists.

    The signature is genuine — it is computed here with the real secret — and
    the payment really is captured at the gateway. It is simply for ₹1. A
    system that stopped after verifying the signature would mark a ₹2,999
    invoice paid.
    """
    tenant_id, invoice = await _with_invoice("pay-underpaid")
    async with tenant_session(tenant_id) as session:
        context = await payment_service.create_order(session, tenant_id, invoice.id)

    gateway.payments.payments["pay_cheap"] = _captured(
        "pay_cheap", Decimal("1.00"), context.order_id
    )

    with pytest.raises(Conflict, match="not for the amount"):
        async with tenant_session(tenant_id) as session:
            await payment_service.verify_and_capture(
                session,
                tenant_id,
                order_id=context.order_id,
                payment_id="pay_cheap",
                signature=_sign(context.order_id, "pay_cheap"),
            )

    assert (await invoices_of(tenant_id))[0].status == SubscriptionInvoiceStatus.ISSUED


async def test_a_payment_the_gateway_has_not_captured_is_refused(gateway):
    """`authorized` is permission to take the money, not the money."""
    from app.services.payment.gateway import PaymentRef

    tenant_id, invoice = await _with_invoice("pay-authorised")
    async with tenant_session(tenant_id) as session:
        context = await payment_service.create_order(session, tenant_id, invoice.id)

    gateway.payments.payments["pay_auth"] = PaymentRef(
        id="pay_auth",
        amount=Money(amount=Decimal(str(invoice.total)), currency="INR"),
        status="authorized",
        captured=False,
        order_id=context.order_id,
    )

    with pytest.raises(Conflict, match="not been captured"):
        async with tenant_session(tenant_id) as session:
            await payment_service.verify_and_capture(
                session,
                tenant_id,
                order_id=context.order_id,
                payment_id="pay_auth",
                signature=_sign(context.order_id, "pay_auth"),
            )


async def test_a_payment_belonging_to_another_order_is_refused(gateway):
    tenant_id, invoice = await _with_invoice("pay-wrong-order")
    async with tenant_session(tenant_id) as session:
        context = await payment_service.create_order(session, tenant_id, invoice.id)

    gateway.payments.payments["pay_elsewhere"] = _captured(
        "pay_elsewhere", Decimal(str(invoice.total)), "order_somewhere_else"
    )

    with pytest.raises(Conflict, match="different order"):
        async with tenant_session(tenant_id) as session:
            await payment_service.verify_and_capture(
                session,
                tenant_id,
                order_id=context.order_id,
                payment_id="pay_elsewhere",
                signature=_sign(context.order_id, "pay_elsewhere"),
            )


async def test_the_same_payment_cannot_be_recorded_twice(gateway):
    """Idempotency, by the unique index on `(gateway, gateway_payment_id)`.

    The verification path and the webhook path both end here, and they race by
    design — so recording twice has to be a no-op rather than a second payment.
    """
    tenant_id, invoice = await _with_invoice("pay-idempotent")
    async with tenant_session(tenant_id) as session:
        context = await payment_service.create_order(session, tenant_id, invoice.id)
    gateway.payments.payments["pay_twice"] = _captured(
        "pay_twice", Decimal(str(invoice.total)), context.order_id
    )
    signature = _sign(context.order_id, "pay_twice")

    for _ in range(3):
        async with tenant_session(tenant_id) as session:
            await payment_service.verify_and_capture(
                session,
                tenant_id,
                order_id=context.order_id,
                payment_id="pay_twice",
                signature=signature,
            )

    async with tenant_session(tenant_id) as session:
        rows = (
            await session.execute(
                select(SubscriptionPayment).where(SubscriptionPayment.tenant_id == tenant_id)
            )
        ).scalars().all()
    assert len(rows) == 1, "one gateway payment is one row, however many times it is reported"

    refreshed = (await invoices_of(tenant_id))[0]
    assert billing_service.paid_amount(refreshed) == Decimal(str(invoice.total))


async def test_a_duplicate_gateway_payment_id_is_refused_by_the_database(gateway):
    """The index is not per tenant, so the same gateway payment cannot be
    booked to two clinics either."""
    from sqlalchemy.exc import IntegrityError

    tenant_id, invoice = await _with_invoice("pay-unique")

    async with tenant_session(tenant_id) as session:
        session.add(
            SubscriptionPayment(
                tenant_id=tenant_id,
                invoice_id=invoice.id,
                gateway=PaymentGateway.RAZORPAY,
                gateway_payment_id="pay_unique",
                amount=Decimal("10.00"),
            )
        )

    with pytest.raises(IntegrityError):
        async with tenant_session(tenant_id) as session:
            session.add(
                SubscriptionPayment(
                    tenant_id=tenant_id,
                    invoice_id=invoice.id,
                    gateway=PaymentGateway.RAZORPAY,
                    gateway_payment_id="pay_unique",
                    amount=Decimal("10.00"),
                )
            )
            await session.flush()


async def test_a_partial_payment_leaves_the_invoice_part_paid(gateway):
    tenant_id, invoice = await _with_invoice("pay-partial")
    half = (Decimal(str(invoice.total)) / 2).quantize(Decimal("0.01"))

    async with tenant_session(tenant_id) as session:
        row = await payment_service.get_invoice(session, tenant_id, invoice.id)
        await payment_service.record_capture(
            session,
            invoice=row,
            gateway=PaymentGateway.RAZORPAY,
            gateway_payment_id="pay_half",
            amount=half,
        )

    refreshed = (await invoices_of(tenant_id))[0]
    assert refreshed.status == SubscriptionInvoiceStatus.PARTIALLY_PAID
    assert billing_service.balance_of(refreshed) == Decimal(str(invoice.total)) - half


async def test_a_failed_payment_is_kept_and_does_not_unpay_an_invoice(gateway):
    """Razorpay reports a failed *attempt*, not a reversal of a later success.

    Three failures then a capture is a real sequence, and the failures are kept
    because the metrics and support both need them.
    """
    tenant_id, invoice = await _with_invoice("pay-failed")

    async with tenant_session(tenant_id) as session:
        row = await payment_service.get_invoice(session, tenant_id, invoice.id)
        await payment_service.record_failure(
            session,
            invoice=row,
            gateway=PaymentGateway.RAZORPAY,
            gateway_payment_id="pay_bad",
            amount=Decimal(str(invoice.total)),
            reason="card declined",
        )
        await payment_service.record_capture(
            session,
            invoice=row,
            gateway=PaymentGateway.RAZORPAY,
            gateway_payment_id="pay_good",
            amount=Decimal(str(invoice.total)),
        )

    refreshed = (await invoices_of(tenant_id))[0]
    assert refreshed.status == SubscriptionInvoiceStatus.PAID
    assert {p.status for p in refreshed.payments} == {
        SubscriptionPaymentStatus.FAILED,
        SubscriptionPaymentStatus.CAPTURED,
    }

    # A late failure notice about the captured payment changes nothing.
    async with tenant_session(tenant_id) as session:
        row = await payment_service.get_invoice(session, tenant_id, invoice.id)
        await payment_service.record_failure(
            session,
            invoice=row,
            gateway=PaymentGateway.RAZORPAY,
            gateway_payment_id="pay_good",
            amount=Decimal(str(invoice.total)),
            reason="late notice",
        )
    assert (await invoices_of(tenant_id))[0].status == SubscriptionInvoiceStatus.PAID


async def test_a_refund_moves_the_invoice_back_to_owing(gateway):
    tenant_id, invoice = await _with_invoice("pay-refund")
    total = Decimal(str(invoice.total))

    async with tenant_session(tenant_id) as session:
        row = await payment_service.get_invoice(session, tenant_id, invoice.id)
        payment = await payment_service.record_capture(
            session,
            invoice=row,
            gateway=PaymentGateway.RAZORPAY,
            gateway_payment_id="pay_refundable",
            amount=total,
        )
        payment_id = payment.id

    async with tenant_session(tenant_id) as session:
        refund = await payment_service.refund(
            session, tenant_id, payment_id, amount=Decimal("500.00"), reason="goodwill"
        )
        assert refund.status == RefundStatus.PROCESSED

    refreshed = (await invoices_of(tenant_id))[0]
    payment = refreshed.payments[0]
    assert payment.status == SubscriptionPaymentStatus.PARTIALLY_REFUNDED
    assert Decimal(str(payment.amount_refunded)) == Decimal("500.00")
    assert billing_service.balance_of(refreshed) == Decimal("500.00")
    assert refreshed.status == SubscriptionInvoiceStatus.PARTIALLY_PAID


async def test_a_refund_cannot_exceed_what_is_left(gateway):
    tenant_id, invoice = await _with_invoice("pay-over-refund")
    total = Decimal(str(invoice.total))

    async with tenant_session(tenant_id) as session:
        row = await payment_service.get_invoice(session, tenant_id, invoice.id)
        payment = await payment_service.record_capture(
            session,
            invoice=row,
            gateway=PaymentGateway.RAZORPAY,
            gateway_payment_id="pay_capped",
            amount=total,
        )
        payment_id = payment.id

    with pytest.raises(Conflict, match="more than the"):
        async with tenant_session(tenant_id) as session:
            await payment_service.refund(
                session, tenant_id, payment_id, amount=total + Decimal("1.00")
            )


async def test_a_settled_invoice_cannot_open_another_order(gateway):
    tenant_id, invoice = await _with_invoice("pay-settled")
    async with tenant_session(tenant_id) as session:
        row = await payment_service.get_invoice(session, tenant_id, invoice.id)
        await payment_service.record_capture(
            session,
            invoice=row,
            gateway=PaymentGateway.RAZORPAY,
            gateway_payment_id="pay_done",
            amount=Decimal(str(invoice.total)),
        )

    with pytest.raises(Conflict, match="already settled"):
        async with tenant_session(tenant_id) as session:
            await payment_service.create_order(session, tenant_id, invoice.id)


async def test_paying_the_first_invoice_activates_a_pending_subscription(gateway):
    """A subscription waiting on its first payment goes live when it arrives —
    from the server's own record of the capture, never from a browser."""
    tenant_id, invoice = await _with_invoice("pay-activates")

    async with tenant_session(tenant_id) as session:
        subscription = await billing_service.require_subscription(
            session, tenant_id, for_update=True
        )
        subscription.status = SubscriptionStatus.PENDING

    async with tenant_session(tenant_id) as session:
        row = await payment_service.get_invoice(session, tenant_id, invoice.id)
        await payment_service.record_capture(
            session,
            invoice=row,
            gateway=PaymentGateway.RAZORPAY,
            gateway_payment_id="pay_activates",
            amount=Decimal(str(invoice.total)),
        )
        subscription = await billing_service.require_subscription(session, tenant_id)
        await subscription_service.activate(session, subscription)

    assert (await subscription_of(tenant_id)).status == SubscriptionStatus.ACTIVE
