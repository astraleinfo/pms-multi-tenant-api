"""Proration, over months of every length.

The requirement is that the denominator is the number of days the cycle
actually has. These tests are the proof: the same add-on, at the same monthly
price, activated on the same *relative* day of four differently-sized months,
must come out at four different amounts — and a 29-day February must differ
from a 28-day one.

No database. `prorate` takes dates and a price and returns an explanation, so
the arithmetic can be tested without a clinic existing.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.payment.money import add_months, anchored, days_between, money, next_anchor
from app.services.payment.proration.calculator import prorate, unused_value

MONTHLY = Decimal("2800.00")


def test_cycle_length_is_the_real_month_not_thirty():
    """28, 29, 30, 31 — never an assumption."""
    assert days_between(date(2026, 2, 1), date(2026, 3, 1)) == 28
    assert days_between(date(2024, 2, 1), date(2024, 3, 1)) == 29, "2024 is a leap year"
    assert days_between(date(2026, 4, 1), date(2026, 5, 1)) == 30
    assert days_between(date(2026, 1, 1), date(2026, 2, 1)) == 31


@pytest.mark.parametrize(
    ("start", "end", "expected_days"),
    [
        (date(2026, 2, 1), date(2026, 3, 1), 28),
        (date(2024, 2, 1), date(2024, 3, 1), 29),
        (date(2026, 4, 1), date(2026, 5, 1), 30),
        (date(2026, 1, 1), date(2026, 2, 1), 31),
    ],
)
def test_first_day_of_cycle_is_the_full_monthly_charge(start, end, expected_days):
    """Requirement 5, first half — and it is not a special case.

    Activating on `period_start` gives `charged_days == cycle_days`, so the
    formula produces the whole month by itself. The month's length does not
    change the answer, which is exactly right: a full month costs a month.
    """
    share = prorate(full_amount=MONTHLY, period_start=start, period_end=end, effective_from=start)
    assert share.amount == MONTHLY
    assert share.is_full
    assert share.cycle_days == expected_days
    assert share.charged_days == expected_days


@pytest.mark.parametrize(
    ("start", "end", "on", "charged", "total", "expected"),
    [
        # February, 28 days: joining on the 15th buys 14 of them.
        (date(2026, 2, 1), date(2026, 3, 1), date(2026, 2, 15), 14, 28, "1400.00"),
        # The same 15th in a leap February is 15 of 29 — a different fraction
        # *and* a different denominator.
        (date(2024, 2, 1), date(2024, 3, 1), date(2024, 2, 15), 15, 29, "1448.28"),
        # April, 30 days.
        (date(2026, 4, 1), date(2026, 5, 1), date(2026, 4, 15), 16, 30, "1493.33"),
        # January, 31 days. Same calendar date, four different amounts.
        (date(2026, 1, 1), date(2026, 2, 1), date(2026, 1, 15), 17, 31, "1535.48"),
    ],
)
def test_mid_cycle_is_prorated_over_the_month_that_is_actually_running(
    start, end, on, charged, total, expected
):
    """Requirement 6. Thirty would be wrong in all four rows."""
    share = prorate(full_amount=MONTHLY, period_start=start, period_end=end, effective_from=on)
    assert share.charged_days == charged
    assert share.cycle_days == total
    assert share.amount == Decimal(expected)
    assert not share.is_full
    assert share.fraction == f"{charged}/{total}"


def test_thirty_day_assumption_would_be_visibly_wrong():
    """The test that would fail if anybody reintroduced `/ 30`.

    Mid-February, a 30-day denominator gives 1,306.67 and the real one gives
    1,400.00 — a 7% undercharge that nothing else in the system would catch.
    """
    share = prorate(
        full_amount=MONTHLY,
        period_start=date(2026, 2, 1),
        period_end=date(2026, 3, 1),
        effective_from=date(2026, 2, 15),
    )
    naive = money(MONTHLY * Decimal(14) / Decimal(30))
    assert share.amount == Decimal("1400.00")
    assert share.amount != naive


def test_last_day_of_the_cycle_is_charged_for_one_day():
    """A feature usable for one day is billed for one day, not zero."""
    share = prorate(
        full_amount=MONTHLY,
        period_start=date(2026, 2, 1),
        period_end=date(2026, 3, 1),
        effective_from=date(2026, 2, 28),
    )
    assert share.charged_days == 1
    assert share.amount == Decimal("100.00")


def test_activation_after_the_cycle_ends_charges_nothing():
    """Not an error — it is what the generator sees for a module switched on
    for the *next* cycle, and it must produce a zero rather than raise."""
    share = prorate(
        full_amount=MONTHLY,
        period_start=date(2026, 2, 1),
        period_end=date(2026, 3, 1),
        effective_from=date(2026, 3, 5),
    )
    assert share.amount == Decimal("0.00")
    assert share.charged_days == 0


def test_backdated_activation_is_clamped_to_the_cycle():
    """A date before the cycle buys the cycle, not more than the cycle.

    Charging from an earlier date would bill days that another invoice already
    covered.
    """
    share = prorate(
        full_amount=MONTHLY,
        period_start=date(2026, 2, 1),
        period_end=date(2026, 3, 1),
        effective_from=date(2025, 11, 4),
    )
    assert share.amount == MONTHLY
    assert share.charged_days == 28


def test_a_cycle_that_ends_before_it_starts_is_refused():
    with pytest.raises(ValueError, match="must end after it starts"):
        prorate(
            full_amount=MONTHLY,
            period_start=date(2026, 3, 1),
            period_end=date(2026, 2, 1),
            effective_from=date(2026, 2, 15),
        )


def test_unused_value_is_the_other_side_of_the_same_split():
    """Charged plus unused is the whole month, to the cent.

    This is what makes a mid-cycle plan change honest: the clinic paid for one
    month, uses part of it, and is credited the rest.
    """
    charged = prorate(
        full_amount=MONTHLY,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 2, 1),
        effective_from=date(2026, 1, 15),
    )
    unused = unused_value(
        full_amount=MONTHLY,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 2, 1),
        effective_to=date(2026, 1, 15),
    )
    assert charged.charged_days + (31 - unused.charged_days) == 31
    assert charged.amount + Decimal("1264.52") == MONTHLY
    assert unused.amount == Decimal("1535.48")


def test_a_full_month_is_exact_even_when_the_price_divides_badly():
    """1,000 over 3 days would drift; a full cycle must not.

    The generator takes the monthly price directly when the whole cycle is
    charged, rather than multiplying by days/days — otherwise a price that
    divides badly loses a cent on every full month.
    """
    odd = Decimal("1000.01")
    for start, end in (
        (date(2026, 2, 1), date(2026, 3, 1)),
        (date(2026, 1, 1), date(2026, 2, 1)),
        (date(2024, 2, 1), date(2024, 3, 1)),
    ):
        share = prorate(
            full_amount=odd, period_start=start, period_end=end, effective_from=start
        )
        assert share.amount == odd


# --- the calendar the cycles are built on ------------------------------------


def test_anchor_survives_a_short_month():
    """A clinic that subscribed on the 31st bills on the 31st again in March.

    The anchor is stored rather than re-derived from the last cycle's start,
    which is why February clamping it to the 28th does not permanently move it.
    """
    assert anchored(2026, 2, 31) == date(2026, 2, 28)
    assert anchored(2024, 2, 31) == date(2024, 2, 29)
    assert anchored(2026, 3, 31) == date(2026, 3, 31)
    assert anchored(2026, 4, 31) == date(2026, 4, 30)


def test_next_anchor_is_strictly_after():
    assert next_anchor(date(2026, 1, 31), 31) == date(2026, 2, 28)
    assert next_anchor(date(2026, 2, 28), 31) == date(2026, 3, 31)
    assert next_anchor(date(2026, 1, 15), 15) == date(2026, 2, 15)


def test_add_months_clamps_rather_than_overflowing():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
