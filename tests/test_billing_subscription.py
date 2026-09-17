"""The subscription lifecycle, and the invoices it produces.

Creation, activation, pause, resume, cancellation, renewal and plan changes —
plus the two things about renewal that are easy to get wrong and expensive to
get wrong quietly: a cycle must never be invoiced twice, and a clinic that has
been left un-renewed for months must be caught up one real month at a time
rather than billed for one long period.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.core.errors import Conflict
from app.db.session import tenant_session
from app.models.enums import (
    BillingCycleStatus,
    InvoiceItemKind,
    SubscriptionInvoiceStatus,
    SubscriptionStatus,
    TenantFeatureStatus,
)
from app.services.payment import billing_service, subscription_service
from app.services.payment.addon import billing_service as addon_service
from app.services.payment.invoice import generation_service
from tests.billing_helpers import (
    CLINIC,
    LAB_MONTHLY,
    PRACTICE,
    STARTER,
    entitlement,
    invoices_of,
    make_tenant,
    plan,
    subscription_of,
)

#: The billing catalogue — plans, plan features and tax rates — is seeded, and
#: the seeder truncates first. Declaring the dependency means this module gets a
#: clean database whether it is run alone or with the rest of the suite; without
#: it, a rerun would find the previous run's payments still there.
pytestmark = pytest.mark.usefixtures("seeded")

JAN_1 = date(2026, 1, 1)
FEB_1 = date(2026, 2, 1)
MAR_1 = date(2026, 3, 1)


async def _subscribe(label: str, code: str = STARTER, *, on: date = JAN_1, trial: int = 0):
    tenant_id = await make_tenant(label)
    chosen = await plan(code)
    async with tenant_session(tenant_id) as session:
        result = await subscription_service.subscribe(
            session, tenant_id, plan=chosen, today=on, trial_days=trial
        )
        return tenant_id, result.subscription.id, chosen


async def test_subscribing_opens_a_cycle_and_raises_the_first_invoice():
    """Requirement 1: every tenant has a mandatory monthly base plan."""
    tenant_id, _, starter = await _subscribe("sub-create")

    subscription = await subscription_of(tenant_id)
    assert subscription.status == SubscriptionStatus.ACTIVE
    assert subscription.started_on == JAN_1
    assert subscription.anchor_day == 1

    invoices = await invoices_of(tenant_id)
    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice.status == SubscriptionInvoiceStatus.ISSUED
    assert invoice.period_start == JAN_1
    assert invoice.period_end == FEB_1, "the cycle is half-open: February 1st is the next one"
    base = [i for i in invoice.items if str(i.kind) == InvoiceItemKind.BASE_PLAN.value]
    assert len(base) == 1
    assert Decimal(str(base[0].amount)) == Decimal(str(starter.amount))


async def test_a_clinic_cannot_hold_two_subscriptions():
    """The partial unique index, not a check in Python — two browser tabs
    subscribing at once meet the database, not a race."""
    tenant_id, _, _ = await _subscribe("sub-double")
    starter = await plan(STARTER)
    with pytest.raises(Conflict, match="already has a subscription"):
        async with tenant_session(tenant_id) as session:
            await subscription_service.subscribe(
                session, tenant_id, plan=starter, today=JAN_1, trial_days=0
            )


async def test_a_trial_defers_the_first_invoice_without_deferring_the_product():
    tenant_id, _, _ = await _subscribe("sub-trial", trial=14)

    subscription = await subscription_of(tenant_id)
    assert subscription.status == SubscriptionStatus.TRIALING
    assert subscription.trial_ends_on == date(2026, 1, 15)
    assert await invoices_of(tenant_id) == [], "a trial is not invoiced while it runs"

    # The trial ends inside the first cycle, so the renewal that closes that
    # cycle both activates the subscription and bills for it.
    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        await subscription_service.renew(session, row, today=FEB_1)

    subscription = await subscription_of(tenant_id)
    assert subscription.status == SubscriptionStatus.ACTIVE
    assert [i.period_start for i in await invoices_of(tenant_id)] == [JAN_1, FEB_1]


async def test_pause_stops_billing_and_resume_catches_the_calendar_up():
    tenant_id, _, _ = await _subscribe("sub-pause")

    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        await subscription_service.pause(session, row)

    assert (await subscription_of(tenant_id)).status == SubscriptionStatus.PAUSED

    # A renewal while paused does nothing at all.
    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        await subscription_service.renew(session, row, today=FEB_1)
    assert len(await invoices_of(tenant_id)) == 1

    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        result = await subscription_service.resume(session, row, today=MAR_1)
        # The anchor survived the pause: cycles resume on the 1st.
        assert result.cycle.period_start == MAR_1
        assert result.cycle.period_end == date(2026, 4, 1)

    assert (await subscription_of(tenant_id)).status == SubscriptionStatus.ACTIVE


async def test_resuming_something_that_is_not_paused_is_refused():
    tenant_id, _, _ = await _subscribe("sub-resume-active")
    with pytest.raises(Conflict, match="not paused"):
        async with tenant_session(tenant_id) as session:
            row = await billing_service.require_subscription(session, tenant_id, for_update=True)
            await subscription_service.resume(session, row, today=FEB_1)


async def test_cancelling_keeps_the_period_already_paid_for():
    """The same rule as an add-on: money taken buys the whole period it bought."""
    tenant_id, _, _ = await _subscribe("sub-cancel")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)

    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        await subscription_service.cancel(session, row, today=date(2026, 1, 20))

    subscription = await subscription_of(tenant_id)
    assert subscription.status == SubscriptionStatus.ACTIVE, "still live until the cycle ends"
    assert subscription.cancel_at_cycle_end

    before = len(await invoices_of(tenant_id))
    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        await subscription_service.renew(session, row, today=FEB_1)

    subscription = await subscription_of(tenant_id)
    assert subscription.status == SubscriptionStatus.CANCELLED
    assert subscription.cancelled_at == FEB_1
    assert len(await invoices_of(tenant_id)) == before, "no invoice for a cycle never opened"
    assert (await entitlement(tenant_id, "lab")).status == TenantFeatureStatus.INACTIVE


async def test_a_scheduled_cancellation_can_be_taken_back():
    tenant_id, _, _ = await _subscribe("sub-uncancel")
    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        await subscription_service.cancel(session, row, today=date(2026, 1, 20))
    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        await subscription_service.resume_cancellation(session, row)

    assert not (await subscription_of(tenant_id)).cancel_at_cycle_end

    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        await subscription_service.renew(session, row, today=FEB_1)

    assert (await subscription_of(tenant_id)).status == SubscriptionStatus.ACTIVE
    assert [i.period_start for i in await invoices_of(tenant_id)] == [JAN_1, FEB_1]


async def test_renewal_is_idempotent():
    """The property every job in this package depends on.

    Running the renewal five times on the same day must produce one February
    cycle and one February invoice — because of the unique indexes, not
    because anything counted.
    """
    tenant_id, _, _ = await _subscribe("sub-idempotent")

    for _ in range(5):
        async with tenant_session(tenant_id) as session:
            row = await billing_service.require_subscription(session, tenant_id, for_update=True)
            await subscription_service.renew(session, row, today=FEB_1)

    invoices = await invoices_of(tenant_id)
    assert [i.period_start for i in invoices] == [JAN_1, FEB_1]
    assert len({i.number for i in invoices}) == 2, "invoice numbers are never reused"


async def test_a_clinic_left_unrenewed_is_caught_up_one_real_month_at_a_time():
    """Three months behind becomes three invoices of the right lengths — not
    one invoice for ninety days."""
    tenant_id, _, _ = await _subscribe("sub-behind")

    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        # Each renewal advances exactly one cycle, so catching up takes as many
        # calls as there are months — which is what keeps every period real.
        for _ in range(3):
            await subscription_service.renew(session, row, today=date(2026, 4, 10))

    periods = [(i.period_start, i.period_end) for i in await invoices_of(tenant_id)]
    assert periods == [
        (JAN_1, FEB_1),
        (FEB_1, MAR_1),
        (MAR_1, date(2026, 4, 1)),
        (date(2026, 4, 1), date(2026, 5, 1)),
    ]
    # 31, 28, 31, 30 — the real lengths, in order.
    assert [(end - start).days for start, end in periods] == [31, 28, 31, 30]


async def test_changing_plan_credits_the_unused_part_of_the_old_one():
    """A mid-cycle plan change ends the old cycle today, credits what was not
    used, and starts a new cycle on the new plan."""
    tenant_id, _, starter = await _subscribe("sub-plan-change")
    practice = await plan(PRACTICE)

    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        result = await subscription_service.change_plan(
            session, row, plan=practice, today=date(2026, 1, 21)
        )
        assert result.cycle.period_start == date(2026, 1, 21)

    invoices = await invoices_of(tenant_id)
    assert len(invoices) == 2
    new_invoice = invoices[-1]

    credits = [i for i in new_invoice.items if str(i.kind) == InvoiceItemKind.CREDIT.value]
    assert len(credits) == 1, "the unused part of the old cycle comes back as a credit"
    # 11 unused days of January (21st → 1 Feb), of 31, at the Starter price.
    expected = (Decimal(str(starter.amount)) * 11 / 31).quantize(Decimal("0.01"))
    assert Decimal(str(credits[0].amount)) == -expected
    assert Decimal(str(new_invoice.discount_total)) == expected

    base = [i for i in new_invoice.items if str(i.kind) == InvoiceItemKind.BASE_PLAN.value][0]
    assert Decimal(str(base.amount)) == Decimal(str(practice.amount))

    subscription = await subscription_of(tenant_id)
    assert subscription.plan_id == practice.id
    assert subscription.anchor_day == 21, "the new plan bills on the day it started"


async def test_a_plan_change_supersedes_the_old_cycle_rather_than_rewriting_it():
    tenant_id, _, _ = await _subscribe("sub-supersede")
    clinic_plan = await plan(CLINIC)

    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        await subscription_service.change_plan(session, row, plan=clinic_plan, today=date(2026, 1, 21))

        old = await billing_service.latest_cycle(session, row)
        assert old.sequence == 2, "the new cycle is a new row"

    async with tenant_session(tenant_id) as session:
        from sqlalchemy import select

        from app.models.billing import BillingCycle

        cycles = (
            await session.execute(
                select(BillingCycle).order_by(BillingCycle.sequence)
            )
        ).scalars().all()
        assert [c.status for c in cycles] == [
            BillingCycleStatus.SUPERSEDED,
            BillingCycleStatus.OPEN,
        ]
        assert cycles[0].period_end == date(2026, 1, 21), "it records the period actually lived"


async def test_changing_to_the_same_plan_is_refused():
    tenant_id, _, starter = await _subscribe("sub-same-plan")
    with pytest.raises(Conflict, match="already this clinic's plan"):
        async with tenant_session(tenant_id) as session:
            row = await billing_service.require_subscription(session, tenant_id, for_update=True)
            await subscription_service.change_plan(session, row, plan=starter, today=FEB_1)


async def test_voiding_an_invoice_lets_the_cycle_be_invoiced_again():
    """The duplicate-invoice index is partial on `status <> 'void'`, which is
    what makes "void and re-raise" the correction for a bad invoice."""
    tenant_id, _, _ = await _subscribe("sub-void")
    first = (await invoices_of(tenant_id))[0]

    async with tenant_session(tenant_id) as session:
        await generation_service.void_invoice(
            session, tenant_id, first.id, reason="raised in error"
        )

    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        cycle = await billing_service.current_cycle(session, row, on=JAN_1)
        invoice, created = await generation_service.generate_for_cycle(
            session, subscription=row, cycle=cycle, today=JAN_1
        )
        assert created, "a voided cycle can be re-invoiced"
        assert invoice.number != first.number, "with a new number, never a reused one"

    statuses = {i.number: i.status for i in await invoices_of(tenant_id)}
    assert SubscriptionInvoiceStatus.VOID in statuses.values()
    assert SubscriptionInvoiceStatus.ISSUED in statuses.values()


async def test_changing_plan_on_the_first_day_replaces_the_cycle():
    """No days used, so there is nothing to credit — and nothing to supersede.

    A new cycle starting today would collide with the one that started today,
    so the cycle is replaced in place: same period, new plan, and the invoice
    for the old plan is voided and re-raised. Leaving the old cycle superseded
    *and* current is the bug this covers — it would leave the clinic paying for
    a plan it no longer had.
    """
    tenant_id, _, starter = await _subscribe("sub-same-day")
    practice = await plan(PRACTICE)
    first = (await invoices_of(tenant_id))[0]

    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        result = await subscription_service.change_plan(session, row, plan=practice, today=JAN_1)
        assert result.cycle.period_start == JAN_1
        assert result.cycle.period_end == FEB_1
        assert result.cycle.status == BillingCycleStatus.OPEN, "replaced, not superseded"

    invoices = await invoices_of(tenant_id)
    assert len(invoices) == 2
    voided = [i for i in invoices if i.number == first.number][0]
    live = [i for i in invoices if i.status != SubscriptionInvoiceStatus.VOID]
    assert voided.status == SubscriptionInvoiceStatus.VOID
    assert len(live) == 1
    assert Decimal(str(live[0].total)) == Decimal(str(practice.amount))
    # No credit: nothing of the old plan was used.
    assert not [i for i in live[0].items if str(i.kind) == InvoiceItemKind.CREDIT.value]


async def test_changing_plan_on_the_first_day_is_refused_once_it_has_been_paid():
    """Voiding a paid invoice is not on offer, so neither is this.

    Refusing with a clear message is better than the alternatives — silently
    double-billing, or quietly leaving the clinic on a plan it did not choose.
    """
    from app.models.enums import PaymentGateway
    from app.services.payment import payment_service

    tenant_id, _, _ = await _subscribe("sub-same-day-paid")
    invoice = (await invoices_of(tenant_id))[0]

    async with tenant_session(tenant_id) as session:
        row = await payment_service.get_invoice(session, tenant_id, invoice.id)
        await payment_service.record_capture(
            session,
            invoice=row,
            gateway=PaymentGateway.RAZORPAY,
            gateway_payment_id="pay_same_day",
            amount=Decimal(str(invoice.total)),
        )

    practice = await plan(PRACTICE)
    with pytest.raises(Conflict, match="already been paid for"):
        async with tenant_session(tenant_id) as session:
            row = await billing_service.require_subscription(session, tenant_id, for_update=True)
            await subscription_service.change_plan(session, row, plan=practice, today=JAN_1)


async def test_voiding_an_invoice_releases_the_addons_it_charged_for():
    """Otherwise a void-and-re-raise silently drops the add-on lines.

    Voiding does not un-charge anything, but it must un-*record* the charge:
    `charged_through` still saying "paid to the end of the cycle" would make
    the replacement invoice skip the module, and the clinic would keep it for
    free.

    February's invoice is the one used here, because that is the cycle whose
    own invoice carries the add-on — January's was raised before the lab was
    switched on, so the January charge is a one-off invoice of its own.
    """
    tenant_id, _, _ = await _subscribe("sub-void-addons")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)

    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        await subscription_service.renew(session, row, today=FEB_1)

    february = [i for i in await invoices_of(tenant_id) if i.period_start == FEB_1][0]
    assert any(i.feature_key == "lab" for i in february.items)

    async with tenant_session(tenant_id) as session:
        await generation_service.void_invoice(session, tenant_id, february.id)

    row = await entitlement(tenant_id, "lab")
    assert row.charged_through != MAR_1, "the void released the charge"

    async with tenant_session(tenant_id) as session:
        subscription = await billing_service.require_subscription(
            session, tenant_id, for_update=True
        )
        cycle = await billing_service.current_cycle(session, subscription, on=FEB_1)
        _, created = await generation_service.generate_for_cycle(
            session, subscription=subscription, cycle=cycle, today=FEB_1
        )
        assert created

    live = [
        i
        for i in await invoices_of(tenant_id)
        if i.period_start == FEB_1 and i.status != SubscriptionInvoiceStatus.VOID
    ]
    assert len(live) == 1
    lab = [i for i in live[0].items if i.feature_key == "lab"]
    assert lab, "the re-raised invoice still bills the lab"
    assert Decimal(str(lab[0].amount)) == LAB_MONTHLY
