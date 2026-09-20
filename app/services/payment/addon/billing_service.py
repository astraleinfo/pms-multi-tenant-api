"""Switching a module on and off, and what that costs.

Requirements 5 to 8 live here, and they are worth restating as the four things
this file guarantees:

*   **On, on the first day of the cycle** — the full monthly charge. Not a
    special case: `prorate` over `[period_start, period_end)` from
    `period_start` is the whole month by construction.
*   **On, mid-cycle** — a prorated charge over the days the cycle actually has,
    billed immediately on a one-off invoice rather than held until renewal.
*   **Off** — the feature stays usable until the end of the cycle already paid
    for. No refund, no credit, and nothing on the next invoice. The entitlement
    row stays `active` with `cancel_at` set; the renewal job is what finally
    switches it off.
*   **On again, before the cycle ends** — the scheduled cancellation is simply
    dropped. Nothing is charged, because the clinic never stopped paying for
    the period they are in. That is the whole of requirement 8, and it works
    because cancelling never took the money back in the first place.

The entitlement row is `tenant_features` — the same row the feature guard in
`app/api/deps.py` reads. There is deliberately no second table: "can this
clinic use the lab" and "is this clinic paying for the lab" have to be one
answer.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.billing import BillingCycle, Subscription, SubscriptionInvoice
from app.models.enums import InvoiceItemKind, TenantFeatureStatus
from app.models.tenancy import TenantFeature
from app.services.payment import billing_service
from app.services.payment.addon import item_service
from app.services.payment.invoice import generation_service
from app.services.payment.money import ZERO, now
from app.services.payment.proration import calculator as proration
from app.services.payment.proration.calculator import Proration

log = logging.getLogger("pms.billing")


@dataclass(frozen=True)
class AddonResult:
    """What happened, in enough detail for the API to say so plainly."""

    entitlement: TenantFeature
    subscription: Subscription
    cycle: BillingCycle
    #: The one-off invoice for a mid-cycle activation, when one was raised.
    invoice: SubscriptionInvoice | None = None
    #: The proration workings, so the screen can show "14 of 28 days".
    proration: Proration | None = None
    #: True when a scheduled cancellation was dropped rather than a new charge
    #: being made — requirement 8's "no duplicate charge", reported rather than
    #: implied.
    reinstated: bool = False

    @property
    def charged(self) -> bool:
        return self.invoice is not None


async def _entitlement(
    session: AsyncSession, tenant_id: uuid.UUID, feature_key: str
) -> TenantFeature | None:
    return (
        await session.execute(
            select(TenantFeature).where(
                TenantFeature.tenant_id == tenant_id,
                TenantFeature.feature_key == feature_key,
            )
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class Assessment:
    """What switching a module on would cost, worked out without doing it.

    The quote and the purchase both come from here, which is the point: a
    figure a clinic is shown before checkout and the figure it is charged are
    produced by the same function, so they cannot drift apart. Nothing in here
    writes.

    `blocked` carries the reason a module cannot be bought rather than raising,
    because a cart has to be able to show three unbuyable lines at once instead
    of failing on the first.
    """

    feature_key: str
    price: object
    charge: generation_service.DraftLine | None = None
    proration: Proration | None = None
    reinstated: bool = False
    already_paid: bool = False
    blocked: str | None = None

    @property
    def amount(self) -> Decimal:
        """What is charged now — zero when the module is free this cycle."""
        return self.charge.amount if self.charge is not None else ZERO

    @property
    def recurring(self) -> Decimal:
        """What it adds to every invoice after this one."""
        amount = getattr(self.price, "amount", None)
        return amount if amount is not None else ZERO


async def assess(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    feature_key: str,
    *,
    subscription: Subscription,
    cycle: BillingCycle,
    today: date,
) -> Assessment:
    """Price switching this module on, today, without switching it on."""
    price = await billing_service.addon_price(session, subscription.plan_id, feature_key)
    row = await _entitlement(session, tenant_id, feature_key)

    # Included in the base plan, or core to the product: on, and free.
    if not price.billable:
        return Assessment(feature_key=feature_key, price=price)

    # Requirement 8 — re-enable before the cycle ends. The feature never went
    # off, so there is nothing to charge and nothing to re-create.
    if row is not None and row.status == TenantFeatureStatus.ACTIVE and row.cancel_at is not None:
        return Assessment(feature_key=feature_key, price=price, reinstated=True)

    if row is not None and row.status == TenantFeatureStatus.ACTIVE:
        return Assessment(
            feature_key=feature_key, price=price, blocked="That module is already active."
        )

    # Already paid for to the end of this cycle — switched on and off again
    # inside one month. Charging again would be charging twice for the same
    # days.
    if row is not None and row.charged_through is not None and row.charged_through >= cycle.period_end:
        return Assessment(feature_key=feature_key, price=price, already_paid=True)

    share = proration.prorate(
        full_amount=price.amount or ZERO,
        period_start=cycle.period_start,
        period_end=cycle.period_end,
        effective_from=today,
    )
    if share.amount <= ZERO:
        return Assessment(feature_key=feature_key, price=price, proration=share)

    full = share.is_full
    return Assessment(
        feature_key=feature_key,
        price=price,
        proration=share,
        charge=generation_service.DraftLine(
            kind=InvoiceItemKind.ADDON if full else InvoiceItemKind.PRORATION,
            description=(
                f"{price.name} add-on"
                if full
                else f"{price.name} add-on — {share.fraction} days"
            ),
            amount=share.amount,
            unit_amount=share.full_amount,
            feature_key=feature_key,
            period_start=share.period_start,
            period_end=share.period_end,
            prorated_days=share.charged_days,
            cycle_days=share.cycle_days,
        ),
    )


@dataclass(frozen=True)
class Prepared:
    """A module switched on, with the line it owes but no invoice yet.

    The split exists so that buying one module and buying five are the same
    code. `activate` prepares one and invoices it; the cart prepares each and
    puts them all on **one** invoice, which is what makes "several modules, a
    single payment" true rather than a loop that can stop halfway.

    `line` is None when there is nothing to charge — the module is free, it was
    already paid for this cycle, or a scheduled cancellation was simply
    dropped. Those are ordinary outcomes, not failures.
    """

    feature_key: str
    entitlement: TenantFeature
    price: object
    subscription: Subscription
    cycle: BillingCycle
    today: date
    line: generation_service.DraftLine | None = None
    proration: Proration | None = None
    reinstated: bool = False


async def prepare(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    feature_key: str,
    *,
    today: date,
    subscription: Subscription | None = None,
) -> Prepared:
    """Switch a module on and work out what it owes. Raises no invoice.

    Every validation happens here, before anything is billed, so a cart
    containing one module the clinic already has fails the whole checkout
    rather than charging for the others and then stopping — the transaction
    rolls the rest back with it.

    The money is decided by `assess`, which the quote calls too; this function
    is the part that writes.
    """
    if subscription is None:
        subscription = await billing_service.require_subscription(
            session, tenant_id, for_update=True
        )
    cycle = await billing_service.ensure_cycle(session, subscription, on=today)
    verdict = await assess(
        session, tenant_id, feature_key, subscription=subscription, cycle=cycle, today=today
    )
    if verdict.blocked is not None:
        raise Conflict(verdict.blocked)

    row = await _entitlement(session, tenant_id, feature_key)
    if row is None:
        row = TenantFeature(tenant_id=tenant_id, feature_key=feature_key)
        session.add(row)
        await session.flush()

    if verdict.reinstated:
        cancelled_for = row.cancel_at
        row.cancel_at = None
        log.info(
            "tenant %s kept %s: scheduled cancellation on %s dropped",
            tenant_id, feature_key, cancelled_for,
        )
    elif not verdict.price.billable:
        row.status = TenantFeatureStatus.ACTIVE
        row.activated_at = row.activated_at or today
        row.cancel_at = None
        row.deactivated_at = None
    else:
        row.status = TenantFeatureStatus.ACTIVE
        row.activated_at = today
        row.cancel_at = None
        row.deactivated_at = None

    await session.flush()
    return Prepared(
        feature_key=feature_key,
        entitlement=row,
        price=verdict.price,
        subscription=subscription,
        cycle=cycle,
        today=today,
        line=verdict.charge,
        proration=verdict.proration,
        reinstated=verdict.reinstated,
    )


async def settle(
    session: AsyncSession, prep: Prepared, *, invoice: SubscriptionInvoice | None
) -> None:
    """Close the books on a prepared activation.

    Called for every prepared module whether or not it was charged, because
    the item has to exist either way — a module that came back free this cycle
    is still a module the clinic is subscribed to.
    """
    if invoice is not None and prep.line is not None:
        # Paid to the end of this cycle. The renewal that closes this cycle
        # reads this and does not bill for it again.
        prep.entitlement.charged_through = prep.cycle.period_end
    await session.flush()
    await item_service.record_purchase(
        session,
        subscription=prep.subscription,
        entitlement=prep.entitlement,
        price=prep.price,
        cycle=prep.cycle,
        today=prep.today,
    )


async def activate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    feature_key: str,
    *,
    today: date,
) -> AddonResult:
    """Switch one module on, and charge for it on an invoice of its own.

    Buying a single module straight from its page. The cart takes the same
    `prepare`/`settle` pair and puts several lines on one invoice instead.
    """
    prep = await prepare(session, tenant_id, feature_key, today=today)

    invoice = None
    if prep.line is not None:
        invoice = await generation_service.generate_one_off(
            session,
            subscription=prep.subscription,
            lines=[prep.line],
            today=today,
            period_start=prep.line.period_start,
            period_end=prep.line.period_end,
        )

    await settle(session, prep, invoice=invoice)

    if prep.proration is not None and prep.line is not None:
        log.info(
            "tenant %s activated %s: %s for %s",
            tenant_id, feature_key, prep.proration.amount, prep.proration.fraction,
        )

    return AddonResult(
        entitlement=prep.entitlement,
        subscription=prep.subscription,
        cycle=prep.cycle,
        invoice=invoice,
        proration=prep.proration,
        reinstated=prep.reinstated,
    )


async def cancel(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    feature_key: str,
    *,
    today: date,
) -> AddonResult:
    """Schedule a module to stop at the end of the cycle already paid for.

    Requirement 7, in three lines of state: the row stays `active`, `cancel_at`
    is the end of this cycle, and nothing is refunded. The clinic keeps the
    feature for the rest of the month it has paid for, and the next invoice
    does not mention it — because `renew` switches it off before that invoice
    is built.

    Cancelling twice is not an error: the second call finds the same date
    already set and says so.
    """
    subscription = await billing_service.require_subscription(session, tenant_id, for_update=True)
    price = await billing_service.addon_price(session, subscription.plan_id, feature_key)
    cycle = await billing_service.ensure_cycle(session, subscription, on=today)

    row = await _entitlement(session, tenant_id, feature_key)
    if row is None or row.status != TenantFeatureStatus.ACTIVE:
        raise Conflict("That module is not active.")
    if not price.billable:
        raise Conflict(
            "That module is part of the base plan and cannot be cancelled on its own."
        )

    if row.cancel_at is not None:
        await item_service.project(
            session, subscription=subscription, entitlement=row, price=price
        )
        return AddonResult(entitlement=row, subscription=subscription, cycle=cycle)

    # The end of the cycle, never a date the caller chose: the clinic has paid
    # to this day and keeps the feature to this day.
    row.cancel_at = cycle.period_end
    await session.flush()
    await item_service.project(
        session, subscription=subscription, entitlement=row, price=price
    )
    log.info(
        "tenant %s scheduled %s to end on %s (no refund)", tenant_id, feature_key, cycle.period_end
    )
    return AddonResult(entitlement=row, subscription=subscription, cycle=cycle)


async def deactivate_now(
    session: AsyncSession, tenant_id: uuid.UUID, feature_key: str
) -> TenantFeature:
    """Switch a module off immediately.

    The platform's hand on the switch, not a clinic's — for a module that was
    never paid for, or one being withdrawn. It still refunds nothing, and it is
    audited by the console route that calls it.
    """
    row = await _entitlement(session, tenant_id, feature_key)
    if row is None:
        raise NotFound("No such module for this clinic.")
    row.status = TenantFeatureStatus.INACTIVE
    row.cancel_at = None
    row.deactivated_at = now()
    await session.flush()
    # Off is off: a module nobody may use cannot stay on a bill.
    await item_service.end(session, tenant_id, feature_key)
    return row


__all__ = [
    "AddonResult",
    "Assessment",
    "Prepared",
    "activate",
    "assess",
    "cancel",
    "deactivate_now",
    "prepare",
    "settle",
]
