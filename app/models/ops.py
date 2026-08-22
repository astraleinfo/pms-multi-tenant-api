"""Inventory and billing.

Stock on hand is never a stored counter: it is the sum of the ledger for that
item, in that tenant (§3.11). Invoice totals are likewise derived from the line
items and payments, so a status can never disagree with the money.
"""

from __future__ import annotations

import uuid
from datetime import date as date_t
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantScoped, TimestampMixin, uuid_pk
from app.models.clinical import Patient  # noqa: F401 — resolves the relationship below
from app.models.enums import (
    InventoryCategory,
    InventoryRefType,
    InventoryTxType,
    InvoiceStatus,
    LineItemSource,
    PaymentMethod,
)


def _enum(cls, name: str):
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class InventoryItem(Base, TenantScoped, TimestampMixin):
    __tablename__ = "inventory_items"
    __extra_table_args__ = (UniqueConstraint("tenant_id", "sku", name="uq_inventory_tenant_sku"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    sku: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    form: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    strength: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    unit: Mapped[str] = mapped_column(String(40), nullable=False, default="unit")
    category: Mapped[InventoryCategory] = mapped_column(
        _enum(InventoryCategory, "inventory_category"), nullable=False
    )
    reorder_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)


class InventoryBatch(Base, TenantScoped, TimestampMixin):
    __tablename__ = "inventory_batches"
    __extra_table_args__ = (
        UniqueConstraint("tenant_id", "item_id", "batch_no", name="uq_batch_item_no"),
        CheckConstraint("qty >= 0", name="ck_batch_qty_non_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch_no: Mapped[str] = mapped_column(String(60), nullable=False)
    expiry: Mapped[date_t] = mapped_column(Date, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class InventoryTransaction(Base, TenantScoped, TimestampMixin):
    """The single ledger. Every movement lands here, whoever caused it."""

    __tablename__ = "inventory_transactions"
    __extra_table_args__ = (Index("ix_inventory_tx_tenant_item", "tenant_id", "item_id", "at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("inventory_items.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[InventoryTxType] = mapped_column(_enum(InventoryTxType, "inventory_tx_type"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_no: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ref_type: Mapped[InventoryRefType | None] = mapped_column(
        _enum(InventoryRefType, "inventory_ref_type"), nullable=True
    )
    ref_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class Invoice(Base, TenantScoped, TimestampMixin):
    __tablename__ = "invoices"
    __extra_table_args__ = (
        UniqueConstraint("tenant_id", "number", name="uq_invoice_tenant_number"),
        Index("ix_invoices_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("encounters.id", ondelete="SET NULL"), nullable=True
    )
    number: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[InvoiceStatus] = mapped_column(
        _enum(InvoiceStatus, "invoice_status"), nullable=False, default=InvoiceStatus.DRAFT
    )
    issued_at: Mapped[date_t] = mapped_column(Date, nullable=False)
    due_at: Mapped[date_t] = mapped_column(Date, nullable=False)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    line_items: Mapped[list[InvoiceLineItem]] = relationship(
        "InvoiceLineItem",
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceLineItem.position",
        lazy="selectin",
    )
    payments: Mapped[list[Payment]] = relationship(
        "Payment",
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="Payment.at",
        lazy="selectin",
    )
    # So an invoice list can name its patient without the register (see the
    # note on Appointment.patient).
    patient: Mapped[Patient] = relationship("Patient", lazy="selectin")


class InvoiceLineItem(Base, TenantScoped, TimestampMixin):
    __tablename__ = "invoice_line_items"
    __extra_table_args__ = (
        CheckConstraint("qty > 0", name="ck_line_qty"),
        CheckConstraint("unit_price >= 0", name="ck_line_price"),
        CheckConstraint("tax_pct >= 0 and tax_pct <= 100", name="ck_line_tax"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str] = mapped_column(String(240), nullable=False)
    source: Mapped[LineItemSource] = mapped_column(
        _enum(LineItemSource, "line_item_source"), nullable=False, default=LineItemSource.MANUAL
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    tax_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)

    invoice: Mapped[Invoice] = relationship("Invoice", back_populates="line_items")


class Payment(Base, TenantScoped, TimestampMixin):
    __tablename__ = "payments"
    __extra_table_args__ = (CheckConstraint("amount > 0", name="ck_payment_amount"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    method: Mapped[PaymentMethod] = mapped_column(_enum(PaymentMethod, "payment_method"), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    taken_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    invoice: Mapped[Invoice] = relationship("Invoice", back_populates="payments")
