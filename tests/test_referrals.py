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
