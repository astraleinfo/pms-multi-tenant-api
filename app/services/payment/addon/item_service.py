"""`subscription_items` — the billing half of a module, kept in step with the
entitlement half.

`tenant_features` answers "may this clinic use the lab". This table answers
"what is this clinic paying for the lab, on what interval, pinned when". The
codebase's rule that those must never become two different answers is kept by
writing both in one transaction, and this module is the only place the second
one is written.

The division of labour is deliberate, and it is the whole reason there are two
functions rather than one:

*   **`record_purchase` creates.** It is called only where money changed hands,
    and it pins `unit_amount` at that moment. Nothing else in the codebase may
    create an item.
*   **`project` never creates.** It makes an item that already exists agree
    with its entitlement row — status, the scheduled cancellation, the dates —
    and cancels it when the module goes off. Called from everywhere.

That asymmetry is what makes the platform console's hand on the switch behave
correctly. Forcing a module **on** from the console is a grant, not a sale: no
item appears, and the clinic is not billed for something nobody sold them.
Forcing it **off** cancels any item there was, because a module that is off
cannot go on a bill. A comp is therefore an entitlement with no item, which is
exactly what it is.

**`unit_amount` is pinned once and never re-derived.** `project` deliberately
does not touch it. If it did, a catalogue price rise would silently reprice
every clinic already on the module the next time anything synced — which is
the precise failure the table was introduced to make impossible.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import BillingCycle, ModulePrice, Subscription, SubscriptionItem
from app.models.enums import (
    PlanInterval,
    SubscriptionItemStatus,
    TenantFeatureStatus,
)
from app.models.tenancy import TenantFeature
from app.services.payment.money import now

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.payment.billing_service import AddonPrice

log = logging.getLogger("pms.billing")


async def live_item(
    session: AsyncSession, tenant_id: uuid.UUID, feature_key: str
) -> SubscriptionItem | None:
    """The item that is not cancelled, of which there is at most one.

    "At most one" is a partial unique index, not a hope — see
    `uq_subscription_items_one_live` in 0013.
    """
    return (
        await session.execute(
            select(SubscriptionItem).where(
                SubscriptionItem.tenant_id == tenant_id,
                SubscriptionItem.feature_key == feature_key,
                SubscriptionItem.status != SubscriptionItemStatus.CANCELLED,
            )
        )
    ).scalar_one_or_none()


def _status_for(entitlement: TenantFeature) -> SubscriptionItemStatus:
    """The one mapping from entitlement state to item state.

    `pending_cancel` before `trialing` on purpose: a module in its trial that
    has been told to stop is stopping, and saying "trialing" would lose the
    only fact the screen has to act on.
    """
    if entitlement.status == TenantFeatureStatus.INACTIVE:
        return SubscriptionItemStatus.CANCELLED
    if entitlement.cancel_at is not None:
        return SubscriptionItemStatus.PENDING_CANCEL
    if entitlement.status == TenantFeatureStatus.TRIAL:
        return SubscriptionItemStatus.TRIALING
    return SubscriptionItemStatus.ACTIVE


async def _pinned_price_id(
    session: AsyncSession, feature_key: str, currency, amount
) -> uuid.UUID | None:
    """The catalogue row this figure came from, or null.

    Null is the honest answer when a `plan_features` override priced the
    module: the figure is real, but it did not come from the catalogue, and
    recording a price that was never applied would mislead every later audit.
    """
    return (
        await session.execute(
            select(ModulePrice.id).where(
                ModulePrice.feature_key == feature_key,
                ModulePrice.interval == PlanInterval.MONTHLY,
                ModulePrice.currency == currency,
                ModulePrice.active.is_(True),
                ModulePrice.amount == amount,
            )
        )
    ).scalar_one_or_none()


async def record_purchase(
    session: AsyncSession,
    *,
    subscription: Subscription,
    entitlement: TenantFeature,
    price: AddonPrice,
    cycle: BillingCycle | None,
    today: date,
) -> SubscriptionItem | None:
    """A module was bought. Give it an item, at the price it was bought at.

    A module that is core or included in the plan gets nothing: it is switched
    on and it is not a line on a bill. Any item it *did* have — because the
    plan changed under it and it is now free — is cancelled.

    Re-buying a module that was cancelled earlier creates a **new** row rather
    than resurrecting the old one, so the price it was on before stays legible
    and the two spells are two rows.
    """
    existing = await live_item(session, entitlement.tenant_id, entitlement.feature_key)

    if not price.billable:
        if existing is not None:
            await _cancel(session, existing)
        return None

    if existing is not None:
        # Already on and already paid for — the reinstatement path. Nothing is
        # bought twice, so nothing is repriced.
        return await project(
            session, subscription=subscription, entitlement=entitlement, price=price
        )

    amount = price.amount
    currency = subscription.plan.currency
    item = SubscriptionItem(
        tenant_id=entitlement.tenant_id,
        subscription_id=subscription.id,
        feature_key=entitlement.feature_key,
        price_id=await _pinned_price_id(session, entitlement.feature_key, currency, amount),
        interval=PlanInterval.MONTHLY,
        quantity=1,
        unit_amount=amount,
        currency=currency,
        status=_status_for(entitlement),
        started_on=entitlement.activated_at or today,
        current_period_start=cycle.period_start if cycle is not None else None,
        current_period_end=cycle.period_end if cycle is not None else None,
        charged_through=entitlement.charged_through,
        cancel_at=entitlement.cancel_at,
    )
    session.add(item)
    await session.flush()
    log.info(
        "tenant %s item opened for %s at %s %s",
        entitlement.tenant_id, entitlement.feature_key, currency, amount,
    )
    return item


async def project(
    session: AsyncSession,
    *,
    subscription: Subscription,
    entitlement: TenantFeature,
    price: AddonPrice,
) -> SubscriptionItem | None:
    """Make the item agree with the entitlement. Creates nothing.

    Returns the item it touched, or None when there was none to touch — which
    is the ordinary state of a core module, a plan-included one, and anything
    the console switched on as a comp.
    """
    item = await live_item(session, entitlement.tenant_id, entitlement.feature_key)
    if item is None:
        return None

    if not price.billable:
        await _cancel(session, item)
        return item

    status = _status_for(entitlement)
    if status == SubscriptionItemStatus.CANCELLED:
        await _cancel(session, item)
        return item

    item.status = status
    item.cancel_at = entitlement.cancel_at
    item.charged_through = entitlement.charged_through
    if entitlement.activated_at is not None:
        item.started_on = entitlement.activated_at
    # `unit_amount` and `price_id` are pinned. See the module docstring.
    await session.flush()
    return item


async def end(session: AsyncSession, tenant_id: uuid.UUID, feature_key: str) -> bool:
    """Stop billing this module, whatever switched it off.

    The blunt instrument, for the callers that already know the module is
    going off and hold no price to ask about it: the subscription being
    cancelled, a scheduled cancellation arriving, a plan starting to include
    the module, the console forcing it off. Returns whether there was
    anything to stop, so a caller can log honestly.
    """
    item = await live_item(session, tenant_id, feature_key)
    if item is None:
        return False
    await _cancel(session, item)
    return True


async def set_charged_through(
    session: AsyncSession, tenant_id: uuid.UUID, feature_key: str, through: date | None
) -> None:
    """Follow the entitlement's `charged_through` onto the item.

    Called in the same transaction as the invoice that moved it, so the two
    cannot disagree about what has been paid for — including on the rollback
    path, where an unpaid invoice hands the date back.
    """
    item = await live_item(session, tenant_id, feature_key)
    if item is None:
        return
    item.charged_through = through
    await session.flush()


async def _cancel(session: AsyncSession, item: SubscriptionItem) -> None:
    item.status = SubscriptionItemStatus.CANCELLED
    item.cancel_at = None
    item.cancelled_at = now()
    await session.flush()


async def advance_periods(
    session: AsyncSession, subscription: Subscription, cycle: BillingCycle
) -> None:
    """Move every live item onto the cycle now being lived in.

    Called by the renewal, after the cancellations due at the old cycle's end
    have been applied — so a module that has just stopped is already
    `cancelled` here and is not carried into a period it is not in.
    """
    items = list(
        (
            await session.execute(
                select(SubscriptionItem).where(
                    SubscriptionItem.subscription_id == subscription.id,
                    SubscriptionItem.status != SubscriptionItemStatus.CANCELLED,
                )
            )
        ).scalars()
    )
    for item in items:
        item.current_period_start = cycle.period_start
        item.current_period_end = cycle.period_end
    if items:
        await session.flush()


__all__ = [
    "advance_periods",
    "end",
    "live_item",
    "project",
    "record_purchase",
    "set_charged_through",
]
