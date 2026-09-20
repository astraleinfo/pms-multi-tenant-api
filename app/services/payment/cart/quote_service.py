"""What the cart comes to, before anybody is charged.

Requirement 4 — "see the individual price and combined total before checkout"
— and the whole file writes nothing.

Two figures, never one. A module bought on the 15th of a 31-day month is
charged for 17 days now and for the whole month from then on, and a screen
showing a single number will be read as the wrong one of those. So a quote
carries `total` (what leaving this page costs) and `recurring_total` (what
every invoice after it carries), and the proration workings beside each line
so the smaller figure can explain itself.

Nothing here decides money on its own. Each line is priced by
`addon_service.assess`, which is the same function the purchase uses, and the
tax comes from the same `tax_calculator.calculate` the invoice runs through —
so a quote that disagreed with the invoice it turns into would be a bug in one
shared function rather than a drift between two.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.enums import Currency, PlanInterval
from app.services.payment import billing_service
from app.services.payment.addon import billing_service as addon_service
from app.services.payment.cart import cart_service
from app.services.payment.money import ZERO, money
from app.services.payment.tax import calculator as tax_calculator


@dataclass(frozen=True)
class QuoteLine:
    """One module in the cart, priced two ways."""

    feature_key: str
    name: str
    interval: PlanInterval
    #: The full price of a whole cycle — the figure that recurs.
    list_amount: Decimal
    #: What this line adds to the payment being made now. Zero is an ordinary
    #: answer: the module may be free on this plan, already paid for this
    #: cycle, or simply have its scheduled cancellation dropped.
    amount_now: Decimal
    prorated_days: int | None = None
    cycle_days: int | None = None
    #: Nothing is charged because the cancellation was called off.
    reinstated: bool = False
    #: Nothing is charged because this cycle was already paid for.
    already_paid: bool = False
    #: Why this line cannot be bought, or None. A cart shows every problem at
    #: once rather than failing on the first.
    blocked: str | None = None


@dataclass(frozen=True)
class Quote:
    lines: list[QuoteLine]
    currency: Currency
    #: Charged now, before tax.
    subtotal: Decimal
    tax_total: Decimal
    #: Charged now, in full. This is the figure the payment is for.
    total: Decimal
    #: What these modules add to every invoice after this one, before tax.
    recurring_total: Decimal
    #: The cycle the proration is measured against.
    period_start: date
    period_end: date

    @property
    def purchasable(self) -> bool:
        return bool(self.lines) and not any(line.blocked for line in self.lines)

    @property
    def blocked_reasons(self) -> list[str]:
        return [line.blocked for line in self.lines if line.blocked]


async def quote(session: AsyncSession, tenant_id: uuid.UUID, *, today: date) -> Quote:
    """Price the open cart as it stands. Changes nothing."""
    cart = await cart_service.current(session, tenant_id)
    items = sorted(cart.items, key=lambda i: i.feature_key) if cart is not None else []

    subscription = await billing_service.require_subscription(session, tenant_id)
    cycle = await billing_service.latest_cycle(session, subscription)
    if cycle is None:
        cycle = await billing_service.ensure_cycle(session, subscription, on=today)
    plan = await billing_service.get_plan(session, subscription.plan_id)

    lines: list[QuoteLine] = []
    for item in items:
        verdict = await addon_service.assess(
            session,
            tenant_id,
            item.feature_key,
            subscription=subscription,
            cycle=cycle,
            today=today,
        )
        share = verdict.proration
        lines.append(
            QuoteLine(
                feature_key=item.feature_key,
                name=getattr(verdict.price, "name", item.feature_key),
                interval=item.interval,
                list_amount=verdict.recurring,
                amount_now=verdict.amount,
                prorated_days=share.charged_days if share is not None else None,
                cycle_days=share.cycle_days if share is not None else None,
                reinstated=verdict.reinstated,
                already_paid=verdict.already_paid,
                blocked=verdict.blocked,
            )
        )

    subtotal = money(sum((line.amount_now for line in lines), ZERO))
    recurring = money(sum((line.list_amount for line in lines if not line.blocked), ZERO))

    account = await billing_service.get_account(session, tenant_id)
    breakdown = await tax_calculator.calculate(
        session,
        taxable_base=subtotal,
        on=today,
        country=account.country,
        scope=tax_calculator.scope_for(
            supplier_region=settings.billing_supplier_region,
            customer_region=account.place_of_supply,
        ),
    )

    return Quote(
        lines=lines,
        currency=plan.currency,
        subtotal=subtotal,
        tax_total=breakdown.total,
        total=money(subtotal + breakdown.total),
        recurring_total=recurring,
        period_start=cycle.period_start,
        period_end=cycle.period_end,
    )


__all__ = ["Quote", "QuoteLine", "quote"]
