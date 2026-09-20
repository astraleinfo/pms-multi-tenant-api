"""`subscription_items` — the billing half of a module, and the promise that it
never disagrees with the entitlement half.

Everything in `test_billing_addons.py` asserts on `tenant_features`, and all of
it still passes: this file is about the row that was added beside it. The two
must always agree, so the last test here is a sweep that re-checks the
invariant after every flow the others exercise individually.

Three of these are about mistakes that would not show up for months:

*   **grandfathering** — a catalogue price rise must not reach a clinic already
    on the module. That is the entire reason the price is pinned on the item
    rather than looked up, and it is silent when it breaks;
*   **the comp asymmetry** — the console switching a module on is a grant and
    creates no item, while switching one off stops the billing. A comp that
    quietly started charging would be found by a customer, not by us;
*   **re-buying** — a module bought, dropped and bought again is two rows, not
    one edited twice, so the price it was on the first time stays legible.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.db.session import tenant_session
from app.models.enums import SubscriptionItemStatus, TenantFeatureStatus
from app.services import tenants as tenant_service
from app.services.payment import billing_service, subscription_service
from app.services.payment.addon import billing_service as addon_service
from tests.billing_helpers import (
    CLINIC,
    LAB_MONTHLY,
    STARTER,
    entitlement,
    item,
    items_of,
    make_tenant,
    plan,
)

pytestmark = pytest.mark.usefixtures("seeded")

JAN_1 = date(2026, 1, 1)
JAN_15 = date(2026, 1, 15)
FEB_1 = date(2026, 2, 1)


async def _subscribed(label: str, *, code: str = STARTER, on: date = JAN_1):
    tenant_id = await make_tenant(label)
    chosen = await plan(code)
    async with tenant_session(tenant_id) as session:
        await subscription_service.subscribe(
            session, tenant_id, plan=chosen, today=on, trial_days=0
        )
    return tenant_id


async def test_buying_a_module_opens_an_item_at_the_price_it_was_bought_at():
    tenant_id = await _subscribed("item-open")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)

    row = await item(tenant_id, "lab")
    assert row is not None
    assert row.status == SubscriptionItemStatus.ACTIVE
    assert row.unit_amount == LAB_MONTHLY
    assert row.quantity == 1
    assert row.started_on == JAN_1
    # Bought from the catalogue, so the provenance is recorded.
    assert row.price_id is not None
    # And it knows the cycle it is living in.
    assert row.current_period_start == JAN_1
    assert row.current_period_end == FEB_1


async def test_a_module_the_plan_includes_is_free_and_gets_no_item():
    """The Clinic plan bundles the lab. Switched on, and not a line on a bill."""
    tenant_id = await _subscribed("item-included", code=CLINIC)
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)

    row = await entitlement(tenant_id, "lab")
    assert row is not None and row.status == TenantFeatureStatus.ACTIVE
    assert await item(tenant_id, "lab") is None


async def test_cancelling_marks_the_item_pending_cancel_and_leaves_access_alone():
    """The state people misread: still on, still paid for, and stopping."""
    tenant_id = await _subscribed("item-cancel")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
        await addon_service.cancel(session, tenant_id, "lab", today=JAN_15)

    row = await item(tenant_id, "lab")
    assert row is not None
    assert row.status == SubscriptionItemStatus.PENDING_CANCEL
    assert row.cancel_at == FEB_1
    # The entitlement is untouched: the clinic keeps the module to the cycle end.
    ent = await entitlement(tenant_id, "lab")
    assert ent is not None and ent.status == TenantFeatureStatus.ACTIVE


async def test_re_enabling_before_the_cycle_ends_returns_the_same_item_to_active():
    tenant_id = await _subscribed("item-reinstate")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
        await addon_service.cancel(session, tenant_id, "lab", today=date(2026, 1, 10))
        await addon_service.activate(session, tenant_id, "lab", today=date(2026, 1, 20))

    row = await item(tenant_id, "lab")
    assert row is not None
    assert row.status == SubscriptionItemStatus.ACTIVE
    assert row.cancel_at is None
    # One row, not two: the module never actually stopped.
    assert len(await items_of(tenant_id)) == 1


async def test_the_renewal_that_ends_a_module_cancels_its_item():
    tenant_id = await _subscribed("item-renewal-end")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
        await addon_service.cancel(session, tenant_id, "lab", today=JAN_15)
        subscription = await billing_service.require_subscription(session, tenant_id)
        await subscription_service.renew(session, subscription, today=FEB_1)

    assert await item(tenant_id, "lab") is None
    every = await items_of(tenant_id)
    assert len(every) == 1
    assert every[0].status == SubscriptionItemStatus.CANCELLED
    assert every[0].cancelled_at is not None


async def test_a_renewal_moves_a_surviving_item_onto_the_new_cycle():
    tenant_id = await _subscribed("item-renewal-carry")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
        subscription = await billing_service.require_subscription(session, tenant_id)
        await subscription_service.renew(session, subscription, today=FEB_1)

    row = await item(tenant_id, "lab")
    assert row is not None
    assert row.current_period_start == FEB_1
    assert row.current_period_end == date(2026, 3, 1)


async def test_a_catalogue_price_rise_does_not_reprice_a_clinic_already_on_it():
    """The whole reason the price lives on the item.

    The figure is pinned when the module is bought. Later syncs — and cancelling
    is one — must leave it exactly where it was, or every clinic silently moves
    onto the new price the next time anything touches the row.
    """
    tenant_id = await _subscribed("item-grandfather")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)

    from sqlalchemy import update

    from app.db.session import session_factory
    from app.models.tenancy import Feature

    async def set_catalogue_price(amount: Decimal) -> None:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(Feature).where(Feature.key == "lab").values(price_monthly=amount)
                )

    # `features` is platform-wide and the seeded catalogue is shared by every
    # test in the run, so the rise is put back whatever happens below.
    await set_catalogue_price(Decimal("9999.00"))
    try:
        # Anything that syncs the item. Cancelling is the cheapest.
        async with tenant_session(tenant_id) as session:
            await addon_service.cancel(session, tenant_id, "lab", today=JAN_15)

        row = await item(tenant_id, "lab")
        assert row is not None
        assert row.unit_amount == LAB_MONTHLY, "a catalogue rise reached an existing subscription"
    finally:
        await set_catalogue_price(LAB_MONTHLY)


async def test_the_console_switching_a_module_off_stops_billing_for_it():
    tenant_id = await _subscribed("item-console-off")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
    assert await item(tenant_id, "lab") is not None

    async with tenant_session(tenant_id) as session:
        await tenant_service.set_feature(session, tenant_id, "lab", active=False, today=JAN_15)

    assert await item(tenant_id, "lab") is None


async def test_the_console_switching_a_module_on_is_a_grant_and_bills_nothing():
    """A comp is an entitlement with no item. Inventing one would bill a clinic
    for something nobody sold them."""
    tenant_id = await _subscribed("item-console-on")
    async with tenant_session(tenant_id) as session:
        await tenant_service.set_feature(session, tenant_id, "lab", active=True, today=JAN_1)

    ent = await entitlement(tenant_id, "lab")
    assert ent is not None and ent.status == TenantFeatureStatus.ACTIVE
    assert await item(tenant_id, "lab") is None


async def test_buying_again_after_a_full_cancellation_is_a_second_row():
    tenant_id = await _subscribed("item-rebuy")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
        await addon_service.deactivate_now(session, tenant_id, "lab")
        await addon_service.activate(session, tenant_id, "lab", today=date(2026, 1, 20))

    every = await items_of(tenant_id)
    assert len(every) == 2
    assert {r.status for r in every} == {
        SubscriptionItemStatus.CANCELLED,
        SubscriptionItemStatus.ACTIVE,
    }


async def test_cancelling_the_subscription_ends_every_item():
    tenant_id = await _subscribed("item-sub-cancel")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
        await addon_service.activate(session, tenant_id, "pharmacy", today=JAN_1)
        subscription = await billing_service.require_subscription(session, tenant_id)
        await subscription_service.cancel(
            session, subscription, today=JAN_15, immediately=True
        )

    assert await item(tenant_id, "lab") is None
    assert await item(tenant_id, "pharmacy") is None


async def test_the_two_halves_never_disagree():
    """The invariant, swept after a clinic has been put through everything.

    For every purchasable module: switched on and billable implies exactly one
    live item, and a live item implies the module is switched on. This is the
    check a reconciliation job would run.
    """
    tenant_id = await _subscribed("item-invariant")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
        await addon_service.activate(session, tenant_id, "pharmacy", today=JAN_1)
        await addon_service.activate(session, tenant_id, "inventory", today=JAN_15)
        await addon_service.cancel(session, tenant_id, "pharmacy", today=JAN_15)
        await addon_service.deactivate_now(session, tenant_id, "inventory")
        subscription = await billing_service.require_subscription(session, tenant_id)
        await subscription_service.renew(session, subscription, today=FEB_1)

    async with tenant_session(tenant_id) as session:
        subscription = await billing_service.require_subscription(session, tenant_id)
        prices = await billing_service.addon_prices(session, subscription.plan_id)

    for key, price in sorted(prices.items()):
        ent = await entitlement(tenant_id, key)
        live = await item(tenant_id, key)
        on = ent is not None and ent.status in (
            TenantFeatureStatus.ACTIVE,
            TenantFeatureStatus.TRIAL,
        )
        if price.billable and on:
            assert live is not None, f"{key} is on and billable but has no item"
            assert live.unit_amount == price.amount
        else:
            assert live is None, f"{key} is not billable or not on, yet has a live item"

        if live is not None:
            assert on, f"{key} has a live item but is not switched on"
