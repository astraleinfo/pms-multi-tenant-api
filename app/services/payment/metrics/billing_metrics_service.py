"""Billing metrics for the super-admin console.

MRR, revenue, subscription movement, payment success, outstanding invoices,
refunds, and which add-ons clinics actually buy — filterable by date, clinic,
plan, feature, payment status and subscription status.

One structural decision runs through the whole file: **every figure is
aggregated one clinic at a time, with that clinic bound to the session.** There
is no `BYPASSRLS` role in this application and the console does not get one; a
cross-tenant `SUM` would need exactly that. `platform.snapshot` already counts
patients and encounters this way, and the money is counted the same way, for
the same reason — the console is a privileged *view*, not a privileged *query*.

Three definitions worth being explicit about, because a metrics screen that
does not say what it means is worse than no metrics screen:

*   **MRR** is forward-looking: what the live subscriptions would bill in a
    normal month at today's configuration — base plans plus add-ons that are
    not cancelling. It is not last month's revenue, and a clinic partway
    through cancelling is already out of it.
*   **Revenue** is money actually captured inside the window, and **net
    revenue** is that less refunds processed in the same window. It is cash,
    not accrual: an invoice raised and unpaid is in *outstanding*, not here.
*   **Stock figures describe now, movement figures describe the window.** MRR,
    active subscriptions and outstanding invoices ignore the date filter on
    purpose — "MRR between March and April" is not a question with an answer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import set_tenant
from app.models.billing import (
    Plan,
    Subscription,
    SubscriptionInvoice,
    SubscriptionInvoiceItem,
    SubscriptionPayment,
    SubscriptionRefund,
)
from app.models.enums import (
    InvoiceItemKind,
    RefundStatus,
    SubscriptionInvoiceStatus,
    SubscriptionPaymentStatus,
    SubscriptionStatus,
    TenantFeatureStatus,
)
from app.models.tenancy import Feature, Tenant, TenantFeature
from app.services.payment import billing_service
from app.services.payment.money import ZERO, money

#: Payment states that mean money reached the platform. A refunded payment is
#: still revenue that was collected — the refund is counted separately, so that
#: "took ₹10,000, gave ₹2,000 back" reads as two facts rather than one net one.
SETTLED = (
    SubscriptionPaymentStatus.CAPTURED,
    SubscriptionPaymentStatus.PARTIALLY_REFUNDED,
    SubscriptionPaymentStatus.REFUNDED,
)
#: Invoice states that still owe something.
UNPAID = (
    SubscriptionInvoiceStatus.ISSUED,
    SubscriptionInvoiceStatus.PARTIALLY_PAID,
    SubscriptionInvoiceStatus.UNCOLLECTIBLE,
)


@dataclass(frozen=True)
class MetricsFilter:
    """What the console narrowed to. Every field is optional and they compose."""

    start: date | None = None
    end: date | None = None
    tenant_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None
    feature_key: str | None = None
    payment_status: SubscriptionPaymentStatus | None = None
    subscription_status: SubscriptionStatus | None = None

    def covers(self, value: date | None) -> bool:
        if value is None:
            return False
        if self.start is not None and value < self.start:
            return False
        if self.end is not None and value > self.end:
            return False
        return True


@dataclass
class AddonMetric:
    """Adoption and revenue for one purchasable module."""

    feature_key: str
    name: str
    #: How many clinics have it on right now.
    active_tenants: int = 0
    #: …of which, how many have asked for it to stop at the cycle end.
    cancelling: int = 0
    #: What it bills per month across every clinic that keeps it — its share of
    #: MRR.
    monthly_revenue: Decimal = ZERO
    #: What it was actually invoiced for inside the window, from the line items.
    billed_in_window: Decimal = ZERO


@dataclass
class TenantMetric:
    """One clinic's row on the console table."""

    tenant_id: uuid.UUID
    name: str
    slug: str
    plan: str = ""
    subscription_status: str = ""
    mrr: Decimal = ZERO
    revenue: Decimal = ZERO
    outstanding: Decimal = ZERO
    active_addons: int = 0


@dataclass
class BillingMetrics:
    """Everything the metrics screen shows."""

    mrr: Decimal = ZERO
    arr: Decimal = ZERO
    revenue: Decimal = ZERO
    refunded: Decimal = ZERO
    net_revenue: Decimal = ZERO

    subscriptions_active: int = 0
    subscriptions_trialing: int = 0
    subscriptions_paused: int = 0
    subscriptions_past_due: int = 0
    subscriptions_new: int = 0
    subscriptions_cancelled: int = 0
    cancellations_scheduled: int = 0

    payments_successful: int = 0
    payments_failed: int = 0
    refunds_count: int = 0

    invoices_issued: int = 0
    invoices_paid: int = 0
    invoices_outstanding: int = 0
    outstanding_total: Decimal = ZERO

    addons: list[AddonMetric] = field(default_factory=list)
    tenants: list[TenantMetric] = field(default_factory=list)

    @property
    def payment_success_rate(self) -> float:
        """Captured as a share of everything attempted. `0.0` when nothing was."""
        attempted = self.payments_successful + self.payments_failed
        return round(self.payments_successful / attempted, 4) if attempted else 0.0


def _window(stmt: Select, column, filters: MetricsFilter) -> Select:
    if filters.start is not None:
        stmt = stmt.where(column >= filters.start)
    if filters.end is not None:
        stmt = stmt.where(column <= filters.end)
    return stmt


async def report(session: AsyncSession, filters: MetricsFilter | None = None) -> BillingMetrics:
    """The whole report, one clinic at a time.

    The session is left with no tenant bound when this returns, exactly as
    `platform.snapshot` leaves it — a console handler that forgot would
    otherwise carry one clinic's binding into whatever it did next.
    """
    filters = filters or MetricsFilter()
    metrics = BillingMetrics()

    features = {row.key: row for row in (await session.execute(select(Feature))).scalars()}
    plans = {row.id: row for row in (await session.execute(select(Plan))).scalars()}
    addons: dict[str, AddonMetric] = {
        key: AddonMetric(feature_key=key, name=feature.name)
        for key, feature in sorted(features.items())
        if not feature.is_core
    }

    tenants = select(Tenant).order_by(Tenant.name)
    if filters.tenant_id is not None:
        tenants = tenants.where(Tenant.id == filters.tenant_id)

    for tenant in (await session.execute(tenants)).scalars():
        await set_tenant(session, tenant.id)
        row = await _one_tenant(session, tenant, filters, metrics, plans=plans, addons=addons)
        if row is not None:
            metrics.tenants.append(row)

    await set_tenant(session, None)

    # Filtering by feature narrows which add-on rows are *reported* without
    # changing the totals: "how is the lab doing" is a question about the lab,
    # not a claim that the lab is all the revenue there is.
    reported = [
        addon
        for addon in addons.values()
        if filters.feature_key is None or addon.feature_key == filters.feature_key
    ]
    metrics.addons = sorted(reported, key=lambda a: (-a.active_tenants, a.feature_key))
    for addon in metrics.addons:
        addon.monthly_revenue = money(addon.monthly_revenue)
        addon.billed_in_window = money(addon.billed_in_window)

    metrics.mrr = money(metrics.mrr)
    metrics.arr = money(metrics.mrr * 12)
    metrics.revenue = money(metrics.revenue)
    metrics.refunded = money(metrics.refunded)
    metrics.net_revenue = money(metrics.revenue - metrics.refunded)
    metrics.outstanding_total = money(metrics.outstanding_total)
    return metrics


async def _one_tenant(
    session: AsyncSession,
    tenant: Tenant,
    filters: MetricsFilter,
    metrics: BillingMetrics,
    *,
    plans: dict[uuid.UUID, Plan],
    addons: dict[str, AddonMetric],
) -> TenantMetric | None:
    """One clinic's contribution to every figure, with that clinic bound.

    Returns `None` — and contributes nothing — for a clinic with no
    subscription, or one the plan/status filters exclude. A clinic that has
    never subscribed is not a zero in the average; it is not in the population.
    """
    subscription = (
        await session.execute(
            select(Subscription)
            .where(Subscription.tenant_id == tenant.id)
            .order_by(Subscription.created_at.desc())
        )
    ).scalars().first()
    if subscription is None:
        return None
    if filters.plan_id is not None and subscription.plan_id != filters.plan_id:
        return None
    if filters.subscription_status is not None and (
        SubscriptionStatus(subscription.status) != filters.subscription_status
    ):
        return None

    plan = plans.get(subscription.plan_id)
    status = SubscriptionStatus(subscription.status)
    row = TenantMetric(
        tenant_id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        plan=plan.name if plan else "",
        subscription_status=status.value,
    )

    # --- stock: where this clinic stands today ------------------------------
    metrics.subscriptions_active += status == SubscriptionStatus.ACTIVE
    metrics.subscriptions_trialing += status == SubscriptionStatus.TRIALING
    metrics.subscriptions_paused += status == SubscriptionStatus.PAUSED
    metrics.subscriptions_past_due += status == SubscriptionStatus.PAST_DUE
    metrics.cancellations_scheduled += bool(subscription.cancel_at_cycle_end)

    # A paused or cancelling subscription bills nothing next month, so it is
    # not in MRR — which is the figure's whole point.
    counts_towards_mrr = (
        status not in billing_service.DEAD_STATUSES
        and status != SubscriptionStatus.PAUSED
        and not subscription.cancel_at_cycle_end
    )
    if counts_towards_mrr and plan is not None:
        row.mrr += money(plan.amount)

    prices = await billing_service.addon_prices(session, subscription.plan_id)
    entitlements = (
        await session.execute(
            select(TenantFeature).where(
                TenantFeature.tenant_id == tenant.id,
                TenantFeature.status == TenantFeatureStatus.ACTIVE,
            )
        )
    ).scalars()
    for entitlement in entitlements:
        price = prices.get(entitlement.feature_key)
        addon = addons.get(entitlement.feature_key)
        if price is None or addon is None:
            continue

        # Adoption and revenue are different questions, and the split matters:
        # a module the clinic's plan *bundles* is switched on and earns nothing
        # as an add-on. Counting it in `active_tenants` is what makes "how many
        # practices have the lab" true; leaving it out of `monthly_revenue` is
        # what keeps MRR from double-counting the base plan that already
        # includes it.
        addon.active_tenants += 1
        if not price.billable:
            continue

        row.active_addons += 1
        if entitlement.cancel_at is not None:
            addon.cancelling += 1
            continue
        if counts_towards_mrr:
            row.mrr += price.amount or ZERO
            addon.monthly_revenue += price.amount or ZERO

    # --- movement: what happened inside the window --------------------------
    if filters.covers(subscription.started_on):
        metrics.subscriptions_new += 1
    if filters.covers(subscription.cancelled_at):
        metrics.subscriptions_cancelled += 1

    await _money(session, tenant.id, filters, metrics, row, addons)

    metrics.mrr += row.mrr
    metrics.revenue += row.revenue
    metrics.outstanding_total += row.outstanding
    row.mrr = money(row.mrr)
    row.revenue = money(row.revenue)
    row.outstanding = money(row.outstanding)
    return row


async def _money(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: MetricsFilter,
    metrics: BillingMetrics,
    row: TenantMetric,
    addons: dict[str, AddonMetric],
) -> None:
    """Invoices, payments, refunds and add-on revenue for one bound clinic."""
    invoices = (
        await session.execute(
            _window(
                select(SubscriptionInvoice).where(SubscriptionInvoice.tenant_id == tenant_id),
                SubscriptionInvoice.issued_on,
                filters,
            )
        )
    ).scalars()
    for invoice in invoices:
        if invoice.status == SubscriptionInvoiceStatus.VOID:
            continue
        metrics.invoices_issued += 1
        if invoice.status == SubscriptionInvoiceStatus.PAID:
            metrics.invoices_paid += 1

    # Outstanding describes the present, so it is deliberately not windowed: an
    # unpaid invoice from March is still unpaid in June.
    owing = (
        await session.execute(
            select(SubscriptionInvoice).where(
                SubscriptionInvoice.tenant_id == tenant_id,
                SubscriptionInvoice.status.in_(UNPAID),
            )
        )
    ).scalars()
    for invoice in owing:
        balance = billing_service.balance_of(invoice)
        if balance > ZERO:
            metrics.invoices_outstanding += 1
            row.outstanding += balance

    payments = (
        await session.execute(
            _window(
                select(SubscriptionPayment).where(SubscriptionPayment.tenant_id == tenant_id),
                func.date(SubscriptionPayment.created_at),
                filters,
            )
        )
    ).scalars()
    for payment in payments:
        status = SubscriptionPaymentStatus(payment.status)
        if filters.payment_status is not None and status != filters.payment_status:
            continue
        if status in SETTLED:
            metrics.payments_successful += 1
            row.revenue += money(payment.amount)
        elif status == SubscriptionPaymentStatus.FAILED:
            metrics.payments_failed += 1

    refunds = (
        await session.execute(
            _window(
                select(SubscriptionRefund).where(
                    SubscriptionRefund.tenant_id == tenant_id,
                    SubscriptionRefund.status == RefundStatus.PROCESSED,
                ),
                func.date(SubscriptionRefund.created_at),
                filters,
            )
        )
    ).scalars()
    for refund in refunds:
        metrics.refunds_count += 1
        metrics.refunded += money(refund.amount)

    # What each add-on was actually invoiced for in the window — the one add-on
    # figure that is history rather than projection. Summed in SQL because it
    # is the only place here where the row count is unbounded by clinics.
    stmt = _window(
        select(SubscriptionInvoiceItem.feature_key, func.sum(SubscriptionInvoiceItem.amount))
        .join(
            SubscriptionInvoice, SubscriptionInvoice.id == SubscriptionInvoiceItem.invoice_id
        )
        .where(
            SubscriptionInvoiceItem.tenant_id == tenant_id,
            SubscriptionInvoiceItem.feature_key.is_not(None),
            SubscriptionInvoiceItem.kind.in_(
                (InvoiceItemKind.ADDON, InvoiceItemKind.PRORATION)
            ),
            SubscriptionInvoice.status != SubscriptionInvoiceStatus.VOID,
        )
        .group_by(SubscriptionInvoiceItem.feature_key),
        SubscriptionInvoice.issued_on,
        filters,
    )
    for feature_key, total in await session.execute(stmt):
        addon = addons.get(feature_key)
        if addon is not None:
            addon.billed_in_window += money(total)


__all__ = [
    "SETTLED", "UNPAID", "AddonMetric", "BillingMetrics", "MetricsFilter", "TenantMetric", "report",
]
