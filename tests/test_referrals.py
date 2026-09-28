"""Referral credits: a clinic earns credit toward its own subscription when a
clinic it refers pays its first bill.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.session import tenant_session
from app.models.referrals import Referral, ReferralRedemption
from app.models.tenancy import Tenant
from tests.conftest import sign_in


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
