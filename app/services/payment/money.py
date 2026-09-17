"""Money and calendars, in one place.

Two rules the whole billing domain keeps, and they are here so that keeping
them is not a matter of anyone remembering:

*   **Every amount is a `Decimal`, rounded half-up to two places, exactly
    once** — at the point it becomes a line on an invoice. Rounding the same
    figure twice is how a total stops matching the sum of its lines.
*   **A month is however many days it has.** `add_months` moves by calendar
    month and clamps to the end of a short one; `days_between` counts real
    days. Nothing in this package multiplies or divides by 30.

`app/services/billing.py` has its own `_money` for the patient ledger. They do
the same arithmetic and are deliberately not shared: that one is a clinic's
money, this one is ours, and a change to how one rounds should never silently
restate the other.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

#: Two decimal places. Every currency this product bills in has hundredths.
CENTS = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value: Decimal | float | int | str | None) -> Decimal:
    """A monetary amount, rounded half-up to the cent.

    `str(value)` rather than `Decimal(value)` on purpose: `Decimal(0.1)` is
    0.1000000000000000055511151231257827, and an invoice built out of those
    does not add up.
    """
    if value is None:
        return ZERO
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


def percent_of(amount: Decimal, percent: Decimal | float | int) -> Decimal:
    """`amount × percent%`, rounded once."""
    return money(Decimal(str(amount)) * Decimal(str(percent)) / Decimal(100))


def to_minor_units(amount: Decimal, currency: str = "INR") -> int:
    """Paise, cents, pence — what a gateway's API actually wants.

    Only adapters call this. The billing engine deals in `Decimal` throughout,
    so that a currency without hundredths would be a change in one function
    rather than a hunt through the arithmetic.
    """
    return int((money(amount) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def from_minor_units(minor: int | str, currency: str = "INR") -> Decimal:
    return money(Decimal(str(minor)) / Decimal(100))


def now() -> datetime:
    return datetime.now(timezone.utc)


# --- calendars ---------------------------------------------------------------


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def add_months(value: date, months: int) -> date:
    """Move by whole calendar months, clamping to the end of a short one.

    The 31st plus one month is the 30th in April and the 28th in February. The
    clamp is why a subscription carries `anchor_day` separately: February moves
    the *date*, and the anchor is what moves it back to the 31st in March.
    """
    total = value.month - 1 + months
    year = value.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(value.day, days_in_month(year, month)))


def anchored(year: int, month: int, anchor_day: int) -> date:
    """The `anchor_day`-th of this month, or its last day if it is shorter.

    A clinic that subscribed on the 31st bills on the 31st of every month that
    has one, the 30th of the ones that do not, and the 28th or 29th of
    February — and comes back to the 31st afterwards, because the anchor is
    stored rather than re-derived from the last cycle's start.
    """
    return date(year, month, min(anchor_day, days_in_month(year, month)))


def next_anchor(after: date, anchor_day: int) -> date:
    """The first anchored date strictly after `after`."""
    candidate = anchored(after.year, after.month, anchor_day)
    if candidate > after:
        return candidate
    following = add_months(date(after.year, after.month, 1), 1)
    return anchored(following.year, following.month, anchor_day)


def days_between(start: date, end: date) -> int:
    """The real number of days in the half-open window `[start, end)`.

    28 for a February cycle, 31 for a January one, 29 in a leap year. This is
    the only place the length of a billing period comes from.
    """
    return (end - start).days
