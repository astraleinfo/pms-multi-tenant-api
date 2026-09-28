"""Referral credits.

A clinic mints its own code from Settings, on demand. Another clinic joins
quoting it; once that clinic pays its first bill, the referrer's credit
vests and comes off its own next one — see `app/services/rent.py` for the
vesting (`settle`) and spending (`raise_bill`) halves of this.

The balance is derived, never stored: `Σ vested credit − Σ redemptions`.
Nothing here holds a running total, so nothing can drift out of step with
the rows that justify it.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.enums import ReferralStatus
from app.models.referrals import Referral, ReferralRedemption
from app.models.tenancy import Tenant

CENTS = Decimal("0.01")

#: no 0/O/1/I/L — a code that survives being read aloud down a phone
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_LENGTH = 8
_MINT_ATTEMPTS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _generate_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))


def normalize_code(raw: str) -> str:
    """However it was typed or read aloud — with a hyphen, without one, in
    lowercase, with stray spaces — this is the one form it is looked up by."""
    return raw.strip().upper().replace("-", "").replace(" ", "")


def format_code(code: str) -> str:
    """`XXXX-XXXX`, for display. `code` is already 8 characters — minted that
    way and never anything else."""
    return f"{code[:4]}-{code[4:]}"


async def mint_code(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    """The clinic's own code — the same one every time this is called again.

    Advisory pre-check then the unique constraint settles a real race, the
    same philosophy `slug_problem` uses for subdomains: at this alphabet and
    length a collision is astronomically unlikely, so this is a courtesy
    retry, not a load-bearing lock.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("No such clinic.")
    if tenant.referral_code:
        return tenant

    for _ in range(_MINT_ATTEMPTS):
        candidate = _generate_code()
        clash = (
            await session.execute(select(Tenant.id).where(Tenant.referral_code == candidate))
        ).scalar_one_or_none()
        if clash is None:
            tenant.referral_code = candidate
            await session.flush()
            return tenant
    raise Conflict("Could not mint a referral code. Try again.")


async def resolve_code(session: AsyncSession, raw_code: str) -> Tenant | None:
    code = normalize_code(raw_code)
    if not code:
        return None
    return (
        await session.execute(select(Tenant).where(Tenant.referral_code == code))
    ).scalar_one_or_none()


async def create_referral(
    session: AsyncSession,
    *,
    referrer_tenant_id: uuid.UUID,
    referred_tenant_id: uuid.UUID,
    code_used: str,
    credit_amount: Decimal,
    currency: str,
) -> Referral:
    row = Referral(
        referrer_tenant_id=referrer_tenant_id,
        referred_tenant_id=referred_tenant_id,
        code_used=code_used,
        credit_amount=credit_amount.quantize(CENTS),
        currency=currency,
        status=ReferralStatus.PENDING,
    )
    session.add(row)
    await session.flush()
    return row


async def vest(session: AsyncSession, referred_tenant_id: uuid.UUID) -> Referral | None:
    """Mark this clinic's referral earned — the first time one of its bills
    is paid. Idempotent: a later paid bill finds this already vested and
    does nothing, so `rent.settle()` can call it on every settlement without
    checking whether this is the clinic's *first* one."""
    row = (
        await session.execute(
            select(Referral).where(
                Referral.referred_tenant_id == referred_tenant_id,
                Referral.status == ReferralStatus.PENDING,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    row.status = ReferralStatus.VESTED
    row.vested_at = _now()
    await session.flush()
    return row


async def balance_for(session: AsyncSession, referrer_tenant_id: uuid.UUID) -> Decimal:
    earned = (
        await session.execute(
            select(func.coalesce(func.sum(Referral.credit_amount), 0)).where(
                Referral.referrer_tenant_id == referrer_tenant_id,
                Referral.status == ReferralStatus.VESTED,
            )
        )
    ).scalar_one()
    spent = (
        await session.execute(
            select(func.coalesce(func.sum(ReferralRedemption.amount), 0)).where(
                ReferralRedemption.tenant_id == referrer_tenant_id
            )
        )
    ).scalar_one()
    return (Decimal(earned) - Decimal(spent)).quantize(CENTS)


def apply_credit(total: Decimal, balance: Decimal) -> tuple[Decimal, Decimal]:
    """What is actually owed, and how much credit that spends.

    Clamped so a bill never goes negative (`CHECK (amount >= 0)` on
    `subscription_invoices`) and never spends more credit than the bill
    needed — the rest stays in the balance for next month.
    """
    redeemed = min(balance, total).quantize(CENTS)
    return (total - redeemed).quantize(CENTS), redeemed


async def redeem(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    subscription_invoice_id: uuid.UUID,
    amount: Decimal,
) -> ReferralRedemption:
    row = ReferralRedemption(
        tenant_id=tenant_id, subscription_invoice_id=subscription_invoice_id, amount=amount
    )
    session.add(row)
    await session.flush()
    return row


async def reverse_redemption(session: AsyncSession, subscription_invoice_id: uuid.UUID) -> None:
    """Undo a redemption when the bill it paid against is voided. The credit
    simply becomes spendable again — nothing here decides where it goes next."""
    row = (
        await session.execute(
            select(ReferralRedemption).where(
                ReferralRedemption.subscription_invoice_id == subscription_invoice_id
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
        await session.flush()


async def list_for(session: AsyncSession, referrer_tenant_id: uuid.UUID) -> list[Referral]:
    return list(
        (
            await session.execute(
                select(Referral)
                .where(Referral.referrer_tenant_id == referrer_tenant_id)
                .order_by(Referral.created_at.desc())
            )
        ).scalars()
    )
