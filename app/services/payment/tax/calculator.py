"""Tax, as configuration.

Requirement 11: no GST anywhere in the code. This module knows that tax is a
list of percentages applied to a taxable base, and nothing else — not what the
components are called, not how many there are, not which jurisdiction splits
one rate into two. All of that is rows in `tax_rates`.

An Indian deployment writes two rows, CGST 9% and SGST 9%, both `intra_state`,
plus one IGST 18% row that is `inter_state`; a clinic in the state the platform
bills from gets two tax lines, a clinic outside it gets one, and neither case is
written down here. A UK deployment writes one VAT row. A deployment that does
not charge tax writes none and every invoice has a zero tax total.

Two decisions worth stating:

*   **Tax is computed on the net subtotal**, after discounts and credits, which
    is what every jurisdiction this product bills in expects. The base is
    floored at zero: a credit larger than the charges produces no tax rather
    than negative tax.
*   **Each component is rounded once, on its own**, and the total is the sum of
    the rounded components — not the rounding of the sum. That is what makes
    the tax lines on the invoice add up to the tax total printed on it, which
    is the property an auditor actually checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import TaxRate
from app.models.enums import TaxScope
from app.services.payment.money import ZERO, money, percent_of


@dataclass(frozen=True)
class TaxComponent:
    """One tax line. `percent` is recorded on the invoice beside the amount, so
    a rate change later cannot restate an invoice already issued."""

    code: str
    name: str
    percent: Decimal
    amount: Decimal


@dataclass(frozen=True)
class TaxBreakdown:
    base: Decimal
    components: tuple[TaxComponent, ...]

    @property
    def total(self) -> Decimal:
        """The sum of the rounded components — never the rounding of a sum."""
        return money(sum((c.amount for c in self.components), ZERO))


def scope_for(*, supplier_region: str, customer_region: str) -> TaxScope:
    """Which side of a same-region split this supply falls on.

    A deployment whose tax rows are all `any` never reaches a case where this
    matters — the lookup returns those rows whatever this says. It is here for
    the deployments that do split, and a blank region on either side is treated
    as intra-state, which is the conservative answer when nobody has said.
    """
    supplier = (supplier_region or "").strip().casefold()
    customer = (customer_region or "").strip().casefold()
    if not supplier or not customer:
        return TaxScope.INTRA_STATE
    return TaxScope.INTRA_STATE if supplier == customer else TaxScope.INTER_STATE


async def applicable_rates(
    session: AsyncSession,
    *,
    on: date,
    country: str = "",
    scope: TaxScope = TaxScope.ANY,
) -> list[TaxRate]:
    """The tax components in force on a day, for a place and a supply type.

    A row matches when it is active, its window contains the day, its country
    is blank ("wherever this deployment bills") or matches, and its scope is
    `any` or matches. Ordered by code so an invoice's tax lines come out in the
    same order every time.
    """
    country = (country or "").strip().upper()
    stmt = (
        select(TaxRate)
        .where(
            TaxRate.active.is_(True),
            TaxRate.effective_from <= on,
            or_(TaxRate.effective_to.is_(None), TaxRate.effective_to > on),
            or_(TaxRate.country == "", TaxRate.country == country),
            or_(TaxRate.scope == TaxScope.ANY, TaxRate.scope == scope),
        )
        .order_by(TaxRate.code, TaxRate.effective_from.desc())
    )
    rows = list((await session.execute(stmt)).scalars())

    # One row per code: where a rate was superseded mid-window, the most
    # recently effective one wins. Ordering above put it first.
    seen: set[str] = set()
    current: list[TaxRate] = []
    for row in rows:
        if row.code in seen:
            continue
        seen.add(row.code)
        current.append(row)
    return current


async def calculate(
    session: AsyncSession,
    *,
    taxable_base: Decimal,
    on: date,
    country: str = "",
    scope: TaxScope = TaxScope.ANY,
) -> TaxBreakdown:
    """The tax on a net subtotal, as a list of components.

    A negative base — more credit than charges — is treated as zero. Refunding
    tax is a refund, not a negative tax line on a new invoice.
    """
    base = money(taxable_base)
    if base <= ZERO:
        return TaxBreakdown(base=ZERO, components=())

    rates = await applicable_rates(session, on=on, country=country, scope=scope)
    components = tuple(
        TaxComponent(
            code=rate.code,
            name=rate.name,
            percent=Decimal(str(rate.percent)),
            amount=percent_of(base, rate.percent),
        )
        for rate in rates
        if Decimal(str(rate.percent)) > 0
    )
    return TaxBreakdown(base=base, components=components)
