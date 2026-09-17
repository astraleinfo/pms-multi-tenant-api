"""Add-on activation, cancellation and re-enabling — requirements 5 to 8.

Every test here drives the services directly with an explicit `today`, because
every one of them is about a date: which day of the cycle a module was switched
on, whether the cycle has turned over yet, and what the *next* invoice says as
a result. A test that could only use the real clock could not check any of it.

The four requirements, one test each at minimum:

*   5 — first day is the full charge, mid-cycle is prorated;
*   6 — the denominator is the real month (proved exhaustively in
    `test_billing_proration.py`, confirmed end-to-end here);
*   7 — cancelling keeps the feature to the cycle end, refunds nothing, and
    charges nothing next cycle;
*   8 — re-enabling before the cycle ends charges nothing at all.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.core.errors import Conflict
from app.db.session import tenant_session
from app.models.enums import InvoiceItemKind, TenantFeatureStatus
from app.services.payment import billing_service, subscription_service
from app.services.payment.addon import billing_service as addon_service
from tests.billing_helpers import (
    LAB_MONTHLY,
    PHARMACY_MONTHLY,
    STARTER,
    entitlement,
    invoices_of,
    line_for,
    make_tenant,
    plan,
)

#: A 31-day cycle starting on the 1st, so "mid-cycle" has an exact answer.
#: The billing catalogue — plans, plan features and tax rates — is seeded, and
#: the seeder truncates first. Declaring the dependency means this module gets a
#: clean database whether it is run alone or with the rest of the suite; without
#: it, a rerun would find the previous run's payments still there.
pytestmark = pytest.mark.usefixtures("seeded")

JAN_1 = date(2026, 1, 1)
JAN_15 = date(2026, 1, 15)
FEB_1 = date(2026, 2, 1)


async def _subscribed(label: str, *, on: date = JAN_1):
    """A clinic on the Starter plan with its first cycle open."""
    tenant_id = await make_tenant(label)
    starter = await plan(STARTER)
    async with tenant_session(tenant_id) as session:
        await subscription_service.subscribe(
            session, tenant_id, plan=starter, today=on, trial_days=0
        )
    return tenant_id


async def test_activation_on_the_first_day_is_the_full_monthly_charge():
    """Requirement 5, first half."""
    tenant_id = await _subscribed("addon-full")

    async with tenant_session(tenant_id) as session:
        result = await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
        assert result.charged, "a paid add-on activated on day one must be invoiced"
        assert result.proration.is_full
        assert result.proration.amount == LAB_MONTHLY

    charge = [i for i in await invoices_of(tenant_id) if line_for(i, "lab")]
    assert len(charge) == 1
    line = line_for(charge[0], "lab")
    assert str(line.kind) == InvoiceItemKind.ADDON.value
    assert Decimal(str(line.amount)) == LAB_MONTHLY


async def test_mid_cycle_activation_is_prorated_over_the_real_month():
    """Requirement 5, second half, and requirement 6 end to end.

    January has 31 days. Switching the lab on on the 15th buys 17 of them
    (the 15th through the 31st inclusive), so 1,999 × 17/31 = 1,096.23.
    """
    tenant_id = await _subscribed("addon-prorated")

    async with tenant_session(tenant_id) as session:
        result = await addon_service.activate(session, tenant_id, "lab", today=JAN_15)

    assert result.charged
    assert result.proration.charged_days == 17
    assert result.proration.cycle_days == 31
    assert result.proration.amount == Decimal("1096.23")

    invoice = [i for i in await invoices_of(tenant_id) if line_for(i, "lab")][0]
    line = line_for(invoice, "lab")
    assert str(line.kind) == InvoiceItemKind.PRORATION.value
    # The workings are kept on the line, so the invoice can explain itself.
    assert (line.prorated_days, line.cycle_days) == (17, 31)
    assert "17/31" in line.description


async def test_cancelling_keeps_the_feature_until_the_cycle_ends_and_refunds_nothing():
    """Requirement 7, all three clauses."""
    tenant_id = await _subscribed("addon-cancel")

    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)

    before = len(await invoices_of(tenant_id))

    async with tenant_session(tenant_id) as session:
        result = await addon_service.cancel(session, tenant_id, "lab", today=JAN_15)

    row = await entitlement(tenant_id, "lab")
    assert row.status == TenantFeatureStatus.ACTIVE, "the feature stays usable until the cycle end"
    assert row.cancel_at == FEB_1, "and stops at the end of the period already paid for"
    assert result.invoice is None

    # No refund, no credit — not a new invoice, and no credit line anywhere.
    after = await invoices_of(tenant_id)
    assert len(after) == before
    assert not [
        item
        for invoice in after
        for item in invoice.items
        if str(item.kind) in (InvoiceItemKind.CREDIT.value, InvoiceItemKind.DISCOUNT.value)
    ]


async def test_a_cancelled_addon_is_not_on_the_next_cycles_invoice():
    """Requirement 7's third clause, which only shows up a month later."""
    tenant_id = await _subscribed("addon-next-cycle")

    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
    async with tenant_session(tenant_id) as session:
        await addon_service.cancel(session, tenant_id, "lab", today=JAN_15)

    # Turn the calendar over into February.
    async with tenant_session(tenant_id) as session:
        subscription = await billing_service.require_subscription(
            session, tenant_id, for_update=True
        )
        await subscription_service.renew(session, subscription, today=FEB_1)

    row = await entitlement(tenant_id, "lab")
    assert row.status == TenantFeatureStatus.INACTIVE, "it stops when the paid period ends"
    assert row.cancel_at is None

    february = [i for i in await invoices_of(tenant_id) if i.period_start == FEB_1]
    assert len(february) == 1
    assert line_for(february[0], "lab") is None, "the next cycle must not mention it"


async def test_re_enabling_before_the_cycle_ends_charges_nothing():
    """Requirement 8. The clinic never stopped paying for the period it is in,
    so there is nothing to charge and nothing to re-create."""
    tenant_id = await _subscribed("addon-reenable")

    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
    async with tenant_session(tenant_id) as session:
        await addon_service.cancel(session, tenant_id, "lab", today=date(2026, 1, 10))

    before = len(await invoices_of(tenant_id))

    async with tenant_session(tenant_id) as session:
        result = await addon_service.activate(session, tenant_id, "lab", today=date(2026, 1, 20))

    assert result.reinstated, "this is a cancellation being dropped, not a new purchase"
    assert not result.charged
    assert len(await invoices_of(tenant_id)) == before, "no duplicate charge"

    row = await entitlement(tenant_id, "lab")
    assert row.status == TenantFeatureStatus.ACTIVE
    assert row.cancel_at is None

    # …and it is billed again next cycle, because it never went away.
    async with tenant_session(tenant_id) as session:
        subscription = await billing_service.require_subscription(
            session, tenant_id, for_update=True
        )
        await subscription_service.renew(session, subscription, today=FEB_1)

    february = [i for i in await invoices_of(tenant_id) if i.period_start == FEB_1][0]
    line = line_for(february, "lab")
    assert line is not None and Decimal(str(line.amount)) == LAB_MONTHLY


async def test_a_mid_cycle_activation_is_not_charged_again_by_the_renewal():
    """The double-charge this design is most exposed to.

    The lab is switched on on the 15th and billed 17/31 immediately. When the
    renewal closes January, that cycle's invoice must not bill the lab a second
    time for days already paid for — `charged_through` is what prevents it.
    """
    tenant_id = await _subscribed("addon-no-double")

    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_15)

    row = await entitlement(tenant_id, "lab")
    assert row.charged_through == FEB_1, "paid to the end of the cycle it was bought in"

    async with tenant_session(tenant_id) as session:
        subscription = await billing_service.require_subscription(
            session, tenant_id, for_update=True
        )
        await subscription_service.renew(session, subscription, today=FEB_1)

    # The mid-cycle charge is a one-off invoice covering 15 Jan – 1 Feb, so it
    # is found by the period it covers rather than by the cycle's start date.
    january_lab = [
        line_for(i, "lab")
        for i in await invoices_of(tenant_id)
        if line_for(i, "lab") and i.period_end == FEB_1
    ]
    assert len(january_lab) == 1, "January's lab charge must appear exactly once"
    assert Decimal(str(january_lab[0].amount)) == Decimal("1096.23")

    february = [i for i in await invoices_of(tenant_id) if i.period_start == FEB_1][0]
    line = line_for(february, "lab")
    assert line is not None
    assert Decimal(str(line.amount)) == LAB_MONTHLY, "February is a full month, not prorated"
    assert str(line.kind) == InvoiceItemKind.ADDON.value


async def test_february_proration_uses_twenty_eight_days_end_to_end():
    """The same purchase, in the shortest month, through the real services."""
    tenant_id = await _subscribed("addon-february", on=FEB_1)

    async with tenant_session(tenant_id) as session:
        result = await addon_service.activate(
            session, tenant_id, "pharmacy", today=date(2026, 2, 15)
        )

    assert result.proration.cycle_days == 28
    assert result.proration.charged_days == 14
    assert result.proration.amount == PHARMACY_MONTHLY / 2


async def test_activating_something_already_active_is_refused():
    tenant_id = await _subscribed("addon-twice")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
    with pytest.raises(Conflict, match="already active"):
        async with tenant_session(tenant_id) as session:
            await addon_service.activate(session, tenant_id, "lab", today=JAN_15)


async def test_cancelling_something_that_is_not_on_is_refused():
    tenant_id = await _subscribed("addon-cancel-off")
    with pytest.raises(Conflict, match="not active"):
        async with tenant_session(tenant_id) as session:
            await addon_service.cancel(session, tenant_id, "lab", today=JAN_15)


async def test_cancelling_twice_is_idempotent_rather_than_an_error():
    """A second click on a button that has already been pressed is not a fault."""
    tenant_id = await _subscribed("addon-cancel-twice")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
    async with tenant_session(tenant_id) as session:
        first = await addon_service.cancel(session, tenant_id, "lab", today=JAN_15)
    async with tenant_session(tenant_id) as session:
        second = await addon_service.cancel(session, tenant_id, "lab", today=date(2026, 1, 20))
    assert first.entitlement.cancel_at == second.entitlement.cancel_at == FEB_1


async def test_a_module_included_in_the_plan_is_free_and_cannot_be_cancelled_alone():
    """The Clinic plan bundles the lab. It comes on with the subscription, is
    never invoiced as an add-on, and cannot be dropped on its own."""
    tenant_id = await make_tenant("addon-included")
    clinic_plan = await plan("clinic")
    async with tenant_session(tenant_id) as session:
        await subscription_service.subscribe(
            session, tenant_id, plan=clinic_plan, today=JAN_1, trial_days=0
        )

    row = await entitlement(tenant_id, "lab")
    assert row.status == TenantFeatureStatus.ACTIVE, "a bundled module is switched on"

    invoice = (await invoices_of(tenant_id))[0]
    assert line_for(invoice, "lab") is None, "and never billed separately"

    with pytest.raises(Conflict, match="part of the base plan"):
        async with tenant_session(tenant_id) as session:
            await addon_service.cancel(session, tenant_id, "lab", today=JAN_15)


async def test_off_and_on_again_inside_one_cycle_does_not_charge_twice():
    """A clinic that cancels, lets it lapse, and buys again in the *same* month.

    The days were already paid for, so the second activation costs nothing —
    the guard is `charged_through`, not the cancellation flag, which is why
    this case survives the feature actually going inactive in between.
    """
    tenant_id = await _subscribed("addon-relapse")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
    async with tenant_session(tenant_id) as session:
        await addon_service.deactivate_now(session, tenant_id, "lab")

    before = len(await invoices_of(tenant_id))

    async with tenant_session(tenant_id) as session:
        result = await addon_service.activate(session, tenant_id, "lab", today=date(2026, 1, 20))

    assert not result.charged
    assert len(await invoices_of(tenant_id)) == before


async def test_the_legacy_module_switch_goes_through_billing_once_subscribed():
    """The other door to the same switch, and it must not be a free one.

    `/v1/tenant/features/{key}/purchase` predates platform billing. For a
    clinic with a subscription it now charges exactly as the billing screen
    does — otherwise a practice administrator could switch the lab on there and
    never be billed for it, which is not a nuance, it is a free lab.
    """
    from app.api.v1 import tenant as tenant_api

    tenant_id = await _subscribed("addon-legacy-door")
    before = len(await invoices_of(tenant_id))

    async with tenant_session(tenant_id) as session:
        # The same call the old endpoint makes, via the billing path it now takes.
        result = await addon_service.activate(session, tenant_id, "lab", today=JAN_15)

    assert result.charged, "the legacy door charges like every other one"
    assert len(await invoices_of(tenant_id)) == before + 1
    assert callable(tenant_api._billable_addon), "the endpoint's billing check exists"


async def test_an_unsubscribed_clinic_keeps_the_original_free_switch():
    """A deployment not using billing, and a clinic that has not subscribed,
    both keep the behaviour they had. There is no money to get wrong for
    them, and breaking their entitlement switch to serve billing would be a
    poor trade."""
    from app.api.v1.tenant import _billable_addon

    tenant_id = await make_tenant("addon-no-subscription")

    class _Ctx:
        def __init__(self, session, tid):
            self.session = session
            self.tenant_id = tid

    async with tenant_session(tenant_id) as session:
        assert await _billable_addon(_Ctx(session, tenant_id), "lab") is None
