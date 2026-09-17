"""Building an invoice, once.

Requirement 10, literally:

    Base Plan + Add-ons + Proration - Discounts/Credits + Taxes = Invoice Total

and requirement 9's "immutable" is not a convention kept here — it is a trigger
and two rules in the database (migration 0009). This module's job is to get the
lines right the first time, because there is no second time.

Two invariants, both load-bearing:

*   **One invoice per cycle.** The `uq_subscription_invoice_per_cycle` partial
    index enforces it; this module looks first only so the common case is a
    read rather than a caught error. A job that runs twice produces one
    invoice, and a job that half-ran and died produces one invoice.
*   **An add-on is charged for a period at most once.** `charged_through` on
    the entitlement row is advanced in the same transaction as the invoice, so
    a mid-cycle activation that was already billed pro rata is not billed again
    by the renewal that closes that same cycle.

The totals are stored rather than recomputed on read. An invoice is a statement
of what was charged on a day; if a price changes next month, last month's
invoice must still say what it said.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import Conflict
from app.models.billing import (
    BillingCycle,
    Plan,
    Subscription,
    SubscriptionInvoice,
    SubscriptionInvoiceItem,
)
from app.models.enums import (
    InvoiceItemKind,
    SubscriptionInvoiceStatus,
    TenantFeatureStatus,
)
from app.models.tenancy import TenantFeature
from app.services.payment import billing_service
from app.services.payment.money import ZERO, money, now
from app.services.payment.proration import calculator as proration
from app.services.payment.tax import calculator as tax_calculator

log = logging.getLogger("pms.billing")


@dataclass
class DraftLine:
    """A line on its way to becoming a row. Ordering is assigned at the end, in
    the order requirement 10 states the sum in."""

    kind: InvoiceItemKind
    description: str
    amount: Decimal
    unit_amount: Decimal = ZERO
    quantity: int = 1
    feature_key: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    prorated_days: int | None = None
    cycle_days: int | None = None
    tax_code: str = ""
    tax_percent: Decimal | None = None


@dataclass(frozen=True)
class Credit:
    """Something taken off before tax — a promotional discount, or the unused
    remainder of a plan the clinic has moved off partway through a cycle.

    Always a positive amount here; it is written to the invoice as a negative
    line, so the line items still add up to the subtotal.
    """

    description: str
    amount: Decimal
    kind: InvoiceItemKind = InvoiceItemKind.CREDIT


#: The order lines appear on the invoice, and it is requirement 10's order.
_KIND_ORDER = {
    InvoiceItemKind.BASE_PLAN: 0,
    InvoiceItemKind.ADDON: 1,
    InvoiceItemKind.PRORATION: 2,
    InvoiceItemKind.DISCOUNT: 3,
    InvoiceItemKind.CREDIT: 4,
    InvoiceItemKind.TAX: 5,
}


async def invoice_for_cycle(
    session: AsyncSession, cycle: BillingCycle
) -> SubscriptionInvoice | None:
    """The cycle's invoice, if one has been raised and not voided."""
    return (
        await session.execute(
            select(SubscriptionInvoice).where(
                SubscriptionInvoice.billing_cycle_id == cycle.id,
                SubscriptionInvoice.status != SubscriptionInvoiceStatus.VOID,
            )
        )
    ).scalar_one_or_none()


async def generate_for_cycle(
    session: AsyncSession,
    *,
    subscription: Subscription,
    cycle: BillingCycle,
    today: date,
    credits: tuple[Credit, ...] = (),
) -> tuple[SubscriptionInvoice, bool]:
    """The invoice for one billing cycle. Returns `(invoice, created)`.

    `created` is False when the cycle already had one — which is the normal
    answer on a retry, and the reason the caller can run this as often as it
    likes.

    The caller is expected to hold the subscription row lock (every job here
    takes it through `billing_service.active_subscription(for_update=True)`),
    so two workers cannot both get past the existence check below. The unique
    index is what holds if one ever does.
    """
    existing = await invoice_for_cycle(session, cycle)
    if existing is not None:
        return existing, False

    plan = await billing_service.get_plan(session, cycle.plan_id)
    lines: list[DraftLine] = [
        DraftLine(
            kind=InvoiceItemKind.BASE_PLAN,
            description=(
                f"{plan.name} — {cycle.period_start:%d %b %Y} "
                f"to {_inclusive(cycle.period_end):%d %b %Y}"
            ),
            amount=money(cycle.plan_amount),
            unit_amount=money(cycle.plan_amount),
            period_start=cycle.period_start,
            period_end=cycle.period_end,
        )
    ]
    lines.extend(
        await _addon_lines(session, subscription=subscription, cycle=cycle, plan=plan)
    )
    lines.extend(_credit_lines(credits))

    invoice = await _finalise(
        session,
        subscription=subscription,
        cycle=cycle,
        lines=lines,
        today=today,
        period_start=cycle.period_start,
        period_end=cycle.period_end,
    )
    return invoice, True


async def generate_one_off(
    session: AsyncSession,
    *,
    subscription: Subscription,
    lines: list[DraftLine],
    today: date,
    period_start: date,
    period_end: date,
) -> SubscriptionInvoice:
    """An invoice that does not belong to a cycle.

    What an add-on bought mid-cycle produces: a single prorated charge, billed
    now rather than waiting for the renewal. It carries no `billing_cycle_id`,
    so the one-invoice-per-cycle index does not apply to it and the cycle's own
    invoice is still raised at renewal.
    """
    if not lines:
        raise Conflict("An invoice needs at least one line.")
    return await _finalise(
        session,
        subscription=subscription,
        cycle=None,
        lines=lines,
        today=today,
        period_start=period_start,
        period_end=period_end,
    )


# --- the add-on half ---------------------------------------------------------


async def _addon_lines(
    session: AsyncSession, *, subscription: Subscription, cycle: BillingCycle, plan: Plan
) -> list[DraftLine]:
    """What each switched-on module adds to this cycle's invoice.

    Four rules, and each one is a requirement rather than a preference:

    *   a module cancelled before this cycle begins is not billed (7);
    *   a module already paid up to or past the end of this cycle is not billed
        again — that is a mid-cycle activation which was charged pro rata at the
        time, and this is the run that would otherwise double-charge it (8);
    *   a module cancelling *at* the end of this cycle is still billed for this
        cycle if it has not been paid for yet, because the clinic keeps the
        feature until then (7);
    *   anything else is prorated from the later of the cycle start and the day
        it was switched on, over the days this cycle actually has (5, 6).

    `charged_through` is advanced here, in the same transaction as the invoice,
    so the two can never disagree.
    """
    prices = await billing_service.addon_prices(session, plan.id)
    entitlements = list(
        (
            await session.execute(
                select(TenantFeature).where(
                    TenantFeature.tenant_id == subscription.tenant_id,
                    TenantFeature.status == TenantFeatureStatus.ACTIVE,
                )
            )
        ).scalars()
    )

    lines: list[DraftLine] = []
    for entitlement in sorted(entitlements, key=lambda e: e.feature_key):
        price = prices.get(entitlement.feature_key)
        if price is None or not price.billable:
            continue
        if entitlement.cancel_at is not None and entitlement.cancel_at <= cycle.period_start:
            continue
        if entitlement.charged_through is not None and entitlement.charged_through >= cycle.period_end:
            continue

        effective = max(
            cycle.period_start,
            entitlement.activated_at or cycle.period_start,
            entitlement.charged_through or cycle.period_start,
        )
        share = proration.prorate(
            full_amount=price.amount or ZERO,
            period_start=cycle.period_start,
            period_end=cycle.period_end,
            effective_from=effective,
        )
        if share.amount <= ZERO:
            continue

        full = share.is_full
        lines.append(
            DraftLine(
                kind=InvoiceItemKind.ADDON if full else InvoiceItemKind.PRORATION,
                description=(
                    f"{price.name} add-on"
                    if full
                    else f"{price.name} add-on — {share.fraction} days"
                ),
                amount=share.amount,
                unit_amount=share.full_amount,
                feature_key=entitlement.feature_key,
                period_start=share.period_start,
                period_end=share.period_end,
                prorated_days=share.charged_days,
                cycle_days=share.cycle_days,
            )
        )
        entitlement.charged_through = cycle.period_end

    return lines


def _credit_lines(credits: tuple[Credit, ...]) -> list[DraftLine]:
    return [
        DraftLine(
            kind=credit.kind,
            description=credit.description,
            # Written negative so the line items still sum to the subtotal.
            amount=-money(credit.amount),
            unit_amount=money(credit.amount),
        )
        for credit in credits
        if money(credit.amount) > ZERO
    ]


# --- assembly ----------------------------------------------------------------


async def _finalise(
    session: AsyncSession,
    *,
    subscription: Subscription,
    cycle: BillingCycle | None,
    lines: list[DraftLine],
    today: date,
    period_start: date,
    period_end: date,
) -> SubscriptionInvoice:
    """Total the lines, add tax, write the invoice, and issue it.

    Issued immediately rather than left as a draft: a draft invoice is one
    somebody is still editing, and nobody edits these — they are generated from
    the agreement and the calendar, and the next thing that happens to one is
    that it is paid.
    """
    account = await billing_service.get_account(session, subscription.tenant_id)
    plan = await billing_service.get_plan(session, subscription.plan_id)

    charges = money(sum((line.amount for line in lines if line.amount > ZERO), ZERO))
    credits_total = money(-sum((line.amount for line in lines if line.amount < ZERO), ZERO))
    net = money(charges - credits_total)

    breakdown = await tax_calculator.calculate(
        session,
        taxable_base=net,
        on=today,
        country=account.country,
        scope=tax_calculator.scope_for(
            supplier_region=settings.billing_supplier_region,
            customer_region=account.place_of_supply,
        ),
    )
    for component in breakdown.components:
        lines.append(
            DraftLine(
                kind=InvoiceItemKind.TAX,
                description=f"{component.name} @ {_trim(component.percent)}%",
                amount=component.amount,
                unit_amount=component.amount,
                tax_code=component.code,
                tax_percent=component.percent,
            )
        )

    total = money(net + breakdown.total)

    invoice = SubscriptionInvoice(
        tenant_id=subscription.tenant_id,
        subscription_id=subscription.id,
        billing_cycle_id=cycle.id if cycle is not None else None,
        number=await billing_service.next_invoice_number(session, on=today),
        status=SubscriptionInvoiceStatus.DRAFT,
        currency=plan.currency,
        issued_on=today,
        due_on=today + timedelta(days=max(settings.billing_payment_terms_days, 0)),
        period_start=period_start,
        period_end=period_end,
        subtotal=charges,
        discount_total=credits_total,
        tax_total=breakdown.total,
        total=total,
    )
    session.add(invoice)
    await session.flush()

    ordered = sorted(lines, key=lambda line: (_KIND_ORDER[line.kind], line.description))
    for position, line in enumerate(ordered):
        session.add(
            SubscriptionInvoiceItem(
                tenant_id=subscription.tenant_id,
                invoice_id=invoice.id,
                position=position,
                kind=line.kind,
                description=line.description[:240],
                feature_key=line.feature_key,
                quantity=line.quantity,
                unit_amount=money(line.unit_amount),
                amount=money(line.amount),
                period_start=line.period_start,
                period_end=line.period_end,
                prorated_days=line.prorated_days,
                cycle_days=line.cycle_days,
                tax_code=line.tax_code,
                tax_percent=line.tax_percent,
            )
        )

    # Issued last, so the freeze trigger has nothing to object to: everything
    # above happened while the row was still a draft.
    invoice.status = SubscriptionInvoiceStatus.ISSUED
    invoice.issued_at = now()
    await session.flush()
    await session.refresh(invoice, ["items", "payments"])

    log.info(
        "raised subscription invoice %s for tenant %s: %s %s",
        invoice.number, subscription.tenant_id, invoice.currency, invoice.total,
    )
    return invoice


def _inclusive(period_end: date) -> date:
    """The last day *inside* a half-open period, for human-readable text.

    The period is `[start, end)`; a person reading "1 Feb to 1 Mar" on an
    invoice for February would reasonably ask whether they are paying for the
    1st of March.
    """
    return period_end - timedelta(days=1)


def _trim(percent: Decimal) -> str:
    """`18` rather than `18.000`, `2.5` rather than `2.500`."""
    text = f"{percent:.3f}".rstrip("0").rstrip(".")
    return text or "0"


async def void_invoice(
    session: AsyncSession, tenant_id: uuid.UUID, invoice_id: uuid.UUID, *, reason: str = ""
) -> SubscriptionInvoice:
    """Void an invoice that should not have been raised.

    Voiding is the only way to unmake one — the figures cannot be edited, by
    anybody, so a mistake is corrected by a void and a re-raise. A void invoice
    releases the one-per-cycle index, which is precisely what lets the cycle be
    re-invoiced afterwards.
    """
    invoice = (
        await session.execute(
            select(SubscriptionInvoice).where(
                SubscriptionInvoice.id == invoice_id,
                SubscriptionInvoice.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if invoice is None:
        raise Conflict("No such invoice.")
    if invoice.status == SubscriptionInvoiceStatus.VOID:
        return invoice
    if billing_service.paid_amount(invoice) > ZERO:
        raise Conflict(
            "That invoice has money against it. Refund the payment before voiding it."
        )
    invoice.status = SubscriptionInvoiceStatus.VOID
    invoice.voided_at = now()
    if reason:
        invoice.notes = (invoice.notes + "\n" + reason).strip()
    await _release_addon_charges(session, invoice)
    await session.flush()
    return invoice


async def _release_addon_charges(
    session: AsyncSession, invoice: SubscriptionInvoice
) -> None:
    """Undo a voided invoice's effect on `charged_through`.

    Without this, voiding and re-raising silently loses the add-on lines: the
    void does not un-charge anything, but `charged_through` still says the
    module is paid to the end of the cycle, so the replacement invoice skips
    it. The clinic keeps the module and stops being billed for it.

    Each line is rolled back to the date it was charged *from* rather than to
    null, because that is the value that reproduces the same line. The
    generator computes `effective = max(period_start, activated_at,
    charged_through)`, and the line recorded exactly that date — so restoring it
    makes the re-raise produce an identical charge, and leaves any earlier
    one-off invoice for the same module undisturbed.
    """
    await session.refresh(invoice, ["items"])
    for item in invoice.items:
        if item.feature_key is None or item.kind not in (
            InvoiceItemKind.ADDON,
            InvoiceItemKind.PRORATION,
        ):
            continue
        row = (
            await session.execute(
                select(TenantFeature).where(
                    TenantFeature.tenant_id == invoice.tenant_id,
                    TenantFeature.feature_key == item.feature_key,
                )
            )
        ).scalar_one_or_none()
        if row is not None and row.charged_through == invoice.period_end:
            row.charged_through = item.period_start
