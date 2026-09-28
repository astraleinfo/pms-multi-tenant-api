"""What a clinic owes the platform: the monthly "app rent".

One bill per clinic per month, for the paid modules it had switched on when
the bill was raised, at the catalogue prices of that day. The lines are copied
onto the bill rather than joined at read time, so a price a super admin
changes next month does not rewrite what a clinic was billed this month.

Tenant-owned like everything else a clinic can see: the clinic reads its own
bills under its own policy, and the console reads them one clinic at a time
with that clinic bound, exactly as it reads everything else.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin, uuid_pk
from app.models.enums import RentReminder, SubscriptionInvoiceStatus


class SubscriptionInvoice(Base, TenantScoped, TimestampMixin):
    __tablename__ = "subscription_invoices"
    __extra_table_args__ = (
        UniqueConstraint("tenant_id", "period_start", name="uq_subscription_invoice_period"),
        Index("ix_subscription_invoices_status_due", "status", "due_on"),
        CheckConstraint("amount >= 0", name="ck_subscription_invoice_amount"),
        CheckConstraint("due_on >= issued_on", name="ck_subscription_invoice_due"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    number: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    #: the first day of the month billed for
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    #: [{"feature_key", "name", "amount"}] — copied, never joined
    lines: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    issued_on: Mapped[date] = mapped_column(Date, nullable=False)
    due_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[SubscriptionInvoiceStatus] = mapped_column(
        Enum(
            SubscriptionInvoiceStatus,
            name="subscription_invoice_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=SubscriptionInvoiceStatus.OPEN,
    )
    #: the furthest reminder emailed so far — each rung goes once
    reminder_stage: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RentReminder.NONE.value
    )
    reminders_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: the bank reference, cheque number or gateway id the platform recorded
    payment_reference: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    settled_by: Mapped[str] = mapped_column(String(160), nullable=False, default="")
