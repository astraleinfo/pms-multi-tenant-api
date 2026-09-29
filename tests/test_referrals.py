"""Referral credits: a clinic earns credit toward its own subscription when a
clinic it refers pays its first bill.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, update

from app.db.session import session_factory, tenant_session
from app.models.rent import SubscriptionInvoice
from app.models.referrals import Referral, ReferralRedemption
from app.models.tenancy import Tenant
from app.services import mailer, rent
from tests.conftest import sign_in


@pytest.fixture(autouse=True)
async def _clean_referrals():
    """Every test here starts and ends with no referrals, no redemptions,
    and no referral code minted anywhere — mirrors test_rent.py's own
    clean_bills fixture, and for the same reason: these tests share one
    seeded database across the whole file."""
    await _wipe_referrals()
    yield
    await _wipe_referrals()


async def _wipe_referrals() -> None:
    async with tenant_session(None) as session:
        ids = list((await session.execute(select(Tenant.id))).scalars())
        await session.execute(update(Tenant).values(referral_code=None))
        await session.execute(delete(Referral))
    for tenant_id in ids:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                delete(ReferralRedemption).where(ReferralRedemption.tenant_id == tenant_id)
            )


@pytest.fixture(autouse=True)
async def _clean_bills():
    """The redeeming tests below (see "--- redeeming") raise real bills for
    northgate *itself*, not just for the clinics it refers — several of them
    on the same date (2026-10-01). `_clean_referrals` above only wipes
    referral/redemption state, not `subscription_invoices`, so without this a
    bill raised for northgate in one test survives into the next one for the
    same month: `raise_bill`'s existing-bill branch then returns that stale
    bill unchanged, silently ignoring the new test's own balance. Mirrors
    test_rent.py's own `clean_bills` fixture, for the same reason."""
    await _wipe_bills()
    yield
    await _wipe_bills()


async def _wipe_bills() -> None:
    async with tenant_session(None) as session:
        ids = list((await session.execute(select(Tenant.id))).scalars())
    for tenant_id in ids:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                delete(SubscriptionInvoice).where(SubscriptionInvoice.tenant_id == tenant_id)
            )


async def test_the_referral_tables_exist_and_hold_a_row():
    """Not a real referral — just proves the schema is there and the
    platform-owned table has no tenant_id at all."""
    async with tenant_session(None) as session:
        tenants = list((await session.execute(select(Tenant.id).limit(2))).scalars())
        a, b = tenants[0], tenants[1]
        row = Referral(
            referrer_tenant_id=a,
            referred_tenant_id=b,
            code_used="TESTCODE",
            credit_amount=Decimal("100.00"),
            currency="INR",
        )
        session.add(row)
        await session.flush()
        assert not hasattr(row, "tenant_id")
        await session.delete(row)


# --- minting -----------------------------------------------------------------------


async def test_minting_a_code_is_idempotent(northgate):
    first = await northgate.post("/v1/tenant/referral-code")
    assert first.status_code == 200, first.text
    code = first.json()["code"]
    assert code and len(code.replace("-", "")) == 8

    second = await northgate.post("/v1/tenant/referral-code")
    assert second.status_code == 200
    assert second.json()["code"] == code


async def test_only_an_admin_can_mint_a_code(northgate_doctor):
    refused = await northgate_doctor.post("/v1/tenant/referral-code")
    assert refused.status_code == 403


async def test_a_clinic_with_no_code_reads_a_null_one(clearview):
    body = (await clearview.get("/v1/tenant/referrals")).json()
    assert body["code"] is None
    assert body["balance"] == 0
    assert body["referrals"] == []


async def test_the_program_reflects_the_platform_credit_amount(northgate, console):
    await console.patch("/v1/platform/settings", json={"referral_credit_amount": 250})
    body = (await northgate.get("/v1/tenant/referrals")).json()
    assert body["credit_amount"] == 250.0
    await console.patch("/v1/platform/settings", json={"referral_credit_amount": 100})


# --- applying a code at signup ------------------------------------------------------


async def test_a_new_clinic_can_be_created_with_a_referral_code(console, northgate):
    minted = await northgate.post("/v1/tenant/referral-code")
    code = minted.json()["code"]

    created = await console.post(
        "/v1/platform/tenants",
        json={
            "name": "Riverside Vets", "slug": "riverside-vets-referral-test",
            "city": "Leeds", "clinical_type": "dental",
            "admin_name": "Priya Nair", "admin_email": "priya@riverside.example",
            "referral_code": code,
        },
    )
    assert created.status_code == 201, created.text

    program = (await northgate.get("/v1/tenant/referrals")).json()
    assert len(program["referrals"]) == 1
    assert program["referrals"][0]["status"] == "pending"
    assert program["referrals"][0]["referred_tenant_name"] == "Riverside Vets"
    assert program["referrals"][0]["credit_amount"] == program["credit_amount"]


async def test_a_code_typed_with_different_casing_and_a_hyphen_still_resolves(console, northgate):
    minted = await northgate.post("/v1/tenant/referral-code")
    code = minted.json()["code"]
    messy = f"{code[:4].lower()}-{code[4:].lower()}"

    created = await console.post(
        "/v1/platform/tenants",
        json={
            "name": "Casing Test Clinic", "slug": "casing-test-clinic",
            "city": "Leeds", "clinical_type": "dental",
            "admin_name": "Priya Nair", "admin_email": "priya@casing.example",
            "referral_code": messy,
        },
    )
    assert created.status_code == 201, created.text


async def test_an_unknown_referral_code_is_refused_against_the_field(console):
    refused = await console.post(
        "/v1/platform/tenants",
        json={
            "name": "No Such Referrer", "slug": "no-such-referrer",
            "city": "Leeds", "clinical_type": "dental",
            "admin_name": "Priya Nair", "admin_email": "priya@nosuch.example",
            "referral_code": "NOPE1234",
        },
    )
    assert refused.status_code == 422
    fields = {f["field"] for f in refused.json()["error"]["details"]["fields"]}
    assert "referral_code" in fields


async def test_a_clinic_created_without_a_code_has_no_referral(console):
    created = await console.post(
        "/v1/platform/tenants",
        json={
            "name": "No Referral Clinic", "slug": "no-referral-clinic",
            "city": "Leeds", "clinical_type": "dental",
            "admin_name": "Priya Nair", "admin_email": "priya@noref.example",
        },
    )
    assert created.status_code == 201, created.text


async def test_a_clinic_can_be_referred_only_once(console, northgate, clearview):
    """Enforced by the unique constraint on `referred_tenant_id`, not by
    application logic — there is no route that could even attempt this
    through the API, since a clinic is only ever referred once, at its own
    creation. This proves the constraint itself, directly."""
    from sqlalchemy.exc import IntegrityError

    from app.models.referrals import Referral

    async with tenant_session(None) as session:
        northgate_id = uuid.UUID(northgate.tenant_id)
        clearview_id = uuid.UUID(clearview.tenant_id)
        session.add(Referral(
            referrer_tenant_id=northgate_id, referred_tenant_id=clearview_id,
            code_used="ALREADY1", credit_amount=Decimal("100.00"), currency="INR",
        ))
        await session.flush()
        session.add(Referral(
            referrer_tenant_id=northgate_id, referred_tenant_id=clearview_id,
            code_used="AGAIN0002", credit_amount=Decimal("100.00"), currency="INR",
        ))
        with pytest.raises(IntegrityError):
            await session.flush()


# --- vesting -------------------------------------------------------------------------


@pytest.fixture
def outbox(monkeypatch) -> list[mailer.Email]:
    """Captures mail rather than sending it for real, the same pattern
    `tests/test_rent.py` uses — not shared via conftest, so mirrored here."""
    sent: list[mailer.Email] = []

    async def capture(email: mailer.Email) -> None:
        sent.append(email)

    monkeypatch.setattr(mailer, "send", capture)
    return sent


async def _referred_clinic(console, referrer, *, slug: str) -> uuid.UUID:
    code = (await referrer.post("/v1/tenant/referral-code")).json()["code"]
    created = await console.post(
        "/v1/platform/tenants",
        json={
            "name": f"Vesting Test {slug}", "slug": slug, "city": "Leeds",
            "clinical_type": "dental", "admin_name": "Priya Nair",
            "admin_email": f"priya@{slug}.example", "referral_code": code,
        },
    )
    assert created.status_code == 201, created.text
    return uuid.UUID(created.json()["tenant"]["id"])


async def test_settling_the_referred_clinics_first_bill_vests_the_referral(
    console, northgate, outbox
):
    from app.services import rent as rent_service

    referred_id = await _referred_clinic(console, northgate, slug="vest-test-one")
    # Give the referred clinic something billable, then raise and settle its bill.
    await console.put(f"/v1/platform/tenants/{referred_id}/features/billing", json={"status": "active"})
    today = date(2026, 10, 1)
    await rent_service.run_cycle(session_factory, today=today, tenant_ids=[referred_id])
    async with tenant_session(referred_id) as session:
        bill = (
            await session.execute(select(SubscriptionInvoice).where(
                SubscriptionInvoice.tenant_id == referred_id
            ))
        ).scalar_one()

    settled = await console.post(
        f"/v1/platform/tenants/{referred_id}/subscription/invoices/{bill.id}/settle",
        json={"reference": "TEST-REF-1"},
    )
    assert settled.status_code == 200, settled.text

    program = (await northgate.get("/v1/tenant/referrals")).json()
    [referral] = [r for r in program["referrals"] if r["referred_tenant_name"].startswith("Vesting Test")]
    assert referral["status"] == "vested"
    assert referral["vested_at"] is not None
    assert program["balance"] == referral["credit_amount"]


async def test_vesting_a_second_bill_does_not_double_credit(console, northgate):
    from app.services import rent as rent_service

    referred_id = await _referred_clinic(console, northgate, slug="vest-test-two")
    await console.put(f"/v1/platform/tenants/{referred_id}/features/billing", json={"status": "active"})

    for month in (date(2026, 10, 1), date(2026, 11, 1)):
        await rent_service.run_cycle(session_factory, today=month, tenant_ids=[referred_id])
        async with tenant_session(referred_id) as session:
            bill = (
                await session.execute(
                    select(SubscriptionInvoice).where(
                        SubscriptionInvoice.tenant_id == referred_id,
                        SubscriptionInvoice.period_start == month,
                    )
                )
            ).scalar_one()
        await console.post(
            f"/v1/platform/tenants/{referred_id}/subscription/invoices/{bill.id}/settle",
            json={"reference": f"TEST-{month.isoformat()}"},
        )

    program = (await northgate.get("/v1/tenant/referrals")).json()
    # exactly one credit_amount's worth, not two
    assert program["balance"] == program["credit_amount"]


# --- redeeming -----------------------------------------------------------------------


@pytest.fixture
async def northgate_id(northgate) -> uuid.UUID:
    """Mirrors test_rent.py's own fixture — not shared via conftest, so
    mirrored here, the same pattern this file already uses for `outbox`."""
    return uuid.UUID(northgate.tenant_id)


async def _bills(tenant_id: uuid.UUID) -> list[SubscriptionInvoice]:
    """Mirrors test_rent.py's own helper, for the same reason as `northgate_id` above."""
    async with tenant_session(tenant_id) as session:
        return list(
            (
                await session.execute(
                    select(SubscriptionInvoice).where(SubscriptionInvoice.tenant_id == tenant_id)
                )
            ).scalars()
        )


async def _vested_referrer_with_balance(console, referrer, *, slug: str) -> None:
    """Runs a referral all the way to vested, leaving `referrer` with a
    spendable balance. Reuses `_referred_clinic` from the vesting tests."""
    from app.services import rent as rent_service

    referred_id = await _referred_clinic(console, referrer, slug=slug)
    await console.put(f"/v1/platform/tenants/{referred_id}/features/billing", json={"status": "active"})
    today = date(2026, 10, 1)
    await rent_service.run_cycle(session_factory, today=today, tenant_ids=[referred_id])
    async with tenant_session(referred_id) as session:
        bill = (
            await session.execute(select(SubscriptionInvoice).where(
                SubscriptionInvoice.tenant_id == referred_id
            ))
        ).scalar_one()
    settled = await console.post(
        f"/v1/platform/tenants/{referred_id}/subscription/invoices/{bill.id}/settle",
        json={"reference": "TEST-VEST"},
    )
    assert settled.status_code == 200, settled.text


async def test_a_vested_credit_comes_off_the_referrers_next_bill(console, northgate, northgate_id):
    await console.patch("/v1/platform/settings", json={"referral_credit_amount": 50})
    await _vested_referrer_with_balance(console, northgate, slug="redeem-test-small")

    await console.put(f"/v1/platform/tenants/{northgate_id}/features/billing", json={"status": "active"})
    today = date(2026, 10, 1)
    await rent.run_cycle(session_factory, today=today, tenant_ids=[northgate_id])

    [bill] = await _bills(northgate_id)
    credit_line = next(line for line in bill.lines if line["feature_key"] == "referral_credit")
    assert credit_line["amount"] == -50.0

    program = (await northgate.get("/v1/tenant/referrals")).json()
    assert program["balance"] == 0.0
    await console.patch("/v1/platform/settings", json={"referral_credit_amount": 100})


async def test_a_credit_larger_than_the_bill_clamps_at_zero_and_carries_forward(
    console, northgate, northgate_id
):
    await console.patch("/v1/platform/settings", json={"referral_credit_amount": 999999})
    await _vested_referrer_with_balance(console, northgate, slug="redeem-test-large")

    await console.put(f"/v1/platform/tenants/{northgate_id}/features/billing", json={"status": "active"})
    today = date(2026, 10, 1)
    await rent.run_cycle(session_factory, today=today, tenant_ids=[northgate_id])

    [bill] = await _bills(northgate_id)
    assert float(bill.amount) == 0.0
    credit_line = next(line for line in bill.lines if line["feature_key"] == "referral_credit")
    gross = sum(line["amount"] for line in bill.lines if line["feature_key"] != "referral_credit")
    assert credit_line["amount"] == pytest.approx(-gross)

    program = (await northgate.get("/v1/tenant/referrals")).json()
    assert program["balance"] > 0  # the remainder carried forward
    await console.patch("/v1/platform/settings", json={"referral_credit_amount": 100})


async def test_running_the_cycle_twice_does_not_redeem_the_credit_twice(
    console, northgate, northgate_id
):
    await _vested_referrer_with_balance(console, northgate, slug="redeem-test-idempotent")
    await console.put(f"/v1/platform/tenants/{northgate_id}/features/billing", json={"status": "active"})
    today = date(2026, 10, 1)

    await rent.run_cycle(session_factory, today=today, tenant_ids=[northgate_id])
    program_after_first = (await northgate.get("/v1/tenant/referrals")).json()

    await rent.run_cycle(session_factory, today=today, tenant_ids=[northgate_id])
    program_after_second = (await northgate.get("/v1/tenant/referrals")).json()

    assert program_after_first["balance"] == program_after_second["balance"]
    assert len(await _bills(northgate_id)) == 1


async def test_the_next_bill_preview_agrees_with_the_bill_that_gets_raised(
    console, northgate, northgate_id
):
    await _vested_referrer_with_balance(console, northgate, slug="redeem-test-preview")
    await console.put(f"/v1/platform/tenants/{northgate_id}/features/billing", json={"status": "active"})

    preview = (await northgate.get("/v1/tenant/subscription")).json()["plan"]
    await rent.run_cycle(session_factory, today=date(2026, 10, 1), tenant_ids=[northgate_id])
    [bill] = await _bills(northgate_id)

    assert preview["amount_due"] == pytest.approx(float(bill.amount))


async def test_one_clinics_redemption_never_touches_another_clinics_balance(
    console, northgate, clearview, northgate_id
):
    """`ReferralRedemption` is RLS-protected; `balance_for` also filters by
    tenant explicitly. This exercises both at once: clearview must read a
    zero balance whether or not it is asked before or after northgate earns
    and spends a credit."""
    before = (await clearview.get("/v1/tenant/referrals")).json()["balance"]
    assert before == 0

    await _vested_referrer_with_balance(console, northgate, slug="isolation-test")
    await console.put(f"/v1/platform/tenants/{northgate_id}/features/billing", json={"status": "active"})
    await rent.run_cycle(session_factory, today=date(2026, 10, 1), tenant_ids=[northgate_id])

    after = (await clearview.get("/v1/tenant/referrals")).json()["balance"]
    assert after == 0
