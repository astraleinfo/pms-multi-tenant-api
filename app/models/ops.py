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
    Boolean,
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
    BillingDepartment,
    InventoryCategory,
    InventoryRefType,
    InventoryTxType,
    InvoiceStatus,
    LineItemSource,
    PaymentMethod,
    PricingMode,
    ServiceKind,
)


def _enum(cls, name: str):
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class InventoryItem(Base, TenantScoped, TimestampMixin):
    """A stock line, and what it sells for.

    `unit_price` is always the answer — what one unit is billed at. The three
    columns beside it record how that answer was reached (§3.11): a price typed
    outright, a margin over what the box cost, or the printed MRP less a
    discount. Keeping the workings means a cost change can be re-applied
    without anyone recomputing the price in their head.
    """

    __tablename__ = "inventory_items"
    __extra_table_args__ = (
        UniqueConstraint("tenant_id", "sku", name="uq_inventory_tenant_sku"),
        CheckConstraint("unit_price >= 0", name="ck_item_unit_price"),
        CheckConstraint("cost_price >= 0", name="ck_item_cost_price"),
        CheckConstraint("mrp >= 0", name="ck_item_mrp"),
        CheckConstraint("margin_pct >= -100 and margin_pct <= 1000", name="ck_item_margin"),
    )

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

    #: what one unit cost to buy — the basis for a percentage margin
    cost_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    #: the printed maximum retail price, where the pack carries one
    mrp: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    #: margin over cost when `pricing_mode` is `percent`; discount off MRP when
    #: it is `mrp` — negative is therefore meaningful and allowed
    margin_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=0)
    pricing_mode: Mapped[PricingMode] = mapped_column(
        _enum(PricingMode, "pricing_mode"), nullable=False, default=PricingMode.PRICE
    )


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


class ServicePrice(Base, TenantScoped, TimestampMixin):
    """§3.12 — the tariff the practice admin keeps.

    One row per billable thing that is not stock: the consultation, each
    procedure, and each lab test or scan by its catalogue code. Billing reads
    this instead of carrying a number in the code, which is what lets a clinic
    charge its own prices — and lets one scan cost something different from
    another.
    """

    __tablename__ = "service_prices"
    __extra_table_args__ = (
        UniqueConstraint("tenant_id", "kind", "code", name="uq_service_price_kind_code"),
        CheckConstraint("price >= 0", name="ck_service_price_amount"),
        CheckConstraint("tax_pct >= 0 and tax_pct <= 100", name="ck_service_price_tax"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[ServiceKind] = mapped_column(_enum(ServiceKind, "service_kind"), nullable=False)
    #: the lab test code for a lab price; a slug the admin chose otherwise
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    tax_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    #: an inactive price is kept for the invoices that already quote it, but is
    #: not offered to the next one
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Invoice(Base, TenantScoped, TimestampMixin):
    __tablename__ = "invoices"
    __extra_table_args__ = (
        UniqueConstraint("tenant_id", "number", name="uq_invoice_tenant_number"),
        Index("ix_invoices_tenant_status", "tenant_id", "status"),
        Index("ix_invoices_tenant_department", "tenant_id", "department"),
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
    #: which counter raised it — lab and pharmacy bill separately from the
    #: clinic, so one visit can produce three invoices
    department: Mapped[BillingDepartment] = mapped_column(
        _enum(BillingDepartment, "billing_department"),
        nullable=False,
        default=BillingDepartment.CLINIC,
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
    #: What this line bills for, when it bills for something the clinic already
    #: recorded: a prescription item for `pharmacy`, a lab order for `lab`, an
    #: encounter for `consultation`. It is what stops the same medicine being
    #: charged for twice — a charge is outstanding until a line points at it.
    #: `source` says which table to read it in, so no second column is needed.
    ref_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True, index=True)

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
