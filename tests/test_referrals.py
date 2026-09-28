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
