"""Inventory and billing payloads."""

from __future__ import annotations

from typing import Any

from pydantic import Field

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
from app.schemas.common import ApiModel, IdStr, InputModel, iso_date, money, stamp


class InventoryItemOut(ApiModel):
    """A stock line and its price.

    `unit_price` is what one unit bills at, whichever way the clinic arrived at
    it; `pricing_mode` and the three figures beside it are the workings.
    """

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
    cost_price: float
    mrp: float
    margin_pct: float
    pricing_mode: PricingMode
    manufacturer: str
    description: str
    hsn_code: str

    @classmethod
    def of(cls, i) -> InventoryItemOut:
        return cls(
            id=str(i.id), tenant_id=str(i.tenant_id), sku=i.sku, name=i.name, form=i.form,
            strength=i.strength, unit=i.unit, category=i.category,
            reorder_level=i.reorder_level, unit_price=money(i.unit_price),
            cost_price=money(i.cost_price), mrp=money(i.mrp), margin_pct=money(i.margin_pct),
            pricing_mode=i.pricing_mode, manufacturer=i.manufacturer,
            description=i.description, hsn_code=i.hsn_code,
        )


class InventoryItemIn(InputModel):
    """A new stock line.

    The three pricing figures are all optional and are sent alongside the mode
    that says which of them decides: type a price, add a margin to cost, or
    take the MRP less a discount. The server does that arithmetic — a client
    that computed it itself could disagree with the next one.
    """

    sku: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    form: str = Field(default="", max_length=60)
    strength: str = Field(default="", max_length=60)
    unit: str = Field(default="unit", max_length=40)
    category: InventoryCategory
    reorder_level: int = Field(default=0, ge=0, le=100_000)
    unit_price: float = Field(default=0, ge=0, le=1_000_000)
    cost_price: float = Field(default=0, ge=0, le=1_000_000)
    mrp: float = Field(default=0, ge=0, le=1_000_000)
    margin_pct: float = Field(default=0, ge=-100, le=1000)
    pricing_mode: PricingMode = PricingMode.PRICE


class InventoryItemPatch(InputModel):
    """What may be changed about a stock line. Everything is optional; what is
    sent is what changes."""

    sku: str | None = Field(default=None, min_length=1, max_length=40)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    form: str | None = Field(default=None, max_length=60)
    strength: str | None = Field(default=None, max_length=60)
    unit: str | None = Field(default=None, max_length=40)
    category: InventoryCategory | None = None
    reorder_level: int | None = Field(default=None, ge=0, le=100_000)
    unit_price: float | None = Field(default=None, ge=0, le=1_000_000)
    cost_price: float | None = Field(default=None, ge=0, le=1_000_000)
    mrp: float | None = Field(default=None, ge=0, le=1_000_000)
    margin_pct: float | None = Field(default=None, ge=-100, le=1000)
    pricing_mode: PricingMode | None = None


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
    """One ledger row — and, on a delivery, what it cost and what it sells for.

    The pricing half is optional: send none of it and the item is left priced
    exactly as it was, which is what every movement that is not a purchase
    wants.
    """

    item_id: IdStr
    type: InventoryTxType
    qty: int = Field(ge=-100_000, le=100_000)
    batch_no: str = Field(min_length=1, max_length=60)
    expiry: str | None = None
    note: str | None = Field(default=None, max_length=500)

    unit_price: float | None = Field(default=None, ge=0, le=1_000_000)
    cost_price: float | None = Field(default=None, ge=0, le=1_000_000)
    mrp: float | None = Field(default=None, ge=0, le=1_000_000)
    margin_pct: float | None = Field(default=None, ge=-100, le=1000)
    pricing_mode: PricingMode | None = None


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
    #: the row this line bills for, where it bills for one — a prescription
    #: item, a lab order, an encounter. `source` says which.
    ref_id: str | None = None

    @classmethod
    def of(cls, li) -> InvoiceLineItemOut:
        return cls(
            id=str(li.id), description=li.description, source=li.source, qty=li.qty,
            unit_price=money(li.unit_price), tax_pct=money(li.tax_pct),
            ref_id=str(li.ref_id) if li.ref_id else None,
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
    department: BillingDepartment
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
            status=inv.status, department=inv.department,
            issued_at=iso_date(inv.issued_at) or "", due_at=iso_date(inv.due_at) or "",
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
    #: What this line bills for, when it bills for something already recorded.
    #: Sending it is what marks that charge as settled-for; a line typed by
    #: hand leaves it out.
    ref_id: IdStr | None = None


class InvoiceIn(InputModel):
    """A new invoice.

    `department` says which counter raised it. Left out, it is worked out from
    the lines — an invoice of nothing but lab lines is the lab's — so the
    common case needs no decision from the client.
    """

    patient_id: IdStr
    encounter_id: IdStr | None = None
    department: BillingDepartment | None = None
    line_items: list[InvoiceLineItemIn] = Field(min_length=1, max_length=100)
    issue: bool = False


class PaymentIn(InputModel):
    amount: float = Field(gt=0, le=1_000_000)
    method: PaymentMethod
    reference: str | None = Field(default=None, max_length=120)


# --- a patient's account, counter by counter ---------------------------------


class ChargeOut(ApiModel):
    """Something the patient has had that no invoice covers yet."""

    source: LineItemSource
    department: BillingDepartment
    description: str
    qty: int
    unit_price: float
    tax_pct: float
    #: the row it comes from — a prescription item, a lab order, an encounter
    ref_id: str
    #: false when the clinic's tariff has nothing for it, so it reads as zero
    priced: bool
    total: float

    @classmethod
    def of(cls, charge) -> ChargeOut:
        return cls(
            source=charge.source, department=charge.department, description=charge.description,
            qty=charge.qty, unit_price=charge.unit_price, tax_pct=charge.tax_pct,
            ref_id=charge.ref_id, priced=charge.priced, total=charge.total,
        )


class DepartmentAccountOut(ApiModel):
    """What one counter is owed.

    `outstanding` is on invoices already raised; `unbilled_total` is what has
    not been put on one yet; `due` is what the counter would ask for now.
    `paid` is what it has already taken, which is why it is not in `due`.
    """

    department: BillingDepartment
    outstanding: float
    unbilled_total: float
    due: float
    paid: float
    unbilled: list[ChargeOut]
    invoices: list[InvoiceOut]


class PatientAccountOut(ApiModel):
    patient_id: str
    patient_name: str
    patient_mrn: str
    #: keyed by department, so a screen renders one panel per counter
    departments: dict[str, DepartmentAccountOut]
    total_due: float


class RaiseInvoiceIn(InputModel):
    """Put a counter's outstanding charges onto one invoice.

    `ref_ids` narrows it to particular charges — the dispensing window billing
    exactly what it just handed over. Left out, everything that counter is owed
    for goes on.
    """

    department: BillingDepartment
    issue: bool = True
    ref_ids: list[IdStr] | None = Field(default=None, max_length=200)


class CounterPaymentIn(InputModel):
    """One amount, handed over at one counter."""

    department: BillingDepartment
    amount: float = Field(gt=0, le=1_000_000)
    method: PaymentMethod
    reference: str | None = Field(default=None, max_length=120)


class CounterPaymentOut(ApiModel):
    """What that payment did — which invoices it touched, and what is left."""

    department: BillingDepartment
    paid: float
    remaining: float
    invoices: list[InvoiceOut]


class SuggestedLineOut(ApiModel):
    """One line the visit implies.

    `priced` is false when nothing in the clinic's tariff covers this line, so
    the composer can say "no price set" rather than quietly suggesting zero.
    """

    description: str
    source: LineItemSource
    department: BillingDepartment
    qty: int
    unit_price: float
    tax_pct: float
    priced: bool = True


# --- the tariff (§3.12) ------------------------------------------------------


class ServicePriceOut(ApiModel):
    id: str
    tenant_id: str
    kind: ServiceKind
    code: str
    name: str
    price: float
    tax_pct: float
    active: bool
    note: str | None = None

    @classmethod
    def of(cls, row) -> ServicePriceOut:
        return cls(
            id=str(row.id), tenant_id=str(row.tenant_id), kind=row.kind, code=row.code,
            name=row.name, price=money(row.price), tax_pct=money(row.tax_pct),
            active=row.active, note=row.note,
        )


class ServicePriceIn(InputModel):
    kind: ServiceKind
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=160)
    price: float = Field(ge=0, le=1_000_000)
    tax_pct: float = Field(default=0, ge=0, le=100)
    active: bool = True
    note: str | None = Field(default=None, max_length=500)


class ServicePricePatch(InputModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    price: float | None = Field(default=None, ge=0, le=1_000_000)
    tax_pct: float | None = Field(default=None, ge=0, le=100)
    active: bool | None = None
    note: str | None = Field(default=None, max_length=500)


class LabCatalogueImportIn(InputModel):
    """Give every test this clinic can order a row to price, at one starting
    figure. Tests that already have a price are left exactly as they are."""

    price: float = Field(default=0, ge=0, le=1_000_000)
    tax_pct: float = Field(default=0, ge=0, le=100)


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
