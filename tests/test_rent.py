"""The monthly bill, its reminders, the grace period and the hold.

The cycle is driven here with an explicit `today`, so a month of reminders
runs in a second. Every test leaves Northgate as it found it: no bills, no hold.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import delete, select, update

from app.db.session import session_factory, tenant_session
from app.models.enums import SubscriptionInvoiceStatus
from app.models.rent import SubscriptionInvoice
from app.models.tenancy import Tenant
from app.services import mailer, rent
from app.services.platform import Timings, get_timings
from tests.conftest import NORTHGATE, sign_in


@pytest.fixture
async def northgate_id(northgate) -> uuid.UUID:
    return uuid.UUID(northgate.tenant_id)


@pytest.fixture
async def timings() -> Timings:
    async with tenant_session(None) as session:
        return await get_timings(session)


@pytest.fixture(autouse=True)
async def clean_bills():
    """Every test here starts and ends with no bills and no hold anywhere."""
    await _wipe()
    yield
    await _wipe()


async def _wipe() -> None:
    async with tenant_session(None) as session:
        ids = list((await session.execute(select(Tenant.id))).scalars())
        await session.execute(
            update(Tenant).values(billing_hold_since=None, billing_exempt=False)
        )
    for tenant_id in ids:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                delete(SubscriptionInvoice).where(SubscriptionInvoice.tenant_id == tenant_id)
            )


@pytest.fixture
def outbox(monkeypatch) -> list[mailer.Email]:
    sent: list[mailer.Email] = []

    async def capture(email: mailer.Email) -> None:
        sent.append(email)

    monkeypatch.setattr(mailer, "send", capture)
    return sent


async def _cycle(tenant_id: uuid.UUID, today: date) -> dict:
    return await rent.run_cycle(session_factory, today=today, tenant_ids=[tenant_id])


async def _bills(tenant_id: uuid.UUID) -> list[SubscriptionInvoice]:
    async with tenant_session(tenant_id) as session:
        return list(
            (await session.execute(
                select(SubscriptionInvoice).where(SubscriptionInvoice.tenant_id == tenant_id)
            )).scalars()
        )


async def _held(tenant_id: uuid.UUID) -> bool:
    async with tenant_session(None) as session:
        tenant = await session.get(Tenant, tenant_id)
        return tenant.billing_hold_since is not None


def _stages(outbox: list[mailer.Email]) -> list[str]:
    return [e.subject for e in outbox]


# --- raising ------------------------------------------------------------------------------


FEE_LINE = "Astrale PMS subscription"


async def _tenant_id(console, slug: str) -> uuid.UUID:
    tenants = (await console.get("/v1/platform/tenants")).json()
    return uuid.UUID(next(t["id"] for t in tenants if t["slug"] == slug))


@pytest.fixture
async def fee_set_to(console):
    """Change the flat fee from the console for one test, and put it back."""
    before = (await console.get("/v1/platform/settings")).json()["subscription_fee_monthly"]

    async def set_fee(value: float) -> None:
        response = await console.patch("/v1/platform/settings", json={"subscription_fee_monthly": value})
        assert response.status_code == 200, response.text
        assert response.json()["subscription_fee_monthly"] == value

    yield set_fee
    await set_fee(before)


async def test_a_bill_is_one_line_for_the_flat_clinic_fee(northgate_id, outbox, timings):
    today = date(2026, 10, 1)
    fee = float(timings.subscription_fee_monthly)
    assert fee > 0, "the demo seeds a fee"
    await _cycle(northgate_id, today)

    [bill] = await _bills(northgate_id)
    assert bill.lines == [{"name": FEE_LINE, "amount": fee}]
    assert float(bill.amount) == pytest.approx(fee)
    assert bill.period_start == date(2026, 10, 1) and bill.period_end == date(2026, 10, 31)
    assert bill.due_on == today + timedelta(days=timings.rent_due_days)
    # The admins are told a bill exists, and what it is for.
    assert outbox and all("October 2026 bill" in e.subject for e in outbox)
    assert all(FEE_LINE in e.text for e in outbox)


async def test_running_the_cycle_twice_raises_one_bill_and_sends_one_email(northgate_id, outbox):
    today = date(2026, 10, 1)
    await _cycle(northgate_id, today)
    after_first = len(outbox)
    await _cycle(northgate_id, today)

    assert len(await _bills(northgate_id)) == 1
    assert len(outbox) == after_first


async def test_a_clinic_on_trial_is_not_billed(console, rowan, outbox):
    """Rowan is on `trial`: it has every module and pays nothing yet."""
    rowan_id = await _tenant_id(console, "rowan")
    await _cycle(rowan_id, date(2026, 10, 1))
    assert await _bills(rowan_id) == []

    plan = (await rowan.get("/v1/tenant/subscription")).json()["plan"]
    assert plan["billed"] is False
    assert plan["not_billed_reason"] == "trial"
    assert plan["monthly_total"] == 0


async def test_every_active_clinic_is_billed_the_same_fee(console, outbox, timings):
    """Northgate and Clearview are `active`; both are billed the one figure."""
    fee = float(timings.subscription_fee_monthly)
    for slug in ("northgate", "clearview"):
        tenant_id = await _tenant_id(console, slug)
        await _cycle(tenant_id, date(2026, 10, 1))
        [bill] = await _bills(tenant_id)
        assert float(bill.amount) == pytest.approx(fee), slug


async def test_an_exempt_clinic_is_never_billed(console, northgate_id, outbox):
    await console.patch(f"/v1/platform/tenants/{northgate_id}", json={"billing_exempt": True})
    await _cycle(northgate_id, date(2026, 10, 1))
    assert await _bills(northgate_id) == []
    assert outbox == []


async def test_a_zero_fee_bills_nobody(northgate, northgate_id, fee_set_to, outbox):
    await fee_set_to(0)
    await _cycle(northgate_id, date(2026, 10, 1))
    assert await _bills(northgate_id) == []

    plan = (await northgate.get("/v1/tenant/subscription")).json()["plan"]
    assert plan == {
        "price_monthly": 0, "billed": False, "not_billed_reason": "no_fee", "monthly_total": 0,
    }


async def test_a_fee_changed_in_the_console_is_what_the_next_bill_charges(
    northgate, northgate_id, fee_set_to, outbox
):
    await fee_set_to(12345.5)
    plan = (await northgate.get("/v1/tenant/subscription")).json()["plan"]
    assert plan["price_monthly"] == 12345.5 and plan["monthly_total"] == 12345.5

    await _cycle(northgate_id, date(2026, 10, 1))
    [bill] = await _bills(northgate_id)
    assert bill.lines == [{"name": FEE_LINE, "amount": 12345.5}]
    assert float(bill.amount) == pytest.approx(12345.5)


async def test_the_plan_is_the_fee_and_matches_the_next_bill(northgate, northgate_id, outbox, timings):
    fee = float(timings.subscription_fee_monthly)
    plan = (await northgate.get("/v1/tenant/subscription")).json()["plan"]
    assert plan == {
        "price_monthly": fee, "billed": True, "not_billed_reason": None, "monthly_total": fee,
    }

    # the total is exactly what the next bill charges
    await _cycle(northgate_id, date(2026, 10, 1))
    [bill] = await _bills(northgate_id)
    assert float(bill.amount) == pytest.approx(plan["monthly_total"])


async def test_an_exempt_clinic_keeps_every_module_but_pays_nothing(
    northgate, console, northgate_id, outbox, timings
):
    await console.patch(f"/v1/platform/tenants/{northgate_id}", json={"billing_exempt": True})
    plan = (await northgate.get("/v1/tenant/subscription")).json()["plan"]
    assert plan == {
        "price_monthly": float(timings.subscription_fee_monthly),
        "billed": False,
        "not_billed_reason": "exempt",
        "monthly_total": 0,
    }
    assert (await northgate.get("/v1/billing/invoices")).status_code == 200


async def test_a_suspended_clinic_is_not_billed(console, outbox):
    clearview_id = await _tenant_id(console, "clearview")
    await console.patch(f"/v1/platform/tenants/{clearview_id}/status", json={"status": "suspended"})
    try:
        await _cycle(clearview_id, date(2026, 10, 1))
        assert await _bills(clearview_id) == []
        plan = (await console.get(f"/v1/platform/tenants/{clearview_id}/subscription")).json()["plan"]
        assert plan["billed"] is False and plan["not_billed_reason"] == "suspended"
    finally:
        await console.patch(f"/v1/platform/tenants/{clearview_id}/status", json={"status": "active"})


async def test_a_bill_raised_per_module_still_reads_as_name_and_amount(northgate, northgate_id):
    """A bill from before the flat fee carries a `feature_key` on each line in
    its stored JSON. It is ignored: the line reads as `{name, amount}`."""
    async with tenant_session(northgate_id) as session:
        session.add(
            SubscriptionInvoice(
                tenant_id=northgate_id, number="AST-202609-OLD", period_start=date(2026, 9, 1),
                period_end=date(2026, 9, 30), currency="INR", amount=4498,
                lines=[
                    {"feature_key": "pharmacy", "name": "Pharmacy", "amount": 2499},
                    {"feature_key": "lab", "name": "Lab", "amount": 1999},
                ],
                issued_on=date(2026, 9, 1), due_on=date(2026, 9, 8), status=SubscriptionInvoiceStatus.PAID,
                reminder_stage="none",
            )
        )
    body = (await northgate.get("/v1/tenant/subscription")).json()
    [old] = [b for b in body["invoices"] if b["number"] == "AST-202609-OLD"]
    assert old["lines"] == [{"name": "Pharmacy", "amount": 2499}, {"name": "Lab", "amount": 1999}]
    pdf = await northgate.get(f"/v1/tenant/subscription/invoices/{old['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


# --- the ladder ------------------------------------------------------------------------------


async def test_each_rung_is_emailed_once_and_the_hold_lands_after_the_grace_period(
    northgate_id, outbox, timings
):
    issued = date(2026, 10, 1)
    due = issued + timedelta(days=timings.rent_due_days)
    grace = timings.rent_grace_days

    seen: list[str] = []
    for offset in range(0, timings.rent_due_days + grace + 3):
        day = issued + timedelta(days=offset)
        before = len(outbox)
        await _cycle(northgate_id, day)
        [bill] = await _bills(northgate_id)
        if len(outbox) > before:
            seen.append(bill.reminder_stage)
        # Never held while the grace period runs; held from the day after it.
        assert await _held(northgate_id) == (day > due + timedelta(days=grace))

    assert seen == ["issued", "due_soon", "due", "overdue", "final", "blocked"]


async def test_a_missed_week_sends_only_the_rung_the_bill_is_on_now(northgate_id, outbox, timings):
    issued = date(2026, 10, 1)
    await _cycle(northgate_id, issued)
    outbox.clear()

    late = issued + timedelta(days=timings.rent_due_days + 2)
    await _cycle(northgate_id, late)

    assert len({e.to for e in outbox}) == len(outbox)  # one message per admin
    assert all("overdue" in e.subject for e in outbox)


# --- the hold ------------------------------------------------------------------------------------


async def _put_on_hold(tenant_id: uuid.UUID) -> SubscriptionInvoice:
    issued = date(2026, 10, 1)
    await _cycle(tenant_id, issued)
    async with tenant_session(None) as session:
        timings = await get_timings(session)
    await _cycle(
        tenant_id, issued + timedelta(days=timings.rent_due_days + timings.rent_grace_days + 1)
    )
    assert await _held(tenant_id)
    [bill] = await _bills(tenant_id)
    return bill


async def test_a_clinic_on_hold_sees_its_bill_and_nothing_else(client, northgate_id, outbox):
    await _put_on_hold(northgate_id)
    admin = await sign_in(client, "northgate_admin")

    blocked = await admin.get("/v1/patients")
    assert blocked.status_code == 402
    assert blocked.json()["error"]["code"] == "account_on_hold"
    assert (await admin.get("/v1/appointments")).status_code == 402

    # Still open: who am I, which clinic, and what is owed.
    assert (await admin.get("/v1/auth/me")).status_code == 200
    tenant = (await admin.get("/v1/tenant")).json()
    assert tenant["billing_hold"] is True
    subscription = (await admin.get("/v1/tenant/subscription")).json()
    assert subscription["summary"]["on_hold"] is True
    assert subscription["summary"]["outstanding"] > 0
    bill_id = subscription["invoices"][0]["id"]
    pdf = await admin.get(f"/v1/tenant/subscription/invoices/{bill_id}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


async def test_staff_can_still_sign_in_while_on_hold_but_only_admins_see_the_bill(
    client, northgate_id, outbox
):
    await _put_on_hold(northgate_id)
    doctor = await sign_in(client, "northgate_doctor")
    assert (await doctor.get("/v1/tenant")).json()["billing_hold"] is True
    assert (await doctor.get("/v1/tenant/subscription")).status_code == 403
    assert (await doctor.get("/v1/encounters")).status_code == 402


async def test_a_support_session_is_never_held(console, client, northgate_id, outbox):
    await _put_on_hold(northgate_id)
    token = (
        await console.post(f"/v1/platform/tenants/{northgate_id}/impersonate")
    ).json()["access_token"]
    response = await client.get(
        "/v1/patients", headers={"Host": NORTHGATE, "Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200


async def test_settling_the_bill_lifts_the_hold_at_once(client, console, northgate_id, outbox):
    bill = await _put_on_hold(northgate_id)
    outbox.clear()

    settled = await console.post(
        f"/v1/platform/tenants/{northgate_id}/subscription/invoices/{bill.id}/settle",
        json={"reference": "NEFT 88213"},
    )
    assert settled.status_code == 200, settled.text
    body = settled.json()
    assert body["summary"]["on_hold"] is False
    assert body["invoices"][0]["status"] == "paid"
    assert body["invoices"][0]["payment_reference"] == "NEFT 88213"
    assert any("is paid" in e.subject for e in outbox)

    admin = await sign_in(client, "northgate_admin")
    assert (await admin.get("/v1/patients")).status_code == 200

    again = await console.post(
        f"/v1/platform/tenants/{northgate_id}/subscription/invoices/{bill.id}/settle", json={}
    )
    assert again.status_code == 409


async def test_voiding_the_bill_lifts_the_hold_too(console, northgate_id, outbox):
    bill = await _put_on_hold(northgate_id)
    voided = await console.post(
        f"/v1/platform/tenants/{northgate_id}/subscription/invoices/{bill.id}/void"
    )
    assert voided.status_code == 200
    assert voided.json()["summary"]["on_hold"] is False
    assert not await _held(northgate_id)


async def test_exempting_a_held_clinic_lifts_the_hold(console, northgate_id, outbox):
    await _put_on_hold(northgate_id)
    response = await console.patch(
        f"/v1/platform/tenants/{northgate_id}", json={"billing_exempt": True}
    )
    assert response.status_code == 200
    assert response.json()["billing_hold"] is False
    assert not await _held(northgate_id)


# --- who may do what ------------------------------------------------------------------------------


async def test_the_support_desk_can_read_bills_but_not_settle_them(
    console_support, northgate_id, outbox
):
    bill = await _put_on_hold(northgate_id)
    assert (await console_support.get(f"/v1/platform/tenants/{northgate_id}/subscription")).status_code == 200
    overview = await console_support.get("/v1/platform/billing")
    assert overview.status_code == 200
    # the desk cannot open /platform/settings, so the overview carries the fee
    assert overview.json()["fee_monthly"] > 0
    refused = await console_support.post(
        f"/v1/platform/tenants/{northgate_id}/subscription/invoices/{bill.id}/settle", json={}
    )
    assert refused.status_code == 403
    assert (await console_support.post("/v1/platform/billing/run")).status_code == 403


async def test_a_clinic_cannot_reach_another_clinics_bill(clearview, northgate_id, outbox):
    await _cycle(northgate_id, date(2026, 10, 1))
    [bill] = await _bills(northgate_id)
    response = await clearview.get(f"/v1/tenant/subscription/invoices/{bill.id}/pdf")
    assert response.status_code == 404


async def test_the_console_overview_lists_every_clinic(console, northgate_id, outbox, timings):
    await _cycle(northgate_id, date(2026, 10, 1))
    overview = (await console.get("/v1/platform/billing")).json()
    assert overview["grace_days"] == timings.rent_grace_days
    assert overview["fee_monthly"] == float(timings.subscription_fee_monthly)
    row = next(r for r in overview["clinics"] if r["tenant"]["id"] == str(northgate_id))
    assert row["summary"]["open_count"] == 1


async def test_run_now_is_a_super_admins_and_is_idempotent(console, outbox):
    first = (await console.post("/v1/platform/billing/run")).json()
    second = (await console.post("/v1/platform/billing/run")).json()
    assert first["clinics"] >= 3
    assert second["raised"] == []


async def test_the_ladder_rungs_in_order(timings):
    bill = SubscriptionInvoice(due_on=date(2026, 10, 8))
    grace = timings.rent_grace_days
    before = timings.rent_reminder_days_before_due
    assert rent.stage_for(bill, date(2026, 10, 8) - timedelta(days=before + 1), timings).value == "issued"
    assert rent.stage_for(bill, date(2026, 10, 8) - timedelta(days=before), timings).value == "due_soon"
    assert rent.stage_for(bill, date(2026, 10, 8), timings).value == "due"
    assert rent.stage_for(bill, date(2026, 10, 9), timings).value == "overdue"
    assert rent.stage_for(bill, date(2026, 10, 8) + timedelta(days=grace), timings).value == "final"
    assert rent.stage_for(bill, date(2026, 10, 8) + timedelta(days=grace + 1), timings).value == "blocked"
