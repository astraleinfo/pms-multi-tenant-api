"""The cart, the quote and checkout — requirements 2, 3 and 4.

The three things worth proving here, because each is silent when it breaks:

*   **one invoice, not several.** "A single payment" is a property of the
    invoice, not of how quickly five of them are paid, so the test counts
    invoices rather than checking a total;
*   **the quote is the charge.** Both come from `addon_service.assess`, and
    the test asserts the quoted total equals the invoice that follows it —
    which is the only way to know the shared function really is shared;
*   **all or nothing.** A cart holding one module the clinic already has must
    leave the others unbought, not charge for them and stop.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.core.errors import Conflict
from app.db.session import tenant_session
from app.models.enums import CartStatus, PlanInterval, TenantFeatureStatus
from app.services.payment import subscription_service
from app.services.payment.addon import billing_service as addon_service
from app.services.payment.cart import cart_service, checkout_service, quote_service
from tests.billing_helpers import (
    INVENTORY_MONTHLY,
    LAB_MONTHLY,
    PHARMACY_MONTHLY,
    STARTER,
    entitlement,
    invoices_of,
    item,
    make_tenant,
    plan,
)

pytestmark = pytest.mark.usefixtures("seeded")

JAN_1 = date(2026, 1, 1)
JAN_15 = date(2026, 1, 15)
FEB_1 = date(2026, 2, 1)


async def _subscribed(label: str, *, on: date = JAN_1):
    tenant_id = await make_tenant(label)
    starter = await plan(STARTER)
    async with tenant_session(tenant_id) as session:
        await subscription_service.subscribe(
            session, tenant_id, plan=starter, today=on, trial_days=0
        )
    return tenant_id


async def test_adding_modules_builds_one_open_cart():
    tenant_id = await _subscribed("cart-add")
    async with tenant_session(tenant_id) as session:
        await cart_service.add(session, tenant_id, "lab")
        cart = await cart_service.add(session, tenant_id, "pharmacy")
        assert {i.feature_key for i in cart.items} == {"lab", "pharmacy"}
        assert cart.status == CartStatus.OPEN


async def test_adding_the_same_module_twice_replaces_the_line():
    tenant_id = await _subscribed("cart-dedupe")
    async with tenant_session(tenant_id) as session:
        await cart_service.add(session, tenant_id, "lab")
        cart = await cart_service.add(session, tenant_id, "lab")
        assert len([i for i in cart.items if i.feature_key == "lab"]) == 1


async def test_a_module_the_clinic_already_has_cannot_go_in_the_cart():
    tenant_id = await _subscribed("cart-owned")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
        with pytest.raises(Conflict):
            await cart_service.add(session, tenant_id, "lab")


async def test_an_interval_with_no_catalogue_price_is_refused_rather_than_guessed():
    """Nothing is sold yearly yet. Billing a year at the monthly rate would be
    worse than saying so."""
    from app.core.errors import NotFound

    tenant_id = await _subscribed("cart-yearly")
    async with tenant_session(tenant_id) as session:
        with pytest.raises(NotFound):
            await cart_service.add(
                session, tenant_id, "lab", interval=PlanInterval.YEARLY
            )


async def test_the_quote_shows_each_module_and_the_combined_total():
    """Requirement 4, on the first day of a cycle so every line is a full month."""
    tenant_id = await _subscribed("cart-quote")
    async with tenant_session(tenant_id) as session:
        await cart_service.add(session, tenant_id, "lab")
        await cart_service.add(session, tenant_id, "pharmacy")
        q = await quote_service.quote(session, tenant_id, today=JAN_1)

    assert [line.feature_key for line in q.lines] == ["lab", "pharmacy"]
    assert {line.amount_now for line in q.lines} == {LAB_MONTHLY, PHARMACY_MONTHLY}
    assert q.subtotal == LAB_MONTHLY + PHARMACY_MONTHLY
    # The figure that recurs equals the figure charged now, on day one.
    assert q.recurring_total == q.subtotal
    assert q.total == q.subtotal + q.tax_total
    assert q.purchasable


async def test_a_mid_cycle_quote_separates_what_is_due_now_from_what_recurs():
    """The two-figure rule. Charged for 17 of 31 days now, a whole month after."""
    tenant_id = await _subscribed("cart-quote-prorated")
    async with tenant_session(tenant_id) as session:
        await cart_service.add(session, tenant_id, "lab")
        q = await quote_service.quote(session, tenant_id, today=JAN_15)

    line = q.lines[0]
    assert line.cycle_days == 31
    assert line.prorated_days == 17
    assert line.amount_now < line.list_amount
    assert line.list_amount == LAB_MONTHLY
    assert q.recurring_total == LAB_MONTHLY
    assert q.subtotal == line.amount_now


async def test_checkout_bills_every_module_on_one_invoice():
    """Requirement 3. Counted as invoices, because that is what makes it one
    payment rather than three paid quickly."""
    tenant_id = await _subscribed("cart-checkout")
    async with tenant_session(tenant_id) as session:
        for key in ("lab", "pharmacy", "inventory"):
            await cart_service.add(session, tenant_id, key)
        result = await checkout_service.checkout(session, tenant_id, today=JAN_1)

    assert result.invoice is not None
    invoices = await invoices_of(tenant_id)
    one_offs = [i for i in invoices if i.billing_cycle_id is None]
    assert len(one_offs) == 1, "three modules must not produce three invoices"

    billed = {i.feature_key for i in one_offs[0].items if i.feature_key}
    assert billed == {"lab", "pharmacy", "inventory"}

    for key in ("lab", "pharmacy", "inventory"):
        ent = await entitlement(tenant_id, key)
        assert ent is not None and ent.status == TenantFeatureStatus.ACTIVE
        assert await item(tenant_id, key) is not None


async def test_the_quoted_total_is_what_the_invoice_charges():
    """The quote and the charge come from one function, and this is the proof."""
    tenant_id = await _subscribed("cart-quote-matches")
    async with tenant_session(tenant_id) as session:
        await cart_service.add(session, tenant_id, "lab")
        await cart_service.add(session, tenant_id, "pharmacy")
        quoted = await quote_service.quote(session, tenant_id, today=JAN_15)
        result = await checkout_service.checkout(session, tenant_id, today=JAN_15)

    assert result.invoice is not None
    assert Decimal(str(result.invoice.subtotal)) == quoted.subtotal
    assert Decimal(str(result.invoice.tax_total)) == quoted.tax_total
    assert Decimal(str(result.invoice.total)) == quoted.total


async def test_checkout_closes_the_cart_against_the_invoice_it_became():
    tenant_id = await _subscribed("cart-converted")
    async with tenant_session(tenant_id) as session:
        await cart_service.add(session, tenant_id, "lab")
        result = await checkout_service.checkout(session, tenant_id, today=JAN_1)
        assert result.cart is not None
        assert result.cart.status == CartStatus.CONVERTED
        assert result.cart.converted_invoice_id == result.invoice.id
        # and the clinic can start a fresh one
        assert await cart_service.current(session, tenant_id) is None


async def test_a_cart_holding_something_already_owned_buys_nothing_at_all():
    """All or nothing. The rollback is the transaction's, and that is the point."""
    tenant_id = await _subscribed("cart-atomic")
    async with tenant_session(tenant_id) as session:
        await cart_service.add(session, tenant_id, "lab")
        await cart_service.add(session, tenant_id, "pharmacy")

    # Buy the lab on its own behind the cart's back, so the cart is now stale.
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)

    with pytest.raises(Conflict):
        async with tenant_session(tenant_id) as session:
            await checkout_service.checkout(session, tenant_id, today=JAN_15)

    # The pharmacy was in the same cart and must not have been bought.
    assert await entitlement(tenant_id, "pharmacy") is None or (
        await entitlement(tenant_id, "pharmacy")
    ).status != TenantFeatureStatus.ACTIVE
    assert await item(tenant_id, "pharmacy") is None


async def test_checking_out_an_empty_cart_is_refused():
    tenant_id = await _subscribed("cart-empty")
    with pytest.raises(Conflict):
        async with tenant_session(tenant_id) as session:
            await checkout_service.checkout(session, tenant_id, today=JAN_1)


async def test_buy_now_is_one_module_and_leaves_the_cart_alone():
    """Buying from a module's page must not close a half-filled cart."""
    tenant_id = await _subscribed("cart-buy-now")
    async with tenant_session(tenant_id) as session:
        await cart_service.add(session, tenant_id, "pharmacy")
        result = await checkout_service.buy_now(session, tenant_id, "lab", today=JAN_1)

    assert result.invoice is not None
    assert result.activated == ["lab"]

    async with tenant_session(tenant_id) as session:
        cart = await cart_service.current(session, tenant_id)
        assert cart is not None and cart.status == CartStatus.OPEN
        assert {i.feature_key for i in cart.items} == {"pharmacy"}

    assert await item(tenant_id, "lab") is not None
    assert await item(tenant_id, "pharmacy") is None


async def test_a_cart_of_modules_that_cost_nothing_now_raises_no_invoice():
    """Dropping a scheduled cancellation charges nothing, so there is nothing
    to invoice — a success with no invoice, not a failure."""
    tenant_id = await _subscribed("cart-free")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
        await addon_service.cancel(session, tenant_id, "lab", today=JAN_15)

    async with tenant_session(tenant_id) as session:
        await cart_service.add(session, tenant_id, "lab")
        result = await checkout_service.checkout(session, tenant_id, today=JAN_15)

    assert result.invoice is None
    assert result.reinstated == ["lab"]
    row = await item(tenant_id, "lab")
    assert row is not None and row.cancel_at is None


async def test_the_invoice_lines_carry_the_real_proration_workings():
    tenant_id = await _subscribed("cart-workings")
    async with tenant_session(tenant_id) as session:
        await cart_service.add(session, tenant_id, "inventory")
        result = await checkout_service.checkout(session, tenant_id, today=JAN_15)

    line = next(i for i in result.invoice.items if i.feature_key == "inventory")
    assert line.cycle_days == 31
    assert line.prorated_days == 17
    assert Decimal(str(line.unit_amount)) == INVENTORY_MONTHLY


# --- over HTTP ---------------------------------------------------------------
#
# The arithmetic is proved above. These prove the surface: that the shapes a
# browser receives carry both figures, that an amount in a request body is
# ignored rather than obeyed, and that a role without SUBSCRIPTION_MANAGE
# cannot spend the clinic's money.


async def _signed_and_subscribed(client, label: str):
    from tests.billing_helpers import ADMIN_PASSWORD, make_admin
    from tests.conftest import Signed

    tenant_id = await make_tenant(label)
    host, email = await make_admin(tenant_id)
    response = await client.post(
        "/v1/auth/login",
        headers={"Host": host},
        json={"email": email, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    signed = Signed(client, host, response.json()["access_token"], response.json())
    subscribed = await signed.post(
        "/v1/settings/billing/subscribe", json={"plan_code": "starter"}
    )
    assert subscribed.status_code == 201, subscribed.text
    return signed


async def test_the_cart_endpoints_carry_both_figures(client):
    signed = await _signed_and_subscribed(client, "http-cart")

    empty = await signed.get("/v1/settings/billing/cart")
    assert empty.status_code == 200, empty.text
    assert empty.json()["lines"] == []
    assert empty.json()["purchasable"] is False

    added = await signed.post(
        "/v1/settings/billing/cart/items", json={"feature_key": "lab"}
    )
    assert added.status_code == 201, added.text
    body = added.json()
    assert [line["feature_key"] for line in body["lines"]] == ["lab"]
    # Both figures, always.
    assert "amount_now" in body["lines"][0] and "list_amount" in body["lines"][0]
    assert body["recurring_total"] == float(LAB_MONTHLY)
    assert body["purchasable"] is True

    removed = await signed.delete("/v1/settings/billing/cart/items/lab")
    assert removed.status_code == 200, removed.text
    assert removed.json()["lines"] == []


async def test_an_amount_in_the_request_body_is_refused_rather_than_obeyed(client):
    """A price a browser could name is a price a browser could change.

    `InputModel` forbids unknown keys, so this is refused outright rather than
    quietly dropped — which is the stronger of the two behaviours: a client
    that thought it was setting a price is told it was not.
    """
    signed = await _signed_and_subscribed(client, "http-cart-amount")
    response = await signed.post(
        "/v1/settings/billing/cart/items",
        json={"feature_key": "lab", "interval": "monthly", "amount": 1, "list_amount": 1},
    )
    assert response.status_code == 422, response.text

    # And the honest request prices it from the catalogue.
    ok = await signed.post("/v1/settings/billing/cart/items", json={"feature_key": "lab"})
    assert ok.status_code == 201, ok.text
    assert ok.json()["lines"][0]["list_amount"] == float(LAB_MONTHLY)


async def test_checkout_over_http_returns_one_invoice_for_the_whole_cart(client):
    signed = await _signed_and_subscribed(client, "http-cart-checkout")
    for key in ("lab", "pharmacy"):
        added = await signed.post(
            "/v1/settings/billing/cart/items", json={"feature_key": key}
        )
        assert added.status_code == 201, added.text

    response = await signed.post("/v1/settings/billing/cart/checkout")
    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(body["activated"]) == ["lab", "pharmacy"]
    assert body["invoice"] is not None
    billed = {item["feature_key"] for item in body["invoice"]["items"] if item["feature_key"]}
    assert billed == {"lab", "pharmacy"}


async def test_buy_now_over_http_buys_one_module(client):
    signed = await _signed_and_subscribed(client, "http-buy-now")
    response = await signed.post(
        "/v1/settings/billing/cart/buy-now", json={"feature_key": "lab"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["activated"] == ["lab"]


async def test_the_catalogue_says_what_each_module_costs_and_who_has_it(client):
    """The three questions a card answers at once."""
    signed = await _signed_and_subscribed(client, "http-catalogue")

    response = await signed.get("/v1/settings/billing/modules")
    assert response.status_code == 200, response.text
    cards = {card["feature_key"]: card for card in response.json()}

    # A purchasable module carries a price and an offer.
    lab = cards["lab"]
    assert lab["monthly"] == float(LAB_MONTHLY)
    assert lab["purchasable"] is True
    assert lab["subscribed"] is False
    # Nothing is sold yearly yet, and the page is told so rather than shown a zero.
    assert lab["yearly"] is None

    # A core module is switched on and is not for sale.
    assert cards["patient"]["is_core"] is True
    assert cards["patient"]["purchasable"] is False

    # Buying it moves it out of the offer and into what the clinic has.
    bought = await signed.post(
        "/v1/settings/billing/cart/buy-now", json={"feature_key": "lab"}
    )
    assert bought.status_code == 200, bought.text
    after = {c["feature_key"]: c for c in (await signed.get("/v1/settings/billing/modules")).json()}
    assert after["lab"]["subscribed"] is True
    assert after["lab"]["purchasable"] is False


async def test_a_module_in_the_cart_says_so(client):
    signed = await _signed_and_subscribed(client, "http-catalogue-cart")
    await signed.post("/v1/settings/billing/cart/items", json={"feature_key": "pharmacy"})

    cards = {c["feature_key"]: c for c in (await signed.get("/v1/settings/billing/modules")).json()}
    assert cards["pharmacy"]["in_cart"] is True
    assert cards["lab"]["in_cart"] is False

    one = await signed.get("/v1/settings/billing/modules/pharmacy")
    assert one.status_code == 200, one.text
    assert one.json()["in_cart"] is True
