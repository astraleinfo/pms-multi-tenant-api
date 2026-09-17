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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.billing import BillingCycle, Subscription, SubscriptionInvoice
from app.models.enums import InvoiceItemKind, TenantFeatureStatus
from app.models.tenancy import TenantFeature
from app.services.payment import billing_service
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


async def activate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    feature_key: str,
    *,
    today: date,
) -> AddonResult:
    """Switch a module on, and charge for it.

    Three paths, and which one is taken is decided by state rather than by a
    flag the caller passes:

    *   the module is on with a cancellation scheduled — the cancellation is
        dropped and **nothing is charged**;
    *   the module is off but already paid for to the end of this cycle (it was
        on earlier in the same cycle) — it comes back on and **nothing is
        charged**, because that period's money has already been taken;
    *   otherwise it is prorated over the remaining days and invoiced now.
    """
    subscription = await billing_service.require_subscription(session, tenant_id, for_update=True)
    price = await billing_service.addon_price(session, subscription.plan_id, feature_key)
    cycle = await billing_service.ensure_cycle(session, subscription, on=today)

    row = await _entitlement(session, tenant_id, feature_key)
    if row is None:
        row = TenantFeature(tenant_id=tenant_id, feature_key=feature_key)
        session.add(row)
        await session.flush()

    # Included in the base plan, or core to the product: on, and free.
    if not price.billable:
        row.status = TenantFeatureStatus.ACTIVE
        row.activated_at = row.activated_at or today
        row.cancel_at = None
        row.deactivated_at = None
        await session.flush()
        return AddonResult(entitlement=row, subscription=subscription, cycle=cycle)

    # Requirement 8 — re-enable before the cycle ends. The feature never went
    # off, so there is nothing to charge and nothing to re-create.
    if row.status == TenantFeatureStatus.ACTIVE and row.cancel_at is not None:
        cancelled_for = row.cancel_at
        row.cancel_at = None
        await session.flush()
        log.info(
            "tenant %s kept %s: scheduled cancellation on %s dropped",
            tenant_id, feature_key, cancelled_for,
        )
        return AddonResult(
            entitlement=row, subscription=subscription, cycle=cycle, reinstated=True
        )

    if row.status == TenantFeatureStatus.ACTIVE:
        raise Conflict("That module is already active.")

    # Already paid for to the end of this cycle — switched on and off again
    # inside one month. Charging again would be charging twice for the same
    # days.
    already_paid = row.charged_through is not None and row.charged_through >= cycle.period_end
    row.status = TenantFeatureStatus.ACTIVE
    row.activated_at = today
    row.cancel_at = None
    row.deactivated_at = None

    if already_paid:
        await session.flush()
        return AddonResult(entitlement=row, subscription=subscription, cycle=cycle)

    share = proration.prorate(
        full_amount=price.amount or ZERO,
        period_start=cycle.period_start,
        period_end=cycle.period_end,
        effective_from=today,
    )
    if share.amount <= ZERO:
        await session.flush()
        return AddonResult(
            entitlement=row, subscription=subscription, cycle=cycle, proration=share
        )

    full = share.is_full
    line = generation_service.DraftLine(
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
    )
    invoice = await generation_service.generate_one_off(
        session,
        subscription=subscription,
        lines=[line],
        today=today,
        period_start=share.period_start,
        period_end=share.period_end,
    )
    # Paid to the end of this cycle. The renewal that closes this cycle reads
    # this and does not bill for it again.
    row.charged_through = cycle.period_end
    await session.flush()

    log.info(
        "tenant %s activated %s: %s for %s",
        tenant_id, feature_key, share.amount, share.fraction,
    )
    return AddonResult(
        entitlement=row,
        subscription=subscription,
        cycle=cycle,
        invoice=invoice,
        proration=share,
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
        return AddonResult(entitlement=row, subscription=subscription, cycle=cycle)

    # The end of the cycle, never a date the caller chose: the clinic has paid
    # to this day and keeps the feature to this day.
    row.cancel_at = cycle.period_end
    await session.flush()
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
    return row


__all__ = ["AddonResult", "activate", "cancel", "deactivate_now"]
