"""The questions the rest of the billing domain asks, and the answers.

What a plan costs, what an add-on costs *under that plan*, which cycle a clinic
is living in, what the next invoice number is, and what the Settings → Billing
screen should say. Everything else in this package — the subscription
lifecycle, the invoice generator, the add-on service, the metrics — reads
prices through here, and nothing anywhere carries a number of its own.

No gateway is imported in this file, or in any file above `gateway.py`. That is
the property the whole layout exists to protect: what a clinic owes is decided
here, and a gateway is only ever asked to collect it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import Conflict, NotFound
from app.models.billing import (
    BillingAccount,
    BillingCycle,
    Plan,
    PlanFeature,
    Subscription,
    SubscriptionInvoice,
)
from app.models.enums import (
    BillingCycleStatus,
    SubscriptionInvoiceStatus,
    SubscriptionStatus,
    TenantFeatureStatus,
)
from app.models.tenancy import Feature, TenantFeature
from app.services.payment.money import ZERO, money, next_anchor

#: A subscription in one of these is not billed and not renewed. It is still a
#: row, because the invoices it raised point at it.
DEAD_STATUSES = (SubscriptionStatus.CANCELLED, SubscriptionStatus.EXPIRED)
#: …and in one of these, the clinic is expected to keep paying.
LIVE_STATUSES = (
    SubscriptionStatus.PENDING,
    SubscriptionStatus.TRIALING,
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.PAST_DUE,
)


# --- the catalogue -----------------------------------------------------------


async def list_plans(session: AsyncSession, *, include_inactive: bool = False) -> list[Plan]:
    stmt = select(Plan).order_by(Plan.sort_order, Plan.name)
    if not include_inactive:
        stmt = stmt.where(Plan.active.is_(True))
    return list((await session.execute(stmt)).scalars())


async def get_plan(session: AsyncSession, plan_id: uuid.UUID) -> Plan:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise NotFound("No such plan.")
    return plan


async def plan_by_code(session: AsyncSession, code: str) -> Plan:
    plan = (
        await session.execute(select(Plan).where(Plan.code == code.strip().lower()))
    ).scalar_one_or_none()
    if plan is None:
        raise NotFound("No such plan.")
    return plan


@dataclass(frozen=True)
class AddonPrice:
    """What one module costs a clinic on a given plan, and why.

    `included` means the base plan already covers it: it is switched on for
    free, never invoiced as an add-on, and cancelling it is a support question
    rather than a billing one. `amount` is null in that case, which is why the
    two are separate fields rather than a zero.
    """

    feature_key: str
    name: str
    included: bool
    amount: Decimal | None
    is_core: bool

    @property
    def billable(self) -> bool:
        return not self.included and not self.is_core and (self.amount or ZERO) > ZERO


async def addon_prices(session: AsyncSession, plan_id: uuid.UUID) -> dict[str, AddonPrice]:
    """Every module, priced for this plan, keyed by feature key.

    The fallback chain is the whole pricing policy and it is three lines long:
    a `plan_features` row marked `included` is free; one with an `addon_amount`
    charges that; anything else charges `features.price_monthly`. So the common
    case needs no `plan_features` row at all, and a plan that bundles or
    discounts a module needs exactly one.
    """
    features = list((await session.execute(select(Feature).order_by(Feature.key))).scalars())
    overrides = {
        row.feature_key: row
        for row in (
            await session.execute(select(PlanFeature).where(PlanFeature.plan_id == plan_id))
        ).scalars()
    }

    prices: dict[str, AddonPrice] = {}
    for feature in features:
        override = overrides.get(feature.key)
        if feature.is_core or (override is not None and override.included):
            prices[feature.key] = AddonPrice(
                feature_key=feature.key,
                name=feature.name,
                included=True,
                amount=None,
                is_core=feature.is_core,
            )
            continue
        amount = (
            money(override.addon_amount)
            if override is not None and override.addon_amount is not None
            else money(feature.price_monthly)
        )
        prices[feature.key] = AddonPrice(
            feature_key=feature.key,
            name=feature.name,
            included=False,
            amount=amount,
            is_core=False,
        )
    return prices


async def addon_price(session: AsyncSession, plan_id: uuid.UUID, feature_key: str) -> AddonPrice:
    prices = await addon_prices(session, plan_id)
    price = prices.get(feature_key)
    if price is None:
        raise NotFound("No such module.")
    return price


# --- the account -------------------------------------------------------------


async def get_account(session: AsyncSession, tenant_id: uuid.UUID) -> BillingAccount:
    """This clinic's billing account, created empty on first use.

    The same shape as `tenants.get_config`: a row that should exist for every
    clinic and is made rather than demanded, so nothing has to be backfilled
    for a clinic that predates billing.
    """
    row = (
        await session.execute(
            select(BillingAccount).where(BillingAccount.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = BillingAccount(tenant_id=tenant_id)
        session.add(row)
        await session.flush()
    return row


# --- the subscription --------------------------------------------------------


async def active_subscription(
    session: AsyncSession, tenant_id: uuid.UUID, *, for_update: bool = False
) -> Subscription | None:
    """The clinic's live subscription, if it has one.

    `for_update` takes a row lock, and every job that might open a cycle or
    raise an invoice uses it. Two renewal runs landing on the same clinic at
    the same moment is not hypothetical — it is what a retry looks like — and
    the lock is what makes the second one wait and then find the work done.
    """
    stmt = (
        select(Subscription)
        .where(
            Subscription.tenant_id == tenant_id,
            Subscription.status.not_in(DEAD_STATUSES),
        )
        .order_by(Subscription.created_at.desc())
    )
    if for_update:
        # The plan is eagerly loaded, and an outer join cannot be locked.
        stmt = stmt.with_for_update(of=Subscription).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalars().first()


async def require_subscription(
    session: AsyncSession, tenant_id: uuid.UUID, *, for_update: bool = False
) -> Subscription:
    subscription = await active_subscription(session, tenant_id, for_update=for_update)
    if subscription is None:
        raise NotFound("This clinic does not have a subscription.")
    return subscription


# --- cycles ------------------------------------------------------------------


async def current_cycle(
    session: AsyncSession, subscription: Subscription, *, on: date
) -> BillingCycle | None:
    """The cycle `on` falls inside, if one has been opened for it."""
    return (
        await session.execute(
            select(BillingCycle).where(
                BillingCycle.subscription_id == subscription.id,
                BillingCycle.period_start <= on,
                BillingCycle.period_end > on,
            )
        )
    ).scalar_one_or_none()


async def latest_cycle(session: AsyncSession, subscription: Subscription) -> BillingCycle | None:
    return (
        await session.execute(
            select(BillingCycle)
            .where(BillingCycle.subscription_id == subscription.id)
            .order_by(BillingCycle.sequence.desc())
            .limit(1)
        )
    ).scalars().first()


async def open_cycle(
    session: AsyncSession,
    subscription: Subscription,
    *,
    period_start: date,
    plan: Plan | None = None,
) -> BillingCycle:
    """Open the cycle beginning on `period_start`, or return the one already there.

    Idempotent by lookup *and* by index: the `SELECT` is the fast path, and
    `uq_billing_cycle_period` is what actually holds when two workers race past
    it. A caller that sees an IntegrityError from this has been beaten to it,
    which is a success.

    `period_end` is the next anchored date — the first day of the following
    cycle — so the row records a real month, of whatever length that month
    happens to be.
    """
    existing = (
        await session.execute(
            select(BillingCycle).where(
                BillingCycle.subscription_id == subscription.id,
                BillingCycle.period_start == period_start,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    plan = plan or await get_plan(session, subscription.plan_id)
    previous = await latest_cycle(session, subscription)
    cycle = BillingCycle(
        tenant_id=subscription.tenant_id,
        subscription_id=subscription.id,
        sequence=(previous.sequence + 1) if previous else 1,
        period_start=period_start,
        period_end=next_anchor(period_start, subscription.anchor_day),
        status=BillingCycleStatus.OPEN,
        plan_id=plan.id,
        plan_amount=money(plan.amount),
    )
    session.add(cycle)
    await session.flush()
    return cycle


def anchor_from(started_on: date) -> int:
    """A subscription bills on the day of the month it started on.

    The first cycle therefore starts the day the clinic subscribed, not at the
    next anchor: they have the product from that moment, so they are billed
    from that moment.
    """
    return started_on.day


async def ensure_cycle(
    session: AsyncSession, subscription: Subscription, *, on: date, plan: Plan | None = None
) -> BillingCycle:
    """The cycle covering `on`, opening cycles forward until one covers it.

    A clinic whose renewal job has not run for three months gets three cycles
    opened in order rather than one long one — each with its own real length,
    each invoiceable on its own. The loop is bounded so a bad anchor cannot
    spin: a subscription cannot be more than a few years behind without
    somebody having noticed.
    """
    cycle = await current_cycle(session, subscription, on=on)
    if cycle is not None:
        return cycle

    latest = await latest_cycle(session, subscription)
    period_start = latest.period_end if latest else subscription.started_on
    for _ in range(120):
        cycle = await open_cycle(session, subscription, period_start=period_start, plan=plan)
        if cycle.period_end > on:
            return cycle
        period_start = cycle.period_end
    raise Conflict("This subscription's billing calendar is too far behind to catch up safely.")


# --- invoice numbering -------------------------------------------------------


async def next_invoice_number(session: AsyncSession, *, on: date) -> str:
    """`AST-202609-001042`.

    A PostgreSQL sequence, not a locked row. `nextval` is atomic and never
    rolls back, so two renewal jobs running at once get two numbers and neither
    waits for the other — and a number is never reused, which is exactly what
    an invoice number must promise. The clinic's own patient-invoice counter in
    `tenant_configs` is untouched: different ledger, different series.
    """
    value = (
        await session.execute(text("SELECT nextval('subscription_invoice_number_seq')"))
    ).scalar_one()
    prefix = settings.billing_invoice_prefix.strip("- ") or "AST"
    return f"{prefix}-{on:%Y%m}-{int(value):06d}"


# --- what the Settings → Billing screen shows --------------------------------


@dataclass(frozen=True)
class AddonView:
    feature_key: str
    name: str
    active: bool
    included: bool
    amount: Decimal | None
    activated_on: date | None
    #: The day a scheduled cancellation takes effect — the end of the cycle
    #: already paid for. `None` when nothing is scheduled.
    cancels_on: date | None
    charged_through: date | None


@dataclass(frozen=True)
class BillingSummary:
    """Everything the billing screen needs, in one read."""

    subscription: Subscription | None
    plan: Plan | None
    cycle: BillingCycle | None
    account: BillingAccount
    addons: tuple[AddonView, ...]
    #: What the next invoice will come to before tax, at today's configuration:
    #: the base plan plus every add-on that is not cancelling.
    recurring_subtotal: Decimal
    next_billing_on: date | None
    outstanding_total: Decimal
    outstanding_count: int


async def summary(session: AsyncSession, tenant_id: uuid.UUID, *, today: date) -> BillingSummary:
    """The billing screen's one query, assembled.

    Deliberately read-only apart from creating the account row: opening a
    browser tab must not open a billing cycle. The renewal job does that, on a
    schedule, where a failure is retried rather than shown to somebody.
    """
    account = await get_account(session, tenant_id)
    subscription = await active_subscription(session, tenant_id)

    if subscription is None:
        return BillingSummary(
            subscription=None,
            plan=None,
            cycle=None,
            account=account,
            addons=(),
            recurring_subtotal=ZERO,
            next_billing_on=None,
            outstanding_total=ZERO,
            outstanding_count=0,
        )

    plan = await get_plan(session, subscription.plan_id)
    cycle = await current_cycle(session, subscription, on=today)
    prices = await addon_prices(session, plan.id)

    entitlements = {
        row.feature_key: row
        for row in (
            await session.execute(
                select(TenantFeature).where(TenantFeature.tenant_id == tenant_id)
            )
        ).scalars()
    }

    views: list[AddonView] = []
    recurring = money(plan.amount)
    for key in sorted(prices):
        price = prices[key]
        entitlement = entitlements.get(key)
        active = entitlement is not None and entitlement.status == TenantFeatureStatus.ACTIVE
        cancels_on = entitlement.cancel_at if entitlement else None
        views.append(
            AddonView(
                feature_key=key,
                name=price.name,
                active=active,
                included=price.included,
                amount=price.amount,
                activated_on=entitlement.activated_at if entitlement else None,
                cancels_on=cancels_on,
                charged_through=entitlement.charged_through if entitlement else None,
            )
        )
        # A module cancelling at the end of the cycle is still on, and still
        # paid for — but it is not part of what the *next* invoice will say.
        if active and price.billable and cancels_on is None:
            recurring += price.amount or ZERO

    unpaid = list(
        (
            await session.execute(
                select(SubscriptionInvoice).where(
                    SubscriptionInvoice.tenant_id == tenant_id,
                    SubscriptionInvoice.status.in_(
                        (
                            SubscriptionInvoiceStatus.ISSUED,
                            SubscriptionInvoiceStatus.PARTIALLY_PAID,
                            SubscriptionInvoiceStatus.UNCOLLECTIBLE,
                        )
                    ),
                )
            )
        ).scalars()
    )
    outstanding = money(sum((balance_of(i) for i in unpaid), ZERO))

    return BillingSummary(
        subscription=subscription,
        plan=plan,
        cycle=cycle,
        account=account,
        addons=tuple(views),
        recurring_subtotal=money(recurring),
        next_billing_on=next_billing_date(subscription, cycle, today=today),
        outstanding_total=outstanding,
        outstanding_count=len([i for i in unpaid if balance_of(i) > ZERO]),
    )


def next_billing_date(
    subscription: Subscription, cycle: BillingCycle | None, *, today: date
) -> date | None:
    """When the clinic is next charged, or `None` if it is not going to be.

    A subscription cancelling at the end of the cycle has no next billing date,
    and saying so is the honest answer for a screen that also shows the
    cancellation.
    """
    if subscription.status in DEAD_STATUSES or subscription.cancel_at_cycle_end:
        return None
    if subscription.status == SubscriptionStatus.PAUSED:
        return None
    if cycle is not None:
        return cycle.period_end
    return next_anchor(today, subscription.anchor_day)


def balance_of(invoice: SubscriptionInvoice) -> Decimal:
    """What is still owed on an invoice.

    Derived from the payments rather than stored, for the same reason the
    patient ledger derives its own: a status can then never contradict the
    money. Only a `captured` payment counts — an authorised one is money the
    gateway has permission to take and has not taken.
    """
    from app.models.enums import SubscriptionPaymentStatus

    if invoice.status in (SubscriptionInvoiceStatus.VOID, SubscriptionInvoiceStatus.DRAFT):
        return ZERO
    paid = sum(
        (
            money(p.amount) - money(p.amount_refunded)
            for p in invoice.payments
            if p.status
            in (
                SubscriptionPaymentStatus.CAPTURED,
                SubscriptionPaymentStatus.PARTIALLY_REFUNDED,
            )
        ),
        ZERO,
    )
    balance = money(invoice.total) - money(paid)
    return balance if balance > ZERO else ZERO


def paid_amount(invoice: SubscriptionInvoice) -> Decimal:
    from app.models.enums import SubscriptionPaymentStatus

    return money(
        sum(
            (
                money(p.amount) - money(p.amount_refunded)
                for p in invoice.payments
                if p.status
                in (
                    SubscriptionPaymentStatus.CAPTURED,
                    SubscriptionPaymentStatus.PARTIALLY_REFUNDED,
                )
            ),
            ZERO,
        )
    )


def status_after_payment(invoice: SubscriptionInvoice) -> SubscriptionInvoiceStatus:
    """What an invoice's status should be, given the money against it.

    Never called on a draft or a void invoice: those are states about the
    invoice itself rather than about what has been paid.
    """
    if invoice.status in (SubscriptionInvoiceStatus.VOID, SubscriptionInvoiceStatus.DRAFT):
        return SubscriptionInvoiceStatus(invoice.status)
    paid = paid_amount(invoice)
    if paid <= ZERO:
        return SubscriptionInvoiceStatus.ISSUED
    if paid < money(invoice.total):
        return SubscriptionInvoiceStatus.PARTIALLY_PAID
    return SubscriptionInvoiceStatus.PAID


__all__ = [
    "AddonPrice", "AddonView", "BillingSummary", "DEAD_STATUSES", "LIVE_STATUSES",
    "active_subscription", "addon_price", "addon_prices", "anchor_from", "balance_of",
    "current_cycle", "ensure_cycle", "get_account", "get_plan",
    "latest_cycle", "list_plans", "next_billing_date", "next_invoice_number", "open_cycle",
    "paid_amount", "plan_by_code", "require_subscription", "status_after_payment", "summary",
]
