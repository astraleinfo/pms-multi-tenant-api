"""The subscription lifecycle: create, activate, pause, resume, cancel, renew,
change plan.

The PMS is the source of truth. A gateway is told what happened and is asked to
collect; it is never asked what the clinic owes, and a gateway that is down or
not configured does not stop a clinic from subscribing, pausing or cancelling.
That asymmetry is deliberate — the alternative is a billing system whose
correctness depends on somebody else's uptime.

The only gateway this file knows about is `gateway.Gateway`, the protocol.
`get_gateway()` resolves the configured adapter by name at call time, so
nothing in this module — or anything that imports it — pulls in Razorpay.

Two rules run through every function here:

*   **transitions are checked against `SUBSCRIPTION_TRANSITIONS`**, the same
    way appointments, queue entries and lab orders are checked elsewhere in
    this codebase. A transition that is not in the table cannot happen,
    whoever asks for it;
*   **cancellation is always at the end of the cycle already paid for**, unless
    somebody deliberately asks otherwise. A clinic that has paid for the month
    keeps the month.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import Conflict
from app.models.billing import BillingCycle, Plan, Subscription, SubscriptionInvoice
from app.models.enums import (
    SUBSCRIPTION_TRANSITIONS,
    BillingCycleStatus,
    PaymentGateway,
    SubscriptionStatus,
    TenantFeatureStatus,
)
from app.models.tenancy import Feature, TenantFeature
from app.services.payment import billing_service
from app.services.payment.addon import item_service
from app.services.payment.gateway import Gateway, GatewayError, get_gateway
from app.services.payment.invoice import generation_service
from app.services.payment.money import ZERO, money, now
from app.services.payment.proration import calculator as proration

log = logging.getLogger("pms.billing")


@dataclass(frozen=True)
class SubscriptionResult:
    """What a lifecycle call produced. The invoice is `None` when the call did
    not raise one — pausing, for instance, or a renewal that found this cycle
    already invoiced."""

    subscription: Subscription
    cycle: BillingCycle | None = None
    invoice: SubscriptionInvoice | None = None


def _require_transition(current: SubscriptionStatus, target: SubscriptionStatus) -> None:
    if current == target:
        return
    if target not in SUBSCRIPTION_TRANSITIONS[SubscriptionStatus(current)]:
        raise Conflict(
            f"A {current.value.replace('_', ' ')} subscription cannot become "
            f"{target.value.replace('_', ' ')}."
        )


async def _gateway_or_none(name: str | None = None) -> Gateway | None:
    """The configured gateway, or nothing at all.

    Nothing at all is a supported state, not a degraded one: a deployment that
    bills by bank transfer sets `BILLING_GATEWAY=manual`, and every function
    below works unchanged — invoices are raised, totals are right, and the
    money arrives by another route.
    """
    if not settings.billing_enabled or settings.billing_gateway == "manual":
        return None
    try:
        candidate = get_gateway(name)
    except GatewayError as exc:
        log.warning("no payment gateway available: %s", exc)
        return None
    return candidate if candidate.configured else None


# --- creation ----------------------------------------------------------------


async def subscribe(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    plan: Plan,
    today: date,
    trial_days: int | None = None,
) -> SubscriptionResult:
    """Put a clinic on a base plan. Every tenant must have one (requirement 1).

    The first cycle starts today, not at the next anchor: the clinic has the
    product from this moment, so it is billed from this moment, and the day of
    the month becomes the anchor every later cycle bills on.

    The first invoice is raised immediately unless the plan carries a trial, in
    which case the subscription is `trialing` and the first invoice waits for
    the renewal job to close the trial cycle. Either way the clinic is billed
    once for the period, never twice.
    """
    existing = await billing_service.active_subscription(session, tenant_id, for_update=True)
    if existing is not None:
        raise Conflict("This clinic already has a subscription.")
    if not plan.active:
        raise Conflict("That plan is no longer offered.")

    await billing_service.get_account(session, tenant_id)
    trial = plan.trial_days if trial_days is None else max(trial_days, 0)

    subscription = Subscription(
        tenant_id=tenant_id,
        plan_id=plan.id,
        status=SubscriptionStatus.TRIALING if trial else SubscriptionStatus.ACTIVE,
        started_on=today,
        anchor_day=billing_service.anchor_from(today),
        trial_ends_on=today + timedelta(days=trial) if trial else None,
        gateway=PaymentGateway(settings.billing_gateway),
    )
    session.add(subscription)
    await session.flush()

    cycle = await billing_service.open_cycle(session, subscription, period_start=today, plan=plan)
    await _sync_included_features(session, subscription, plan, today=today)

    invoice: SubscriptionInvoice | None = None
    if not trial:
        invoice, _ = await generation_service.generate_for_cycle(
            session, subscription=subscription, cycle=cycle, today=today
        )

    await register_with_gateway(session, subscription, plan)
    log.info("tenant %s subscribed to plan %s", tenant_id, plan.code)
    return SubscriptionResult(subscription=subscription, cycle=cycle, invoice=invoice)


async def _sync_included_features(
    session: AsyncSession, subscription: Subscription, plan: Plan, *, today: date
) -> None:
    """Switch on everything the base plan covers.

    Core modules and anything the plan marks `included` come on with the
    subscription and are never billed as add-ons. Nothing is switched *off*
    here — a clinic that already had a module keeps it, and a plan change that
    stops including one is handled where the money for it is, not here.
    """
    from sqlalchemy import select

    prices = await billing_service.addon_prices(session, plan.id)
    existing = {
        row.feature_key: row
        for row in (
            await session.execute(
                select(TenantFeature).where(TenantFeature.tenant_id == subscription.tenant_id)
            )
        ).scalars()
    }
    features = {f.key for f in (await session.execute(select(Feature))).scalars()}

    for key in sorted(features):
        price = prices.get(key)
        if price is None or not price.included:
            continue
        row = existing.get(key)
        if row is None:
            row = TenantFeature(tenant_id=subscription.tenant_id, feature_key=key)
            session.add(row)
        if row.status != TenantFeatureStatus.ACTIVE:
            row.status = TenantFeatureStatus.ACTIVE
            row.activated_at = today
            row.deactivated_at = None
        row.cancel_at = None
        # The plan includes it now, so it is free. Any item that was billing
        # for it stops — a plan change that bundles a module must not leave the
        # clinic paying for it twice.
        await item_service.end(session, subscription.tenant_id, key)
    await session.flush()


async def register_with_gateway(
    session: AsyncSession, subscription: Subscription, plan: Plan
) -> None:
    """Tell the gateway this clinic exists and has a subscription.

    Best effort, and that is the point: a gateway outage must not stop a clinic
    signing up. What we record locally is what the clinic owes; the gateway's
    ids are stored when they arrive, and `SyncRazorpaySubscriptionJob` fills in
    anything that did not.
    """
    provider = await _gateway_or_none()
    if provider is None or not plan.gateway_plan_id:
        return

    account = await billing_service.get_account(session, subscription.tenant_id)
    try:
        if not account.gateway_customer_id:
            customer = await provider.customers.ensure(
                reference=str(subscription.tenant_id),
                name=account.legal_name or str(subscription.tenant_id),
                email=account.billing_email,
                phone=account.phone,
            )
            account.gateway_customer_id = customer.id
            await session.flush()

        remote = await provider.subscriptions.create(
            plan_reference=plan.gateway_plan_id,
            customer_id=account.gateway_customer_id or "",
            total_count=120,
            notes={"tenant_id": str(subscription.tenant_id)},
        )
        subscription.gateway_subscription_id = remote.id
        subscription.gateway_status = remote.status
        subscription.gateway_synced_at = now()
        await session.flush()
    except GatewayError as exc:
        log.warning(
            "subscription %s created locally; gateway registration deferred: %s",
            subscription.id, exc,
        )


# --- lifecycle ---------------------------------------------------------------


async def activate(session: AsyncSession, subscription: Subscription) -> Subscription:
    """Move a pending or trialing subscription to active.

    Called when the first payment is captured, and by the renewal job when a
    trial runs out. Idempotent: activating an active subscription is a no-op,
    which matters because a webhook can arrive twice.
    """
    current = SubscriptionStatus(subscription.status)
    if current == SubscriptionStatus.ACTIVE:
        return subscription
    _require_transition(current, SubscriptionStatus.ACTIVE)
    subscription.status = SubscriptionStatus.ACTIVE
    await session.flush()
    return subscription


async def pause(session: AsyncSession, subscription: Subscription) -> Subscription:
    """Stop billing without ending the agreement.

    The cycle stays open and the feature set is untouched — pausing is a
    billing state, not an entitlement one. Nothing is refunded for the part of
    the cycle already paid for, and nothing new is charged until it resumes.
    """
    current = SubscriptionStatus(subscription.status)
    if current == SubscriptionStatus.PAUSED:
        return subscription
    _require_transition(current, SubscriptionStatus.PAUSED)
    subscription.status = SubscriptionStatus.PAUSED
    subscription.paused_at = now()
    await session.flush()

    provider = await _gateway_or_none()
    if provider is not None and subscription.gateway_subscription_id:
        try:
            remote = await provider.subscriptions.pause(subscription.gateway_subscription_id)
            subscription.gateway_status = remote.status
            subscription.gateway_synced_at = now()
        except GatewayError as exc:
            log.warning("subscription %s paused locally only: %s", subscription.id, exc)
    await session.flush()
    return subscription


async def resume(
    session: AsyncSession, subscription: Subscription, *, today: date
) -> SubscriptionResult:
    """Start billing again.

    The calendar is caught up rather than restarted: `ensure_cycle` opens
    whatever cycles were skipped so the anchor day survives a pause, and the
    invoices for them are raised by the renewal job in the normal way.
    """
    current = SubscriptionStatus(subscription.status)
    if current != SubscriptionStatus.PAUSED:
        raise Conflict("That subscription is not paused.")
    _require_transition(current, SubscriptionStatus.ACTIVE)
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.resumed_at = now()
    await session.flush()

    cycle = await billing_service.ensure_cycle(session, subscription, on=today)

    provider = await _gateway_or_none()
    if provider is not None and subscription.gateway_subscription_id:
        try:
            remote = await provider.subscriptions.resume(subscription.gateway_subscription_id)
            subscription.gateway_status = remote.status
            subscription.gateway_synced_at = now()
        except GatewayError as exc:
            log.warning("subscription %s resumed locally only: %s", subscription.id, exc)
    await session.flush()
    return SubscriptionResult(subscription=subscription, cycle=cycle)


async def cancel(
    session: AsyncSession,
    subscription: Subscription,
    *,
    today: date,
    immediately: bool = False,
) -> Subscription:
    """End the agreement.

    By default at the end of the cycle the clinic has already paid for: the
    subscription stays `active`, `cancel_at_cycle_end` is set, and the renewal
    job is what finally closes it. That is the same shape as an add-on
    cancellation, for the same reason — money already taken buys the whole
    period it was taken for.

    `immediately` is the platform's escape hatch, not a clinic's. It ends the
    subscription now and still does not refund anything.
    """
    current = SubscriptionStatus(subscription.status)
    if current in billing_service.DEAD_STATUSES:
        return subscription

    if immediately:
        _require_transition(current, SubscriptionStatus.CANCELLED)
        subscription.status = SubscriptionStatus.CANCELLED
        subscription.cancelled_at = today
        subscription.cancel_at_cycle_end = False
        await _end_addons(session, subscription, on=today)
    else:
        subscription.cancel_at_cycle_end = True
    await session.flush()

    provider = await _gateway_or_none()
    if provider is not None and subscription.gateway_subscription_id:
        try:
            remote = await provider.subscriptions.cancel(
                subscription.gateway_subscription_id, at_cycle_end=not immediately
            )
            subscription.gateway_status = remote.status
            subscription.gateway_synced_at = now()
        except GatewayError as exc:
            log.warning("subscription %s cancelled locally only: %s", subscription.id, exc)
    await session.flush()
    return subscription


async def resume_cancellation(session: AsyncSession, subscription: Subscription) -> Subscription:
    """Undo a scheduled cancellation, before it takes effect.

    The mirror of re-enabling an add-on (requirement 8): the subscription never
    stopped, so nothing is charged again and nothing is re-created.
    """
    if subscription.status in billing_service.DEAD_STATUSES:
        raise Conflict("That subscription has already ended.")
    if not subscription.cancel_at_cycle_end:
        return subscription
    subscription.cancel_at_cycle_end = False
    await session.flush()
    return subscription


async def _end_addons(session: AsyncSession, subscription: Subscription, *, on: date) -> None:
    """Switch off every purchasable module when the agreement ends.

    Core and plan-included modules are left alone: a cancelled clinic keeps
    reading its own patient records until somebody at the platform decides
    otherwise, and that is not a billing decision.
    """
    from sqlalchemy import select

    prices = await billing_service.addon_prices(session, subscription.plan_id)
    rows = (
        await session.execute(
            select(TenantFeature).where(
                TenantFeature.tenant_id == subscription.tenant_id,
                TenantFeature.status == TenantFeatureStatus.ACTIVE,
            )
        )
    ).scalars()
    for row in rows:
        price = prices.get(row.feature_key)
        if price is None or not price.billable:
            continue
        row.status = TenantFeatureStatus.INACTIVE
        row.cancel_at = None
        row.deactivated_at = now()
        await item_service.end(session, subscription.tenant_id, row.feature_key)
    await session.flush()


# --- renewal -----------------------------------------------------------------


async def renew(
    session: AsyncSession, subscription: Subscription, *, today: date
) -> SubscriptionResult:
    """Close the cycle that has ended, apply what was scheduled, open the next.

    This is the one function that makes scheduled things happen, and the order
    matters:

    1.  the cycle that has ended is invoiced, if it has not been already — the
        clinic is billed for the period it has just used, including any add-on
        that was cancelling at the end of it;
    2.  add-ons whose `cancel_at` has arrived are switched off, so they are not
        in the next cycle's invoice (requirement 7's third clause);
    3.  a subscription cancelling at the end of the cycle ends here, and no
        further cycle is opened;
    4.  otherwise the next cycle is opened and the clinic is billed for it.

    Idempotent throughout: every step either finds its work already done or is
    protected by a unique index. Running this twice on the same day produces
    the same database.
    """
    if subscription.status in billing_service.DEAD_STATUSES:
        return SubscriptionResult(subscription=subscription)
    if subscription.status == SubscriptionStatus.PAUSED:
        return SubscriptionResult(subscription=subscription)

    cycle = await billing_service.latest_cycle(session, subscription)
    if cycle is None:
        cycle = await billing_service.ensure_cycle(session, subscription, on=today)

    if cycle.period_end > today:
        # Nothing has ended yet. Make sure this cycle has its invoice — a
        # subscription created during a gateway outage may not have got one.
        invoice, _ = await _invoice_cycle(session, subscription, cycle, today=today)
        return SubscriptionResult(subscription=subscription, cycle=cycle, invoice=invoice)

    invoice, _ = await _invoice_cycle(session, subscription, cycle, today=today)
    if cycle.status == BillingCycleStatus.OPEN:
        cycle.status = BillingCycleStatus.CLOSED
        cycle.closed_at = now()

    await apply_scheduled_cancellations(session, subscription, on=cycle.period_end)

    if subscription.status == SubscriptionStatus.TRIALING and (
        subscription.trial_ends_on is None or subscription.trial_ends_on <= today
    ):
        await activate(session, subscription)

    if subscription.cancel_at_cycle_end:
        subscription.status = SubscriptionStatus.CANCELLED
        subscription.cancelled_at = cycle.period_end
        await _end_addons(session, subscription, on=cycle.period_end)
        await session.flush()
        log.info("subscription %s ended as scheduled", subscription.id)
        return SubscriptionResult(subscription=subscription, cycle=cycle, invoice=invoice)

    next_cycle = await billing_service.open_cycle(
        session, subscription, period_start=cycle.period_end
    )
    # After the cancellations above, so a module that has just stopped is
    # already cancelled and is not carried into a period it is not in.
    await item_service.advance_periods(session, subscription, next_cycle)
    next_invoice, _ = await _invoice_cycle(session, subscription, next_cycle, today=today)
    await session.flush()
    return SubscriptionResult(
        subscription=subscription, cycle=next_cycle, invoice=next_invoice or invoice
    )


async def _invoice_cycle(
    session: AsyncSession, subscription: Subscription, cycle: BillingCycle, *, today: date
) -> tuple[SubscriptionInvoice | None, bool]:
    """Invoice a cycle, unless it is a trial cycle nobody owes anything for."""
    if subscription.status == SubscriptionStatus.TRIALING and (
        subscription.trial_ends_on is not None and subscription.trial_ends_on >= cycle.period_end
    ):
        return None, False
    return await generation_service.generate_for_cycle(
        session, subscription=subscription, cycle=cycle, today=today
    )


async def apply_scheduled_cancellations(
    session: AsyncSession, subscription: Subscription, *, on: date
) -> list[str]:
    """Switch off the add-ons whose scheduled cancellation has arrived.

    Requirement 7, the part that happens later: the feature stayed usable until
    the end of the cycle, nothing was refunded, and now it stops — and because
    it is `inactive` before the next cycle's invoice is built, it is not
    charged for again.
    """
    from sqlalchemy import select

    rows = list(
        (
            await session.execute(
                select(TenantFeature).where(
                    TenantFeature.tenant_id == subscription.tenant_id,
                    TenantFeature.status == TenantFeatureStatus.ACTIVE,
                    TenantFeature.cancel_at.is_not(None),
                    TenantFeature.cancel_at <= on,
                )
            )
        ).scalars()
    )
    ended: list[str] = []
    for row in rows:
        row.status = TenantFeatureStatus.INACTIVE
        row.deactivated_at = now()
        row.cancel_at = None
        await item_service.end(session, subscription.tenant_id, row.feature_key)
        ended.append(row.feature_key)
    if ended:
        await session.flush()
        log.info("tenant %s add-ons ended: %s", subscription.tenant_id, ", ".join(ended))
    return ended


# --- plan changes ------------------------------------------------------------


async def change_plan(
    session: AsyncSession,
    subscription: Subscription,
    *,
    plan: Plan,
    today: date,
) -> SubscriptionResult:
    """Move a clinic to a different base plan, mid-cycle.

    The cycle in flight is ended today and marked `superseded`, and a fresh one
    starts today on the new plan. The clinic has already paid for the whole of
    the old cycle, so the unused remainder of it becomes a credit line on the
    new cycle's invoice — that is what `proration.unused_value` is for, and it
    is the one place in this package where a credit is issued.

    The anchor day moves to today, because the new cycle starts today. A clinic
    that changes plan on the 8th bills on the 8th from then on.
    """
    if subscription.status in billing_service.DEAD_STATUSES:
        raise Conflict("That subscription has ended.")
    if not plan.active:
        raise Conflict("That plan is no longer offered.")
    if plan.id == subscription.plan_id:
        raise Conflict("That is already this clinic's plan.")

    current = await billing_service.current_cycle(session, subscription, on=today)
    credits: tuple[generation_service.Credit, ...] = ()
    cycle = None

    if current is not None and today == current.period_start:
        # The change is on the day the cycle began, so none of it has been
        # used: there is nothing to credit, and a new cycle starting today
        # would collide with this one on `uq_billing_cycle_period`. The cycle
        # is *replaced* rather than superseded — same period, different plan —
        # and its invoice is voided and re-raised.
        existing = await generation_service.invoice_for_cycle(session, current)
        if existing is not None:
            if billing_service.paid_amount(existing) > ZERO:
                raise Conflict(
                    "This period has already been paid for. Refund the payment "
                    "before changing plan, or change plan from the next cycle."
                )
            await generation_service.void_invoice(
                session,
                subscription.tenant_id,
                existing.id,
                reason=f"Replaced by a plan change to {plan.name}.",
            )
        current.plan_id = plan.id
        current.plan_amount = money(plan.amount)
        current.status = BillingCycleStatus.OPEN
        current.closed_at = None
        cycle = current
        await session.flush()
    elif current is not None:
        previous_plan = await billing_service.get_plan(session, current.plan_id)
        unused = proration.unused_value(
            full_amount=current.plan_amount,
            period_start=current.period_start,
            period_end=current.period_end,
            effective_to=today,
        )
        if unused.amount > ZERO:
            credits = (
                generation_service.Credit(
                    description=(
                        f"Credit for unused {previous_plan.name} — {unused.fraction} days"
                    ),
                    amount=unused.amount,
                ),
            )
        # The period actually lived through is what the cycle now records.
        current.period_end = today
        current.status = BillingCycleStatus.SUPERSEDED
        current.closed_at = now()
        await session.flush()

    subscription.plan_id = plan.id
    subscription.anchor_day = billing_service.anchor_from(today)
    await session.flush()

    if cycle is None:
        cycle = await billing_service.open_cycle(
            session, subscription, period_start=today, plan=plan
        )
    await _sync_included_features(session, subscription, plan, today=today)
    invoice, _ = await generation_service.generate_for_cycle(
        session, subscription=subscription, cycle=cycle, today=today, credits=credits
    )
    log.info("tenant %s moved to plan %s", subscription.tenant_id, plan.code)
    return SubscriptionResult(subscription=subscription, cycle=cycle, invoice=invoice)


async def mark_past_due(session: AsyncSession, subscription: Subscription) -> Subscription:
    """An invoice has gone unpaid past its grace period.

    Nothing is switched off. Suspension is a deliberate act by the platform —
    cutting a clinic off from its own patient records is not a decision a cron
    job gets to make — and this status is what tells somebody to look.
    """
    current = SubscriptionStatus(subscription.status)
    if current == SubscriptionStatus.PAST_DUE:
        return subscription
    _require_transition(current, SubscriptionStatus.PAST_DUE)
    subscription.status = SubscriptionStatus.PAST_DUE
    await session.flush()
    return subscription


async def clear_past_due(session: AsyncSession, subscription: Subscription) -> Subscription:
    if subscription.status != SubscriptionStatus.PAST_DUE:
        return subscription
    subscription.status = SubscriptionStatus.ACTIVE
    await session.flush()
    return subscription


async def sync_from_gateway(
    session: AsyncSession, subscription: Subscription
) -> Subscription:
    """Record what the gateway believes, without obeying it.

    The gateway's word is stored in `gateway_status` and never copied into
    `status`. If the two disagree, the invoices this system raised are still
    what the clinic owes, and somebody can see the disagreement — which is a
    great deal better than a webhook quietly cancelling a paying customer.
    """
    provider = await _gateway_or_none()
    if provider is None or not subscription.gateway_subscription_id:
        return subscription
    try:
        remote = await provider.subscriptions.fetch(subscription.gateway_subscription_id)
    except GatewayError as exc:
        log.warning("could not sync subscription %s: %s", subscription.id, exc)
        return subscription
    subscription.gateway_status = remote.status
    subscription.gateway_synced_at = now()
    await session.flush()
    return subscription


__all__ = [
    "SubscriptionResult", "activate", "apply_scheduled_cancellations", "cancel", "change_plan",
    "clear_past_due", "mark_past_due", "pause", "register_with_gateway", "renew", "resume",
    "resume_cancellation", "subscribe", "sync_from_gateway",
]
