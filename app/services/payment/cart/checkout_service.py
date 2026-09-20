"""Turning a cart into one invoice.

Requirement 3 — several modules, a single payment — and the single payment is
not a loop that pays five invoices quickly. It is **one** `subscription_invoice`
carrying one line per module, which is what makes the rest of it true:

*   one gateway order, so the clinic authorises one amount once;
*   all or nothing, because every module is prepared before any invoice exists
    and the whole thing runs in one transaction — a cart holding one module the
    clinic already has rolls back the others rather than charging for them;
*   one thing to reconcile, because when that invoice is paid, everything on it
    was paid together.

`buy_now` is the same code with one module in it. It deliberately does *not*
create a cart row: a clinic buying the lab from its page should not have its
half-filled cart closed underneath it, and `uq_carts_one_open_per_tenant`
would make that the only alternative. What the two paths share is the part
that matters — `prepare` and `settle`, so the money is decided in one place.

Activation happens here rather than on payment, which is the existing
behaviour of this codebase and not a new decision: `addon_service.activate`
has always switched the module on and billed for it afterwards, and the
invoice sits until it is paid. Changing that for the cart alone would mean two
different answers to "when does a module come on".
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict
from app.models.billing import Cart, SubscriptionInvoice
from app.models.enums import PlanInterval
from app.services.payment import billing_service
from app.services.payment.addon import billing_service as addon_service
from app.services.payment.cart import cart_service
from app.services.payment.invoice import generation_service

log = logging.getLogger("pms.billing")


@dataclass
class CheckoutResult:
    """What a checkout did, in enough detail for the API to say so plainly."""

    #: The one invoice every module on this checkout was billed on. None when
    #: there was nothing to charge — every module was free this cycle, or
    #: already paid for. That is a success, not a failure.
    invoice: SubscriptionInvoice | None
    activated: list[str] = field(default_factory=list)
    #: Modules that came back on with no charge because their scheduled
    #: cancellation was dropped. Reported rather than inferred, because
    #: "you were not charged" is the part people query.
    reinstated: list[str] = field(default_factory=list)
    cart: Cart | None = None

    @property
    def charged(self) -> bool:
        return self.invoice is not None


async def _buy(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    feature_keys: list[str],
    *,
    today: date,
) -> CheckoutResult:
    """Switch every module on and bill them all on one invoice.

    The order is the whole point: prepare *everything* first, so a module that
    cannot be bought raises before any invoice exists.
    """
    subscription = await cart_service.subscription_for(session, tenant_id)

    preps = [
        await addon_service.prepare(
            session, tenant_id, key, today=today, subscription=subscription
        )
        for key in feature_keys
    ]

    lines = [prep.line for prep in preps if prep.line is not None]
    invoice = None
    if lines:
        # Every line was prorated over the same cycle from the same day, so one
        # period covers the invoice.
        invoice = await generation_service.generate_one_off(
            session,
            subscription=subscription,
            lines=lines,
            today=today,
            period_start=lines[0].period_start,
            period_end=lines[0].period_end,
        )

    for prep in preps:
        await addon_service.settle(session, prep, invoice=invoice)

    result = CheckoutResult(
        invoice=invoice,
        activated=[prep.feature_key for prep in preps],
        reinstated=[prep.feature_key for prep in preps if prep.reinstated],
    )
    log.info(
        "tenant %s bought %s on %s",
        tenant_id,
        ", ".join(result.activated) or "nothing",
        invoice.number if invoice is not None else "no invoice",
    )
    return result


async def checkout(
    session: AsyncSession, tenant_id: uuid.UUID, *, today: date
) -> CheckoutResult:
    """Buy everything in the open cart, on one invoice and one payment."""
    cart = await cart_service.current(session, tenant_id)
    if cart is None or not cart.items:
        raise Conflict("There is nothing in the cart.")

    keys = sorted(item.feature_key for item in cart.items)
    result = await _buy(session, tenant_id, keys, today=today)

    await cart_service.mark_converted(session, cart, result.invoice)
    result.cart = cart
    return result


async def buy_now(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    feature_key: str,
    *,
    interval: PlanInterval = PlanInterval.MONTHLY,
    today: date,
) -> CheckoutResult:
    """Buy one module straight from its page, leaving the cart alone.

    The interval is validated against the catalogue even though nothing yet
    bills yearly, so that asking for a year of something sold only by the
    month is refused here rather than silently charged as a month.
    """
    subscription = await cart_service.subscription_for(session, tenant_id)
    plan = await billing_service.get_plan(session, subscription.plan_id)
    # The plan's currency, not a cart's — this path opens no cart, so that a
    # clinic buying from a module's page does not have a half-filled one
    # closed underneath it.
    await cart_service.active_price(
        session, feature_key, interval=interval, currency=plan.currency
    )

    return await _buy(session, tenant_id, [feature_key], today=today)


__all__ = ["CheckoutResult", "buy_now", "checkout"]
