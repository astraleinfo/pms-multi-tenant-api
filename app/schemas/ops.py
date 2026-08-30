"""Inventory and billing payloads."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.models.enums import (
    InventoryCategory,
    InventoryRefType,
    InventoryTxType,
    InvoiceStatus,
    LineItemSource,
    PaymentMethod,
)
from app.schemas.common import ApiModel, IdStr, InputModel, iso_date, money, stamp


class InventoryItemOut(ApiModel):
    id: str
    tenant_id: str
    sku: str
    name: str
    form: str
    strength: str
    unit: str
    category: InventoryCategory
    reorder_level: int
    unit_price: float

    @classmethod
    def of(cls, i) -> InventoryItemOut:
        return cls(
            id=str(i.id), tenant_id=str(i.tenant_id), sku=i.sku, name=i.name, form=i.form,
            strength=i.strength, unit=i.unit, category=i.category,
            reorder_level=i.reorder_level, unit_price=money(i.unit_price),
        )


class InventoryBatchOut(ApiModel):
    batch_no: str
    item_id: str
    expiry: str
    qty: int

    @classmethod
    def of(cls, b) -> InventoryBatchOut:
        return cls(batch_no=b.batch_no, item_id=str(b.item_id), expiry=iso_date(b.expiry) or "", qty=b.qty)


class InventoryTxOut(ApiModel):
    id: str
    tenant_id: str
    item_id: str
    type: InventoryTxType
    qty: int
    batch_no: str
    at: str
    ref_type: InventoryRefType | None = None
    ref_id: str | None = None
    note: str | None = None
    actor_id: str | None = None

    @classmethod
    def of(cls, t, tz: str) -> InventoryTxOut:
        return cls(
            id=str(t.id), tenant_id=str(t.tenant_id), item_id=str(t.item_id), type=t.type, qty=t.qty,
            batch_no=t.batch_no, at=stamp(t.at, tz) or "", ref_type=t.ref_type,
            ref_id=str(t.ref_id) if t.ref_id else None, note=t.note,
            actor_id=str(t.actor_id) if t.actor_id else None,
        )


class StockMovementIn(InputModel):
    item_id: IdStr
    type: InventoryTxType
    qty: int = Field(ge=-100_000, le=100_000)
    batch_no: str = Field(min_length=1, max_length=60)
    expiry: str | None = None
    note: str | None = Field(default=None, max_length=500)


class StockLevelOut(ApiModel):
    item_id: str
    on_hand: int
    reorder_level: int
    below_reorder: bool


class InvoiceLineItemOut(ApiModel):
    id: str
    description: str
    source: LineItemSource
    qty: int
    unit_price: float
    tax_pct: float

    @classmethod
    def of(cls, li) -> InvoiceLineItemOut:
        return cls(
            id=str(li.id), description=li.description, source=li.source, qty=li.qty,
            unit_price=money(li.unit_price), tax_pct=money(li.tax_pct),
        )


class PaymentOut(ApiModel):
    id: str
    invoice_id: str
    amount: float
    method: PaymentMethod
    at: str
    reference: str | None = None

    @classmethod
    def of(cls, p, tz: str) -> PaymentOut:
        return cls(
            id=str(p.id), invoice_id=str(p.invoice_id), amount=money(p.amount), method=p.method,
            at=stamp(p.at, tz) or "", reference=p.reference,
        )


class InvoiceTotalsOut(ApiModel):
    subtotal: float
    tax: float
    total: float
    paid: float
    balance: float


class InvoiceOut(ApiModel):
    id: str
    tenant_id: str
    patient_id: str
    patient_name: str
    patient_mrn: str
    encounter_id: str | None
    number: str
    status: InvoiceStatus
    issued_at: str
    due_at: str
    line_items: list[InvoiceLineItemOut]
    payments: list[PaymentOut]
    totals: InvoiceTotalsOut

    @classmethod
    def of(cls, inv, tz: str, totals: dict[str, float]) -> InvoiceOut:
        return cls(
            id=str(inv.id), tenant_id=str(inv.tenant_id), patient_id=str(inv.patient_id),
            patient_name=inv.patient.full_name, patient_mrn=inv.patient.mrn,
            encounter_id=str(inv.encounter_id) if inv.encounter_id else None, number=inv.number,
            status=inv.status, issued_at=iso_date(inv.issued_at) or "", due_at=iso_date(inv.due_at) or "",
            line_items=[InvoiceLineItemOut.of(li) for li in inv.line_items],
            payments=[PaymentOut.of(p, tz) for p in inv.payments],
            totals=InvoiceTotalsOut(**totals),
        )


class InvoicePageOut(ApiModel):
    """One page of the ledger, the match count, and the tab counts.

    `counts` is keyed by invoice status, plus `unpaid` for the tab that asks a
    question about money rather than about a status value.
    """

    items: list[InvoiceOut]
    total: int
    limit: int
    offset: int
    counts: dict[str, int]


class InvoiceLineItemIn(InputModel):
    description: str = Field(min_length=1, max_length=240)
    source: LineItemSource = LineItemSource.MANUAL
    qty: int = Field(default=1, ge=1, le=10_000)
    unit_price: float = Field(ge=0, le=1_000_000)
    tax_pct: float = Field(default=0, ge=0, le=100)


class InvoiceIn(InputModel):
    patient_id: IdStr
    encounter_id: IdStr | None = None
    line_items: list[InvoiceLineItemIn] = Field(min_length=1, max_length=100)
    issue: bool = False


class PaymentIn(InputModel):
    amount: float = Field(gt=0, le=1_000_000)
    method: PaymentMethod
    reference: str | None = Field(default=None, max_length=120)


class SuggestedLineOut(ApiModel):
    description: str
    source: LineItemSource
    qty: int
    unit_price: float
    tax_pct: float


class AnalyticsOut(ApiModel):
    """§3.14's read model.

    `core` is computed for every clinic; `modules` holds a key only for
    features that are actually active; `clinical` is driven by `clinical_ext`
    alone. A client renders what it is given rather than deciding what to
    compute — which is why a locked module is simply absent here.
    """

    range: dict[str, Any]
    core: dict[str, Any]
    modules: dict[str, Any]
    clinical: dict[str, Any]
