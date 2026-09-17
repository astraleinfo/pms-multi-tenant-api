"""Collecting the money, and believing it only when the server has checked.

The rule this module exists to keep: **a browser never decides that an invoice
is paid.** What the checkout widget hands back is a claim, and a claim is
processed in three steps, all of them server-side —

1.  the signature is checked against the key secret, which proves the fields
    were not edited in the browser;
2.  the payment is re-fetched from the gateway, which proves it exists and is
    captured;
3.  the amount and currency are compared against the invoice this system
    raised, which proves it is the right money.

Any one of those failing means nothing is recorded as paid. A valid signature
over a ₹1 payment for a ₹2,499 invoice fails at step three, and that is the
attack step three is there for.

Amounts are never taken from the request. `create_order` prices the order from
the invoice's stored balance, which was computed from the plan and the
entitlements — a request body cannot influence it, because it is not consulted.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.billing import (
    SubscriptionInvoice,
    SubscriptionPayment,
    SubscriptionRefund,
)
from app.models.enums import (
    PaymentGateway,
    RefundStatus,
    SubscriptionInvoiceStatus,
    SubscriptionPaymentStatus,
)
from app.services.payment import billing_service
from app.services.payment.gateway import (
    CheckoutContext,
    GatewayRefused,
    Money,
    SignatureInvalid,
    get_gateway,
)
from app.services.payment.money import ZERO, money, now

log = logging.getLogger("pms.billing")

#: Statuses that mean the gateway is holding money for us.
_SETTLED = (
    SubscriptionPaymentStatus.CAPTURED,
    SubscriptionPaymentStatus.PARTIALLY_REFUNDED,
)


async def get_invoice(
    session: AsyncSession, tenant_id: uuid.UUID, invoice_id: uuid.UUID
) -> SubscriptionInvoice:
    invoice = (
        await session.execute(
            select(SubscriptionInvoice).where(
                SubscriptionInvoice.id == invoice_id,
                SubscriptionInvoice.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if invoice is None:
        raise NotFound("No such invoice.")
    return invoice


# --- opening a payment -------------------------------------------------------


async def create_order(
    session: AsyncSession, tenant_id: uuid.UUID, invoice_id: uuid.UUID
) -> CheckoutContext:
    """Open a payment against an invoice, and return what a browser may see.

    The amount is the invoice's outstanding balance as this system computes it.
    Nothing from the request reaches it. What comes back is the gateway's
    public key id and an order id — both meant to be public — and never a
    secret of any kind.
    """
    invoice = await get_invoice(session, tenant_id, invoice_id)
    if invoice.status in (SubscriptionInvoiceStatus.VOID, SubscriptionInvoiceStatus.DRAFT):
        raise Conflict("That invoice cannot be paid.")

    balance = billing_service.balance_of(invoice)
    if balance <= ZERO:
        raise Conflict("That invoice is already settled.")

    gateway = get_gateway()
    if not gateway.configured:
        raise Conflict("Online payment is not configured for this deployment.")

    account = await billing_service.get_account(session, tenant_id)
    amount = Money(amount=balance, currency=str(invoice.currency))
    order = await gateway.payments.create_order(
        amount=amount,
        receipt=invoice.number,
        notes={"tenant_id": str(tenant_id), "invoice_id": str(invoice.id)},
    )

    # Recorded before the browser is told anything, so a payment that arrives
    # by webhook before the user's tab comes back still finds its row.
    session.add(
        SubscriptionPayment(
            tenant_id=tenant_id,
            invoice_id=invoice.id,
            gateway=PaymentGateway(gateway.name),
            gateway_order_id=order.id,
            amount=balance,
            currency=invoice.currency,
            status=SubscriptionPaymentStatus.CREATED,
        )
    )
    await session.flush()

    return CheckoutContext(
        gateway=gateway.name,
        key_id=gateway.public_key,
        order_id=order.id,
        amount=amount,
        invoice_number=invoice.number,
        customer_name=account.legal_name,
        customer_email=account.billing_email,
        description=f"Invoice {invoice.number}",
    )


# --- believing a payment -----------------------------------------------------


async def verify_and_capture(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    order_id: str,
    payment_id: str,
    signature: str,
) -> SubscriptionInvoice:
    """What the browser comes back with, checked three ways before it counts.

    Raises `SignatureInvalid` when the claim is not signed with our own secret,
    and `Conflict` when the gateway's own record of the payment does not match
    the invoice this system raised. Neither writes a captured payment.
    """
    gateway = get_gateway()
    if not gateway.payments.verify_checkout_signature(
        order_id=order_id, payment_id=payment_id, signature=signature
    ):
        log.warning(
            "rejected a payment claim for tenant %s: signature did not verify", tenant_id
        )
        raise SignatureInvalid("That payment could not be verified.")

    pending = (
        await session.execute(
            select(SubscriptionPayment).where(
                SubscriptionPayment.tenant_id == tenant_id,
                SubscriptionPayment.gateway_order_id == order_id,
            )
        )
    ).scalars().first()
    if pending is None:
        raise NotFound("No payment was opened for that order.")

    invoice = await get_invoice(session, tenant_id, pending.invoice_id)

    # Step two: what does the *gateway* say about this payment? The browser's
    # word is not evidence, and this is the call that replaces it.
    remote = await gateway.payments.fetch(payment_id)
    if not remote.captured:
        raise Conflict("That payment has not been captured.")

    # Step three: is it the right money, for the right invoice?
    if remote.order_id and remote.order_id != order_id:
        raise Conflict("That payment belongs to a different order.")
    if remote.amount.currency.upper() != str(invoice.currency).upper():
        raise Conflict("That payment is in a different currency.")
    if money(remote.amount.amount) != money(pending.amount):
        log.warning(
            "payment %s is %s but order %s was for %s",
            payment_id, remote.amount.amount, order_id, pending.amount,
        )
        raise Conflict("That payment is not for the amount that was requested.")

    await record_capture(
        session,
        invoice=invoice,
        gateway=PaymentGateway(gateway.name),
        gateway_payment_id=remote.id,
        gateway_order_id=order_id,
        amount=remote.amount.amount,
        method=remote.method,
    )
    return invoice


async def record_capture(
    session: AsyncSession,
    *,
    invoice: SubscriptionInvoice,
    gateway: PaymentGateway,
    gateway_payment_id: str,
    amount: Decimal,
    gateway_order_id: str = "",
    method: str = "",
) -> SubscriptionPayment:
    """Write down that money arrived. Idempotent.

    Called from two places that both mean it — the verification path above and
    the webhook handler — and they race by design: whichever arrives first
    records the payment, and the second finds it. The lookup here is the fast
    path; `uq_subscription_payment_gateway_id` is what actually holds.

    The row created by `create_order` is reused when there is one, so an
    invoice does not end up with a dangling `created` payment beside the
    captured one it became.
    """
    existing = (
        await session.execute(
            select(SubscriptionPayment).where(
                SubscriptionPayment.gateway == gateway,
                SubscriptionPayment.gateway_payment_id == gateway_payment_id,
            )
        )
    ).scalar_one_or_none()

    if existing is None and gateway_order_id:
        existing = (
            await session.execute(
                select(SubscriptionPayment).where(
                    SubscriptionPayment.invoice_id == invoice.id,
                    SubscriptionPayment.gateway_order_id == gateway_order_id,
                    SubscriptionPayment.gateway_payment_id.is_(None),
                )
            )
        ).scalars().first()

    if existing is None:
        existing = SubscriptionPayment(
            tenant_id=invoice.tenant_id,
            invoice_id=invoice.id,
            gateway=gateway,
            currency=invoice.currency,
            amount=money(amount),
            # Spelled out rather than left to the column default: the row is
            # read back below before it is flushed, and a `None` there is a
            # `TypeError` rather than a blank.
            method="",
            failure_reason="",
            status=SubscriptionPaymentStatus.CREATED,
        )
        session.add(existing)

    if existing.status in _SETTLED:
        # Already believed. A redelivered webhook lands here and changes
        # nothing, which is the point.
        return existing

    existing.gateway_payment_id = gateway_payment_id
    existing.gateway_order_id = gateway_order_id or existing.gateway_order_id
    existing.amount = money(amount)
    existing.method = (method or existing.method or "")[:40]
    existing.status = SubscriptionPaymentStatus.CAPTURED
    existing.captured_at = now()
    existing.failure_reason = ""
    await session.flush()

    await session.refresh(invoice, ["payments"])
    await settle(session, invoice)
    log.info("invoice %s received %s via %s", invoice.number, amount, gateway.value)
    return existing


async def record_failure(
    session: AsyncSession,
    *,
    invoice: SubscriptionInvoice,
    gateway: PaymentGateway,
    gateway_payment_id: str,
    amount: Decimal,
    reason: str = "",
    gateway_order_id: str = "",
) -> SubscriptionPayment:
    """A payment the gateway says did not work.

    Kept rather than discarded: "three failed attempts then a success" is a
    thing the metrics report and support needs to see.
    """
    existing = (
        await session.execute(
            select(SubscriptionPayment).where(
                SubscriptionPayment.gateway == gateway,
                SubscriptionPayment.gateway_payment_id == gateway_payment_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = SubscriptionPayment(
            tenant_id=invoice.tenant_id,
            invoice_id=invoice.id,
            gateway=gateway,
            gateway_payment_id=gateway_payment_id,
            gateway_order_id=gateway_order_id,
            currency=invoice.currency,
            amount=money(amount),
            method="",
            failure_reason="",
            status=SubscriptionPaymentStatus.CREATED,
        )
        session.add(existing)
    if existing.status in _SETTLED:
        # A failure notice arriving after a capture is not a reason to unpay an
        # invoice. Razorpay sends `payment.failed` for an attempt, not for the
        # payment that succeeded afterwards.
        return existing
    existing.status = SubscriptionPaymentStatus.FAILED
    existing.failure_reason = (reason or "")[:2000]
    await session.flush()
    return existing


async def settle(session: AsyncSession, invoice: SubscriptionInvoice) -> SubscriptionInvoice:
    """Move the invoice's status to whatever the money says it should be.

    Status follows the payments, never the other way round — the same rule the
    patient ledger keeps in `app/services/billing.py`. The freeze trigger
    permits this: a status is not one of an invoice's figures.
    """
    target = billing_service.status_after_payment(invoice)
    if invoice.status != target:
        invoice.status = target
    invoice.paid_at = (
        now() if target == SubscriptionInvoiceStatus.PAID and invoice.paid_at is None
        else invoice.paid_at
    )
    await session.flush()
    return invoice


# --- giving it back ----------------------------------------------------------


async def refund(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    payment_id: uuid.UUID,
    *,
    amount: Decimal | None = None,
    reason: str = "",
) -> SubscriptionRefund:
    """Refund a captured payment, in whole or in part.

    A deliberate act by the platform. Nothing in this package refunds
    automatically — requirement 7 is explicit that cancelling an add-on does
    not, and no other path does either.
    """
    payment = (
        await session.execute(
            select(SubscriptionPayment).where(
                SubscriptionPayment.id == payment_id,
                SubscriptionPayment.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if payment is None:
        raise NotFound("No such payment.")
    if payment.status not in _SETTLED:
        raise Conflict("Only a captured payment can be refunded.")

    remaining = money(payment.amount) - money(payment.amount_refunded)
    requested = money(amount) if amount is not None else remaining
    if requested <= ZERO:
        raise Conflict("A refund must be for more than nothing.")
    if requested > remaining:
        raise Conflict(
            f"That is more than the {remaining} still refundable on this payment.",
            details={"refundable": float(remaining)},
        )

    gateway = get_gateway(payment.gateway.value)
    if not payment.gateway_payment_id:
        raise Conflict("That payment has no gateway reference to refund against.")

    try:
        remote = await gateway.payments.refund(
            payment_id=payment.gateway_payment_id,
            amount=Money(amount=requested, currency=str(payment.currency)),
            notes={"reason": reason[:200]} if reason else None,
        )
    except GatewayRefused as exc:
        log.warning("refund refused for payment %s: %s", payment.id, exc)
        raise

    return await record_refund(
        session,
        payment=payment,
        gateway_refund_id=remote.id,
        amount=requested,
        status=RefundStatus.PROCESSED if remote.status == "processed" else RefundStatus.PENDING,
        reason=reason,
    )


async def record_refund(
    session: AsyncSession,
    *,
    payment: SubscriptionPayment,
    gateway_refund_id: str,
    amount: Decimal,
    status: RefundStatus = RefundStatus.PENDING,
    reason: str = "",
) -> SubscriptionRefund:
    """Write down a refund. Idempotent, by the same means as a capture.

    The payment's `amount_refunded` is only moved when the refund is actually
    processed — a pending refund is money still with us, and counting it early
    would make an invoice look unpaid while the gateway is still thinking.
    """
    existing = (
        await session.execute(
            select(SubscriptionRefund).where(
                SubscriptionRefund.gateway == payment.gateway,
                SubscriptionRefund.gateway_refund_id == gateway_refund_id,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        # Created `pending` whatever the caller asked for, so that the
        # transition below is the *only* place `amount_refunded` moves. A row
        # born `processed` would skip it, and the payment would quietly keep
        # showing the full amount as still ours.
        existing = SubscriptionRefund(
            tenant_id=payment.tenant_id,
            payment_id=payment.id,
            gateway=payment.gateway,
            gateway_refund_id=gateway_refund_id,
            amount=money(amount),
            currency=payment.currency,
            status=RefundStatus.PENDING,
            reason=reason[:2000],
        )
        session.add(existing)
        await session.flush()
    elif existing.status == RefundStatus.PROCESSED:
        return existing

    was_processed = existing.status == RefundStatus.PROCESSED
    existing.status = status
    if status == RefundStatus.PROCESSED and not was_processed:
        existing.processed_at = now()
        payment.amount_refunded = money(payment.amount_refunded) + money(existing.amount)
        payment.status = (
            SubscriptionPaymentStatus.REFUNDED
            if money(payment.amount_refunded) >= money(payment.amount)
            else SubscriptionPaymentStatus.PARTIALLY_REFUNDED
        )
    await session.flush()

    invoice = await session.get(SubscriptionInvoice, payment.invoice_id)
    if invoice is not None:
        await session.refresh(invoice, ["payments"])
        await settle(session, invoice)
    return existing


# --- chasing -----------------------------------------------------------------


async def overdue_invoices(
    session: AsyncSession, tenant_id: uuid.UUID, *, on: date, grace_days: int
) -> list[SubscriptionInvoice]:
    """Invoices past their due date plus the grace period, still owing."""
    from datetime import timedelta

    cutoff = on - timedelta(days=max(grace_days, 0))
    rows = (
        await session.execute(
            select(SubscriptionInvoice).where(
                SubscriptionInvoice.tenant_id == tenant_id,
                SubscriptionInvoice.status.in_(
                    (
                        SubscriptionInvoiceStatus.ISSUED,
                        SubscriptionInvoiceStatus.PARTIALLY_PAID,
                    )
                ),
                SubscriptionInvoice.due_on < cutoff,
            )
        )
    ).scalars()
    return [row for row in rows if billing_service.balance_of(row) > ZERO]


__all__ = [
    "create_order", "get_invoice", "overdue_invoices", "record_capture", "record_failure",
    "record_refund", "refund", "settle", "verify_and_capture",
]
