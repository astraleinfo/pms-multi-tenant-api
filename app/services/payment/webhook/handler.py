"""Webhooks: store everything, verify everything, process each one once.

The shape is deliberately two-phase, and the phases have different jobs.

**Receipt** (`receive`) happens inside the HTTP request and does almost
nothing: verify the signature, write the row, answer 200. Razorpay retries for
twenty-four hours on anything that is not a 2xx, so a handler that does real
work inside the request turns one slow database call into a retry storm. The
row is the queue, exactly as `messages` is the queue for patient messaging.

**Processing** (`process_one`) happens in the billing worker, in its own
transaction, with the clinic bound. It is safe to run twice: every action it
takes is idempotent underneath — a capture collides on
`uq_subscription_payment_gateway_id`, a refund on its own unique index, a
status change is a no-op the second time.

Five properties the requirements asked for, and where each one actually lives:

*   *verify signatures* — `gateway.parse_webhook`, against the raw bytes;
*   *store every webhook* — `receive`, before anything is believed, including
    the ones that fail verification;
*   *idempotent processing* — `uq_webhook_event_id` plus the per-action indexes;
*   *prevent duplicate processing* — `FOR UPDATE SKIP LOCKED` on the claim;
*   *audit failures* — `status`, `attempts` and `error` on the row, plus an
    `audit_events` line for every refused signature.

And the sixth, which is not a property of this file but of the whole design:
*never trust frontend payment status*. A webhook is how an invoice becomes
paid. The browser's word is checked against the gateway before it counts, in
`payment_service.verify_and_capture`, and both paths end at the same
`record_capture`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import session_factory, set_tenant
from app.models.billing import (
    Subscription,
    SubscriptionInvoice,
    SubscriptionPayment,
    WebhookEvent,
)
from app.models.enums import (
    PaymentGateway,
    RefundStatus,
    SubscriptionStatus,
    TenantStatus,
    WebhookStatus,
)
from app.models.tenancy import Tenant
from app.services import audit
from app.services.payment import payment_service
from app.services.payment.gateway import get_gateway
from app.services.payment.money import from_minor_units, now

log = logging.getLogger("pms.billing.webhook")


# --- phase one: receipt ------------------------------------------------------


async def receive(
    session: AsyncSession,
    *,
    body: bytes,
    headers: dict[str, str],
    gateway_name: str | None = None,
) -> tuple[WebhookEvent, bool]:
    """Store one delivery. Returns `(event, created)`.

    `created` is False for a redelivery, and that is the normal case rather
    than an error — Razorpay resends until it gets a 2xx, and it is entitled to
    get one for something we have already got.

    A delivery whose signature does not verify is **stored and refused**:
    `rejected`, never processed, and audited. Discarding it would erase the
    evidence of the one delivery most worth keeping, and processing it would
    mean a stranger can mark invoices paid.
    """
    gateway = get_gateway(gateway_name)
    envelope = gateway.parse_webhook(body=body, headers=headers)

    existing = (
        await session.execute(
            select(WebhookEvent).where(
                WebhookEvent.gateway == PaymentGateway(gateway.name),
                WebhookEvent.event_id == envelope.event_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        log.info("webhook %s redelivered; already stored as %s", envelope.event_id, existing.status)
        return existing, False

    from app.services.payment.razorpay import SUPPORTED_EVENTS

    if envelope.signature_verified:
        status = (
            WebhookStatus.RECEIVED
            if envelope.event_type in SUPPORTED_EVENTS
            else WebhookStatus.IGNORED
        )
    else:
        status = WebhookStatus.REJECTED

    event = WebhookEvent(
        gateway=PaymentGateway(gateway.name),
        event_id=envelope.event_id,
        event_type=envelope.event_type,
        entity_id=envelope.entity_id,
        signature_verified=envelope.signature_verified,
        status=status,
        payload=envelope.payload,
        error="" if envelope.signature_verified else "signature did not verify",
        processed_at=now() if status == WebhookStatus.IGNORED else None,
    )
    session.add(event)
    await session.flush()

    if not envelope.signature_verified:
        # No tenant is bound here, and none can be: a delivery that did not
        # verify names nothing we should believe. The trail records the attempt
        # against the platform.
        await audit.record(
            session,
            action="billing.webhook_rejected",
            actor_kind="gateway",
            actor_label=gateway.name,
            entity="webhook_event",
            entity_id=str(event.id),
            outcome="refused",
            meta={"event_type": envelope.event_type, "event_id": envelope.event_id},
        )
        log.warning(
            "refused a %s webhook: signature did not verify (event %s)",
            gateway.name, envelope.event_id,
        )

    return event, True


# --- phase two: processing ---------------------------------------------------


async def claim_pending(session: AsyncSession, *, limit: int = 20) -> list[WebhookEvent]:
    """Take the next few unprocessed deliveries, locking them.

    `SKIP LOCKED` is what lets two workers run without either waiting for the
    other or both taking the same row. A delivery that has failed too many
    times is left alone and waits for somebody to look at it — spinning on a
    poisonous payload would bury the ones behind it.
    """
    rows = (
        await session.execute(
            select(WebhookEvent)
            .where(
                WebhookEvent.status == WebhookStatus.RECEIVED,
                WebhookEvent.signature_verified.is_(True),
                WebhookEvent.attempts < settings.webhook_max_attempts,
            )
            .order_by(WebhookEvent.received_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars()
    return list(rows)


async def process_one(event_id: uuid.UUID) -> WebhookStatus:
    """Process one stored delivery, in its own transaction.

    The clinic is resolved first and bound for the length of the work, so the
    webhook path goes through exactly the same row-level security as a request
    does. There is no privileged read anywhere in this file.
    """
    async with session_factory() as session:
        async with session.begin():
            event = await session.get(WebhookEvent, event_id)
            if event is None:  # pragma: no cover - deleted mid-flight
                return WebhookStatus.IGNORED
            if event.status != WebhookStatus.RECEIVED:
                return WebhookStatus(event.status)

            event.attempts += 1
            try:
                status = await _dispatch(session, event)
            except Exception as exc:  # noqa: BLE001 - a bad payload must not kill the worker
                event.status = (
                    WebhookStatus.FAILED
                    if event.attempts >= settings.webhook_max_attempts
                    else WebhookStatus.RECEIVED
                )
                event.error = f"{type(exc).__name__}: {exc}"[:2000]
                log.exception("webhook %s failed on attempt %s", event.event_id, event.attempts)
                return WebhookStatus(event.status)

            event.status = status
            event.error = ""
            event.processed_at = now()
            return status


async def _dispatch(session: AsyncSession, event: WebhookEvent) -> WebhookStatus:
    """Route one delivery to the thing that knows what it means."""
    from app.services.payment.razorpay import SUPPORTED_EVENTS

    if event.event_type not in SUPPORTED_EVENTS:
        return WebhookStatus.IGNORED

    tenant_id = await _resolve_tenant(session, event)
    if tenant_id is None:
        # Not ours. Razorpay accounts can serve more than one product, and a
        # delivery about something this system did not create is not a failure.
        log.info("webhook %s names nothing this deployment owns", event.event_id)
        return WebhookStatus.IGNORED

    await set_tenant(session, tenant_id)
    family = event.event_type.split(".", 1)[0]
    handler = {
        "payment": _handle_payment,
        "subscription": _handle_subscription,
        "invoice": _handle_hosted_invoice,
        "refund": _handle_refund,
    }.get(family)
    if handler is None:  # pragma: no cover - guarded by SUPPORTED_EVENTS
        return WebhookStatus.IGNORED

    status = await handler(session, event, tenant_id)
    await session.flush()
    await set_tenant(session, None)
    return status


# --- finding the clinic ------------------------------------------------------


def _entity(event: WebhookEvent, kind: str) -> dict[str, Any]:
    return ((event.payload.get("payload") or {}).get(kind) or {}).get("entity") or {}


def _note_tenant(entity: dict[str, Any]) -> uuid.UUID | None:
    raw = (entity.get("notes") or {}).get("tenant_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


async def _resolve_tenant(session: AsyncSession, event: WebhookEvent) -> uuid.UUID | None:
    """Which clinic this delivery is about.

    The fast path is the notes: every order, subscription and hosted invoice
    this system creates is stamped with `notes.tenant_id`, and Razorpay copies
    an order's notes onto the payment made against it. That covers everything
    we started.

    The slow path is for things we did not start — a refund issued from the
    Razorpay dashboard, most often. There is no privileged cross-tenant read in
    this codebase and this is not where one gets introduced, so the fallback
    binds each clinic in turn and asks whether the gateway id is theirs, the
    same way `platform.snapshot` counts across clinics. It is O(clinics) and it
    is logged, because it should be rare.
    """
    payload = event.payload.get("payload") or {}
    for kind in ("payment", "subscription", "invoice", "refund", "order"):
        found = _note_tenant((payload.get(kind) or {}).get("entity") or {})
        if found is not None:
            return found

    gateway_ids = [
        str(((payload.get(kind) or {}).get("entity") or {}).get("id") or "")
        for kind in ("payment", "subscription", "invoice", "refund")
    ]
    gateway_ids = [value for value in gateway_ids if value]
    payment_id = str((_entity(event, "refund")).get("payment_id") or "")
    if payment_id:
        gateway_ids.append(payment_id)
    if not gateway_ids:
        return None

    log.info("webhook %s carries no tenant note; searching by gateway id", event.event_id)
    tenants = list(
        (
            await session.execute(
                select(Tenant.id).where(Tenant.status != TenantStatus.SUSPENDED)
            )
        ).scalars()
    )
    for tenant_id in tenants:
        await set_tenant(session, tenant_id)
        hit = (
            await session.execute(
                select(SubscriptionPayment.id).where(
                    SubscriptionPayment.gateway_payment_id.in_(gateway_ids)
                    | SubscriptionPayment.gateway_order_id.in_(gateway_ids)
                )
            )
        ).scalars().first()
        if hit is None:
            hit = (
                await session.execute(
                    select(Subscription.id).where(
                        Subscription.razorpay_subscription_id.in_(gateway_ids)
                    )
                )
            ).scalars().first()
        if hit is None:
            hit = (
                await session.execute(
                    select(SubscriptionInvoice.id).where(
                        SubscriptionInvoice.razorpay_invoice_id.in_(gateway_ids)
                    )
                )
            ).scalars().first()
        if hit is not None:
            await set_tenant(session, None)
            return tenant_id
    await set_tenant(session, None)
    return None


# --- what each event means ---------------------------------------------------


async def _invoice_for(
    session: AsyncSession, tenant_id: uuid.UUID, entity: dict[str, Any]
) -> SubscriptionInvoice | None:
    """The local invoice a payment entity is settling.

    By the `invoice_id` note first — the one we stamped when the order was
    created — and by the order id otherwise, which covers a payment that
    arrived against an order some earlier attempt opened.
    """
    raw = (entity.get("notes") or {}).get("invoice_id")
    if raw:
        try:
            invoice = await session.get(SubscriptionInvoice, uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            invoice = None
        if invoice is not None and invoice.tenant_id == tenant_id:
            return invoice

    order_id = str(entity.get("order_id") or "")
    if order_id:
        payment = (
            await session.execute(
                select(SubscriptionPayment).where(
                    SubscriptionPayment.tenant_id == tenant_id,
                    SubscriptionPayment.gateway_order_id == order_id,
                )
            )
        ).scalars().first()
        if payment is not None:
            return await session.get(SubscriptionInvoice, payment.invoice_id)
    return None


async def _handle_payment(
    session: AsyncSession, event: WebhookEvent, tenant_id: uuid.UUID
) -> WebhookStatus:
    """`payment.captured` and `payment.failed`.

    This is the authoritative path: an invoice becomes paid here, from what the
    gateway said, and not from anything a browser reported.
    """
    entity = _entity(event, "payment")
    payment_id = str(entity.get("id") or "")
    if not payment_id:
        return WebhookStatus.IGNORED

    invoice = await _invoice_for(session, tenant_id, entity)
    if invoice is None:
        log.info("payment %s does not match a local invoice", payment_id)
        return WebhookStatus.IGNORED

    amount = from_minor_units(entity.get("amount", 0))
    if event.event_type == "payment.captured":
        await payment_service.record_capture(
            session,
            invoice=invoice,
            gateway=PaymentGateway(event.gateway),
            gateway_payment_id=payment_id,
            gateway_order_id=str(entity.get("order_id") or ""),
            amount=amount,
            method=str(entity.get("method") or ""),
        )
        await _activate_on_first_payment(session, invoice)
    else:
        await payment_service.record_failure(
            session,
            invoice=invoice,
            gateway=PaymentGateway(event.gateway),
            gateway_payment_id=payment_id,
            gateway_order_id=str(entity.get("order_id") or ""),
            amount=amount,
            reason=str(entity.get("error_description") or entity.get("error_reason") or ""),
        )
    return WebhookStatus.PROCESSED


async def _activate_on_first_payment(
    session: AsyncSession, invoice: SubscriptionInvoice
) -> None:
    """A paid invoice activates a pending subscription, and clears a past-due one."""
    from app.services.payment import subscription_service

    subscription = await session.get(Subscription, invoice.subscription_id)
    if subscription is None:  # pragma: no cover - FK prevents this
        return
    if subscription.status == SubscriptionStatus.PENDING:
        await subscription_service.activate(session, subscription)
    elif subscription.status == SubscriptionStatus.PAST_DUE:
        await subscription_service.clear_past_due(session, subscription)


async def _handle_subscription(
    session: AsyncSession, event: WebhookEvent, tenant_id: uuid.UUID
) -> WebhookStatus:
    """Subscription lifecycle, recorded but not obeyed.

    The gateway's status goes into `gateway_status`; our own `status` is left
    alone. The billing engine knows what it invoiced and what was paid, and a
    webhook saying "halted" is information for whoever looks at the console —
    not permission to stop serving a clinic.
    """
    entity = _entity(event, "subscription")
    remote_id = str(entity.get("id") or "")
    if not remote_id:
        return WebhookStatus.IGNORED

    subscription = (
        await session.execute(
            select(Subscription).where(
                Subscription.tenant_id == tenant_id,
                Subscription.razorpay_subscription_id == remote_id,
            )
        )
    ).scalars().first()
    if subscription is None:
        return WebhookStatus.IGNORED

    subscription.gateway_status = str(entity.get("status") or event.event_type)[:40]
    subscription.gateway_synced_at = now()
    await session.flush()
    return WebhookStatus.PROCESSED


async def _handle_hosted_invoice(
    session: AsyncSession, event: WebhookEvent, tenant_id: uuid.UUID
) -> WebhookStatus:
    """`invoice.*` — the hosted page's own view of one of our invoices.

    `invoice.paid` carries the payment that settled it, so it takes the same
    path as `payment.captured` and lands on the same idempotent `record_capture`
    — whichever of the two arrives first does the work.
    """
    entity = _entity(event, "invoice")
    remote_id = str(entity.get("id") or "")
    if not remote_id:
        return WebhookStatus.IGNORED

    invoice = (
        await session.execute(
            select(SubscriptionInvoice).where(
                SubscriptionInvoice.tenant_id == tenant_id,
                SubscriptionInvoice.razorpay_invoice_id == remote_id,
            )
        )
    ).scalars().first()
    if invoice is None:
        return WebhookStatus.IGNORED

    payment_entity = _entity(event, "payment")
    payment_id = str(payment_entity.get("id") or "")
    if event.event_type in ("invoice.paid", "invoice.partially_paid") and payment_id:
        await payment_service.record_capture(
            session,
            invoice=invoice,
            gateway=PaymentGateway(event.gateway),
            gateway_payment_id=payment_id,
            gateway_order_id=str(payment_entity.get("order_id") or ""),
            amount=from_minor_units(payment_entity.get("amount", 0)),
            method=str(payment_entity.get("method") or ""),
        )
        await _activate_on_first_payment(session, invoice)
    return WebhookStatus.PROCESSED


async def _handle_refund(
    session: AsyncSession, event: WebhookEvent, tenant_id: uuid.UUID
) -> WebhookStatus:
    """`refund.*`, including refunds issued from the Razorpay dashboard.

    A refund we did not start is still recorded against the payment it came
    from, so the invoice's balance and the metrics stay true whoever pressed
    the button.
    """
    entity = _entity(event, "refund")
    refund_id = str(entity.get("id") or "")
    payment_id = str(entity.get("payment_id") or "")
    if not refund_id or not payment_id:
        return WebhookStatus.IGNORED

    payment = (
        await session.execute(
            select(SubscriptionPayment).where(
                SubscriptionPayment.tenant_id == tenant_id,
                SubscriptionPayment.gateway_payment_id == payment_id,
            )
        )
    ).scalars().first()
    if payment is None:
        return WebhookStatus.IGNORED

    status = {
        "refund.created": RefundStatus.PENDING,
        "refund.processed": RefundStatus.PROCESSED,
        "refund.failed": RefundStatus.FAILED,
    }.get(event.event_type, RefundStatus.PENDING)

    await payment_service.record_refund(
        session,
        payment=payment,
        gateway_refund_id=refund_id,
        amount=from_minor_units(entity.get("amount", 0)),
        status=status,
        reason=str((entity.get("notes") or {}).get("reason") or ""),
    )
    return WebhookStatus.PROCESSED


__all__ = ["claim_pending", "process_one", "receive"]
