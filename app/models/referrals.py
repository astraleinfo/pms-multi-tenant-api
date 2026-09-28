"""Referral credits: a clinic earns credit toward its subscription when a
clinic it refers pays its first bill.

`Referral` names two clinics and belongs wholly to neither, so it carries no
`tenant_id` and no row-level security — platform accounting, like `features`
and `platform_settings`, not a clinic's own row. `ReferralRedemption` is the
referrer's own record of spending that credit against one of its bills, and
is tenant-owned like every other row a clinic can see.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin, uuid_pk
from app.models.enums import ReferralStatus


class Referral(Base, TimestampMixin):
    __tablename__ = "referrals"
    __table_args__ = (
        UniqueConstraint("referred_tenant_id", name="uq_referral_referred_tenant"),
        CheckConstraint("referrer_tenant_id <> referred_tenant_id", name="ck_referral_not_self"),
        CheckConstraint("credit_amount >= 0", name="ck_referral_credit_amount"),
        Index("ix_referrals_referrer_tenant_id", "referrer_tenant_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    referrer_tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    #: unique — a clinic can be referred once, ever
    referred_tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    #: the code as resolved at signup, snapshotted
    code_used: Mapped[str] = mapped_column(String(16), nullable=False)
    #: snapshotted from `platform_settings.referral_credit_amount` at creation —
    #: a later change to that setting never rewrites a referral already earned
    credit_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[ReferralStatus] = mapped_column(
        Enum(ReferralStatus, name="referral_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ReferralStatus.PENDING,
    )
    vested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReferralRedemption(Base, TenantScoped, TimestampMixin):
    """One spend of credit against one bill — the referrer's own record.
    Unique per invoice, so re-running the billing cycle can never redeem the
    same credit twice for the same bill."""

    __tablename__ = "referral_redemptions"
    __extra_table_args__ = (
        UniqueConstraint("subscription_invoice_id", name="uq_referral_redemption_invoice"),
        CheckConstraint("amount > 0", name="ck_referral_redemption_amount"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    subscription_invoice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("subscription_invoices.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
