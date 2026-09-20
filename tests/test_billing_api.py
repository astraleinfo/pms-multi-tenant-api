"""Settings → Billing over HTTP: what a browser sees, and who may see it.

The service tests elsewhere prove the arithmetic. These prove the surface: that
the billing screen's data arrives in one response, that the actions a clinic
administrator takes work end to end, that no other role can take them, that no
credential leaks into a response body, and that an amount in a request body is
refused rather than obeyed.

Every test here signs into a clinic of its own, created by `billing_helpers`.
The seeded demo clinics are deliberately left alone: subscribing Rowan to a
plan that bundles the lab would change what Rowan is entitled to, and half a
dozen tests in three other files assert on exactly that.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.billing_helpers import ADMIN_PASSWORD, make_admin, make_tenant
from tests.conftest import Signed, audit_lines, sign_in

pytestmark = pytest.mark.usefixtures("seeded")

#: The plan these tests subscribe to. `clinic` carries no trial, so the first
#: invoice is raised immediately and there is something to pay, download and
#: look at — and it bundles the lab, which is how the "included" half of the
#: add-on display gets exercised.
TEST_PLAN = "clinic"


async def signed_clinic(client: AsyncClient, label: str) -> Signed:
    """A clinic of this test's own, with an administrator signed in."""
    tenant_id = await make_tenant(label)
    host, email = await make_admin(tenant_id)
    response = await client.post(
        "/v1/auth/login",
        headers={"Host": host},
        json={"email": email, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return Signed(client, host, response.json()["access_token"], response.json())


async def _summary(signed: Signed) -> dict:
    response = await signed.get("/v1/settings/billing")
    assert response.status_code == 200, response.text
    return response.json()


async def _subscribe(signed: Signed, plan_code: str = TEST_PLAN) -> dict:
    response = await signed.post(
        "/v1/settings/billing/subscribe", json={"plan_code": plan_code}
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def clinic(client) -> Signed:
    """A signed-in clinic administrator with no subscription yet."""
    return await signed_clinic(client, "api")


@pytest.fixture
async def subscribed(client) -> Signed:
    """…and one that has already taken the Clinic plan."""
    signed = await signed_clinic(client, "api-sub")
    await _subscribe(signed)
    return signed


async def test_a_clinic_with_no_subscription_still_gets_a_billing_screen(clinic):
    """The screen has to render before there is anything on it — otherwise
    there is nowhere to subscribe from."""
    summary = await _summary(clinic)
    assert summary["subscription"] is None
    assert summary["next_billing_on"] is None
    assert summary["outstanding_total"] == 0.0
    assert summary["addons"] == []
    assert summary["account"]["currency"]


async def test_the_billing_screen_answers_in_one_request(subscribed):
    """Everything the requirement lists: plan, add-ons, prices, next billing
    date, scheduled cancellations, and what is outstanding."""
    summary = await _summary(subscribed)

    assert summary["subscription"]["plan"]["code"] == TEST_PLAN
    assert summary["subscription"]["status"] == "active"
    assert summary["subscription"]["current_period_days"] in (28, 29, 30, 31)
    assert summary["next_billing_on"]
    assert summary["currency"]

    addons = {a["feature_key"]: a for a in summary["addons"]}
    # A core module is included and therefore has no price to show.
    assert addons["patient"]["included"] is True
    assert addons["patient"]["monthly_amount"] is None
    # So is one the plan bundles — the Clinic plan covers the lab.
    assert addons["lab"]["included"] is True
    assert addons["lab"]["monthly_amount"] is None
    # A purchasable one carries its price, from the catalogue.
    assert addons["pharmacy"]["included"] is False
    assert addons["pharmacy"]["monthly_amount"] == 2499.0
    assert addons["pharmacy"]["cancels_on"] is None

    assert summary["outstanding_total"] > 0, "the first invoice is outstanding"
    assert summary["outstanding_count"] == 1


async def test_the_plan_catalogue_is_offered(clinic):
    response = await clinic.get("/v1/settings/billing/plans")
    assert response.status_code == 200
    codes = {p["code"] for p in response.json()}
    assert {"starter", "practice", "clinic"} <= codes
    assert all(p["amount"] > 0 for p in response.json())


async def test_subscribing_raises_an_invoice_and_is_audited(clinic):
    summary = await _subscribe(clinic)
    assert summary["subscription"]["status"] == "active"

    invoices = (await clinic.get("/v1/settings/billing/invoices")).json()["items"]
    assert len(invoices) == 1, "the first cycle is invoiced immediately"
    first = invoices[0]
    assert first["status"] == "issued"
    assert first["total"] == pytest.approx(
        first["subtotal"] - first["discount_total"] + first["tax_total"]
    )
    assert first["items"], "the line items are returned with the invoice"
    assert first["balance"] == first["total"]

    trail = await audit_lines(clinic.tenant_id, action="billing.subscribe")
    assert trail, "subscribing leaves an audit line"


async def test_a_plan_with_a_trial_defers_the_first_invoice(clinic):
    """Still a subscription — simply not invoiced until the trial ends."""
    summary = await _subscribe(clinic, "starter")
    assert summary["subscription"]["status"] == "trialing"
    assert summary["subscription"]["trial_ends_on"]
    assert (await clinic.get("/v1/settings/billing/invoices")).json()["items"] == []


async def test_a_request_body_cannot_name_its_own_price(clinic):
    """`InputModel` forbids unknown keys, which is a security property here:
    a client cannot smuggle an amount into a call that decides money."""
    response = await clinic.post(
        "/v1/settings/billing/subscribe", json={"plan_code": "starter", "amount": 1}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_an_unknown_plan_is_not_found(clinic):
    response = await clinic.post(
        "/v1/settings/billing/subscribe", json={"plan_code": "platinum-deluxe"}
    )
    assert response.status_code == 404


async def test_changing_plan_raises_an_invoice_for_the_new_one(subscribed):
    response = await subscribed.post(
        "/v1/settings/billing/plan", json={"plan_code": "practice"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["subscription"]["plan"]["code"] == "practice"

    invoices = (await subscribed.get("/v1/settings/billing/invoices")).json()["items"]
    assert len(invoices) == 2
    # The change happens on the same day as the subscription, so nothing of the
    # old cycle was used and there is no credit. What matters here is that the
    # new cycle is invoiced for the new plan; the credit arithmetic has its own
    # test, where the dates can be chosen.
    newest = invoices[0]
    assert any(i["kind"] == "base_plan" for i in newest["items"])


async def test_activating_an_addon_over_http_reports_what_it_charged(subscribed):
    response = await subscribed.post("/v1/settings/billing/addons/pharmacy")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["active"] is True
    assert body["feature_key"] == "pharmacy"
    assert body["charged"] is True
    assert body["reinstated"] is False
    # The proration workings — over how many days, and how much.
    assert body["cycle_days"] in (28, 29, 30, 31)
    assert 0 < body["prorated_days"] <= body["cycle_days"]
    assert body["invoice"]["items"], "the charge is invoiced immediately"
    assert body["amount"] == body["invoice"]["subtotal"]

    trail = await audit_lines(subscribed.tenant_id, action="billing.addon_activate")
    assert trail and trail[0]["meta"]["charged"] is True


async def test_activating_a_module_the_plan_bundles_costs_nothing(subscribed):
    """The Clinic plan covers the lab, so switching it on is free and raises
    no invoice."""
    response = await subscribed.post("/v1/settings/billing/addons/lab")
    assert response.status_code == 200, response.text
    assert response.json()["charged"] is False
    assert response.json()["invoice"] is None


async def test_cancelling_an_addon_schedules_it_and_re_enabling_charges_nothing(subscribed):
    """Requirements 7 and 8, as a browser experiences them."""
    activated = await subscribed.post("/v1/settings/billing/addons/pharmacy")
    assert activated.status_code == 200, activated.text

    cancelled = await subscribed.delete("/v1/settings/billing/addons/pharmacy")
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["active"] is True, "it keeps working until the cycle ends"
    assert body["cancels_on"], "and the date it stops is shown"
    assert body["charged"] is False, "nothing is refunded and nothing is charged"
    scheduled_for = body["cancels_on"]

    # The screen shows the scheduled cancellation, which is what the
    # requirement asks the UI to display.
    summary = await _summary(subscribed)
    row = [a for a in summary["addons"] if a["feature_key"] == "pharmacy"][0]
    assert row["cancels_on"] == scheduled_for
    assert row["active"] is True

    # Changing your mind costs nothing.
    before = len((await subscribed.get("/v1/settings/billing/invoices")).json()["items"])
    reinstated = await subscribed.post("/v1/settings/billing/addons/pharmacy")
    assert reinstated.status_code == 200, reinstated.text
    assert reinstated.json()["reinstated"] is True
    assert reinstated.json()["charged"] is False
    assert reinstated.json()["cancels_on"] is None
    after = len((await subscribed.get("/v1/settings/billing/invoices")).json()["items"])
    assert after == before, "no duplicate charge"


async def test_cancelling_a_module_that_is_not_on_is_refused(subscribed):
    response = await subscribed.delete("/v1/settings/billing/addons/inventory")
    assert response.status_code == 409


async def test_cancelling_and_uncancelling_a_subscription(subscribed):
    cancelled = await subscribed.post(
        "/v1/settings/billing/cancel", json={"reason": "moving on"}
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["subscription"]["cancel_at_cycle_end"] is True
    assert cancelled.json()["next_billing_on"] is None, "nothing is billed after this"

    restored = await subscribed.post("/v1/settings/billing/resume-cancellation")
    assert restored.status_code == 200, restored.text
    assert restored.json()["subscription"]["cancel_at_cycle_end"] is False
    assert restored.json()["next_billing_on"]

    trail = await audit_lines(subscribed.tenant_id, action="billing.cancel")
    assert trail and trail[0]["meta"]["reason"] == "moving on"


async def test_pause_and_resume_over_http(subscribed):
    paused = await subscribed.post("/v1/settings/billing/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["subscription"]["status"] == "paused"
    assert paused.json()["next_billing_on"] is None

    resumed = await subscribed.post("/v1/settings/billing/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["subscription"]["status"] == "active"
    assert resumed.json()["next_billing_on"]


async def test_the_billing_account_can_be_edited_by_an_administrator(clinic):
    response = await clinic.patch(
        "/v1/settings/billing/account",
        json={
            "legal_name": "Testbury Dental Ltd",
            "billing_email": "accounts@testbury.test",
            "country": "in",
            "place_of_supply": "Karnataka",
            "tax_id": "29ABCDE1234F1Z5",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["legal_name"] == "Testbury Dental Ltd"
    assert body["country"] == "IN", "normalised on the way in"
    assert body["place_of_supply"] == "Karnataka"

    # …and it is what the screen reads back.
    assert (await clinic.get("/v1/settings/billing/account")).json() == body


async def test_an_invoice_can_be_downloaded_as_a_pdf(subscribed):
    invoices = (await subscribed.get("/v1/settings/billing/invoices")).json()["items"]
    assert invoices

    response = await subscribed.get(
        f"/v1/settings/billing/invoices/{invoices[0]['id']}/download"
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert invoices[0]["number"] in response.headers["content-disposition"]


async def test_the_invoice_list_can_be_filtered_and_paged(subscribed):
    page = await subscribed.get(
        "/v1/settings/billing/invoices", params={"status": "issued", "limit": 5}
    )
    assert page.status_code == 200
    body = page.json()
    assert body["limit"] == 5
    assert all(i["status"] == "issued" for i in body["items"])
    assert body["total"] >= len(body["items"])

    empty = await subscribed.get("/v1/settings/billing/invoices", params={"status": "void"})
    assert empty.json()["items"] == []


async def test_an_invoice_belonging_to_another_clinic_is_not_found(client, subscribed):
    """Cross-tenant access is a 404, never a 403 — a 403 would confirm the row
    exists."""
    other = await signed_clinic(client, "api-other")
    theirs = (await subscribed.get("/v1/settings/billing/invoices")).json()["items"]
    assert theirs

    response = await other.get(f"/v1/settings/billing/invoices/{theirs[0]['id']}")
    assert response.status_code == 404


async def test_a_malformed_invoice_id_is_not_found_rather_than_a_crash(subscribed):
    response = await subscribed.get("/v1/settings/billing/invoices/not-a-uuid")
    assert response.status_code == 404


async def test_checkout_is_refused_when_no_gateway_is_configured(subscribed):
    """The test environment has no Razorpay credentials, and the honest answer
    is a conflict rather than a half-opened payment."""
    invoices = (await subscribed.get("/v1/settings/billing/invoices")).json()["items"]
    unpaid = [i for i in invoices if i["balance"] > 0]
    assert unpaid

    response = await subscribed.post(
        f"/v1/settings/billing/invoices/{unpaid[0]['id']}/checkout"
    )
    assert response.status_code == 409
    assert "not configured" in response.json()["error"]["message"]


async def test_no_response_body_carries_a_gateway_secret(subscribed):
    """A blunt check, and worth having: the key secret and webhook secret must
    not appear anywhere a browser can read."""
    from app.core.config import settings

    for path in (
        "/v1/settings/billing",
        "/v1/settings/billing/plans",
        "/v1/settings/billing/invoices",
        "/v1/settings/billing/payments",
        "/v1/settings/billing/account",
    ):
        response = await subscribed.get(path)
        assert response.status_code == 200, path
        for secret in (settings.razorpay_key_secret, settings.razorpay_webhook_secret):
            if secret:
                assert secret not in response.text, path


# --- authorisation -----------------------------------------------------------


async def test_a_clinical_role_cannot_see_or_change_the_subscription(client):
    """`SUBSCRIPTION_*` belongs to the tenant administrator alone.

    A doctor can settle a patient's invoice all day — a different permission,
    a different ledger — and still cannot see what the practice pays for the
    software.
    """
    doctor = await sign_in(client, "northgate_doctor")

    assert (await doctor.get("/v1/settings/billing")).status_code == 403
    assert (await doctor.get("/v1/settings/billing/invoices")).status_code == 403
    assert (
        await doctor.post("/v1/settings/billing/subscribe", json={"plan_code": "starter"})
    ).status_code == 403
    assert (await doctor.post("/v1/settings/billing/addons/lab")).status_code == 403
    assert (await doctor.delete("/v1/settings/billing/addons/lab")).status_code == 403

    # The cart spends the practice's money, so it is behind the same permission
    # — reading it included, because what a clinic is shopping for is as much
    # the administrator's business as what it already pays for.
    assert (await doctor.get("/v1/settings/billing/cart")).status_code == 403
    assert (
        await doctor.post("/v1/settings/billing/cart/items", json={"feature_key": "lab"})
    ).status_code == 403
    assert (await doctor.delete("/v1/settings/billing/cart/items/lab")).status_code == 403
    assert (await doctor.delete("/v1/settings/billing/cart")).status_code == 403
    assert (await doctor.post("/v1/settings/billing/cart/checkout")).status_code == 403
    assert (
        await doctor.post("/v1/settings/billing/cart/buy-now", json={"feature_key": "lab"})
    ).status_code == 403


async def test_billing_is_refused_without_a_session(client):
    from tests.conftest import NORTHGATE

    response = await client.get("/v1/settings/billing", headers={"Host": NORTHGATE})
    assert response.status_code == 401


async def test_the_console_cannot_use_a_clinic_billing_route(console):
    """A platform token is not a clinic token, even for the platform's own
    money."""
    assert (await console.get("/v1/settings/billing")).status_code == 401


async def test_a_clinic_cannot_reach_the_console_billing_routes(subscribed):
    assert (await subscribed.get("/v1/platform/billing/metrics")).status_code == 401
    assert (await subscribed.get("/v1/platform/billing/webhooks")).status_code == 401


async def test_the_console_can_read_one_clinics_invoices_and_webhook_log(console, subscribed):
    invoices = await console.get(
        f"/v1/platform/billing/tenants/{subscribed.tenant_id}/invoices"
    )
    assert invoices.status_code == 200, invoices.text
    assert invoices.json(), "the clinic has invoices"

    webhooks = await console.get("/v1/platform/billing/webhooks")
    assert webhooks.status_code == 200
    # The payload is never returned — gateway JSON is not console table data.
    for row in webhooks.json():
        assert "payload" not in row


async def test_the_console_refuses_an_unknown_clinic(console):
    response = await console.get(f"/v1/platform/billing/tenants/{uuid.uuid4()}/invoices")
    assert response.status_code == 404


async def test_the_console_can_void_an_invoice_and_the_clinic_sees_it(console, subscribed):
    """Voiding is the only way to unmake an invoice — the figures are frozen by
    a database trigger, so a mistake is corrected rather than edited."""
    invoices = (await subscribed.get("/v1/settings/billing/invoices")).json()["items"]
    target = invoices[0]

    response = await console.post(
        f"/v1/platform/billing/tenants/{subscribed.tenant_id}"
        f"/invoices/{target['id']}/void"
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "void"

    seen = (await subscribed.get(f"/v1/settings/billing/invoices/{target['id']}")).json()
    assert seen["status"] == "void"
    assert seen["balance"] == 0.0

    trail = await audit_lines(subscribed.tenant_id, action="billing.invoice_void")
    assert trail
