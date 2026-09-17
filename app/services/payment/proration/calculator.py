"""Proration, over the days a cycle actually has.

Requirement 6 in one sentence: the denominator is `period_end - period_start`,
never 30. A clinic that switches the lab on halfway through February pays for
14 of 28 days; the same clinic doing the same thing in March pays for 16 of 31.
Assuming 30 would overcharge one and undercharge the other, and would do it
silently.

The cycle is half-open, `[period_start, period_end)`. That is what makes the
arithmetic honest:

    charged_days = period_end - activation_date
    amount       = monthly × charged_days / cycle_days

Two consequences fall out of it rather than being special-cased:

*   activating on the first day gives `charged_days == cycle_days`, so the
    clinic pays the full monthly price — requirement 5's first half is what the
    formula already does, and `Proration.is_full` reports it rather than
    branching on it;
*   activating on the last day of the cycle charges one day, not zero. The
    feature is usable that day, so it is billed for that day.

Nothing here reads the database, and nothing here knows what a feature is. It
takes dates and an amount and returns an explanation — which is what lets the
invoice keep the workings (`prorated_days`, `cycle_days`) beside the figure, so
an invoice can say "₹2,499/month, 14 of 28 days" instead of just "₹1,249.50".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.services.payment.money import ZERO, days_between, money


@dataclass(frozen=True)
class Proration:
    """A prorated charge, with the workings kept."""

    amount: Decimal
    #: How many days of the cycle are being charged for.
    charged_days: int
    #: How many days the cycle actually has. 28, 29, 30 or 31.
    cycle_days: int
    #: The full monthly price this was derived from.
    full_amount: Decimal
    #: The window the charge covers, `[start, end)`.
    period_start: date
    period_end: date

    @property
    def is_full(self) -> bool:
        """Whether this is the whole month rather than a part of it.

        True when the add-on was switched on on the first day of the cycle —
        requirement 5's "full monthly charge", arrived at by the same formula
        as every other case rather than by a branch.
        """
        return self.charged_days == self.cycle_days

    @property
    def fraction(self) -> str:
        """`14/28`. For the invoice line's description."""
        return f"{self.charged_days}/{self.cycle_days}"


def cycle_length(period_start: date, period_end: date) -> int:
    """How many days this cycle has. The denominator, and the only one."""
    days = days_between(period_start, period_end)
    if days <= 0:
        raise ValueError(
            f"a billing cycle must end after it starts; got {period_start}..{period_end}"
        )
    return days


def prorate(
    *,
    full_amount: Decimal | float | int,
    period_start: date,
    period_end: date,
    effective_from: date,
) -> Proration:
    """What to charge for a feature switched on partway through a cycle.

    `effective_from` before the cycle starts is treated as the first day: a
    backdated activation is still only one cycle's worth of money, and charging
    for days outside the period being billed would be charging twice.

    `effective_from` on or after `period_end` charges nothing. That is not an
    error — it is what the invoice generator sees for an add-on that was
    switched on for the *next* cycle, and it must produce a zero line rather
    than raise.
    """
    total_days = cycle_length(period_start, period_end)
    monthly = money(full_amount)

    start = max(effective_from, period_start)
    if start >= period_end:
        return Proration(
            amount=ZERO,
            charged_days=0,
            cycle_days=total_days,
            full_amount=monthly,
            period_start=period_end,
            period_end=period_end,
        )

    charged_days = days_between(start, period_end)
    if charged_days >= total_days:
        # The whole cycle. Taking the monthly price directly rather than
        # `monthly × days / days` keeps it exact for any price: a figure that
        # divides badly must not drift by a cent on a full month.
        amount = monthly
    else:
        amount = money(monthly * Decimal(charged_days) / Decimal(total_days))

    return Proration(
        amount=amount,
        charged_days=charged_days,
        cycle_days=total_days,
        full_amount=monthly,
        period_start=start,
        period_end=period_end,
    )


def unused_value(
    *,
    full_amount: Decimal | float | int,
    period_start: date,
    period_end: date,
    effective_to: date,
) -> Proration:
    """What the *unused* remainder of a paid cycle is worth.

    Not used for add-on cancellation — requirement 7 is explicit that there is
    no refund and no credit there, and `addon/billing_service.py` never calls
    this. It exists for a plan change, where the clinic has paid for a month of
    one plan and is moving to another partway through: the credit for what they
    will not use is a line on the invoice, and this is where its size comes
    from.
    """
    total_days = cycle_length(period_start, period_end)
    monthly = money(full_amount)

    end = min(effective_to, period_end)
    if end <= period_start:
        return Proration(
            amount=monthly,
            charged_days=total_days,
            cycle_days=total_days,
            full_amount=monthly,
            period_start=period_start,
            period_end=period_end,
        )

    unused_days = days_between(end, period_end)
    if unused_days <= 0:
        return Proration(
            amount=ZERO,
            charged_days=0,
            cycle_days=total_days,
            full_amount=monthly,
            period_start=period_end,
            period_end=period_end,
        )

    return Proration(
        amount=money(monthly * Decimal(unused_days) / Decimal(total_days)),
        charged_days=unused_days,
        cycle_days=total_days,
        full_amount=monthly,
        period_start=end,
        period_end=period_end,
    )
