"""The purchasable modules: pharmacy, lab, inventory and billing.

Every route here sits behind `require_feature`. That guard is the enforcement —
the front end hiding a nav item is a courtesy, not a control.
"""

from __future__ import annotations

from datetime import date as date_t
from typing import Literal

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import TenantContext, parse_uuid, require_feature
from app.core.errors import ValidationFailed
from app.core.permissions import Permission
from app.models.enums import DispenseStatus, InvoiceStatus, LabOrderStatus
from app.schemas.clinical import (
    DispenseIn,
    LabOrderOut,
    LabOrderPageOut,
    LabResultsIn,
    PrescriptionOut,
)
from app.schemas.ops import (
    InventoryBatchOut,
    InventoryItemOut,
    InventoryTxOut,
    InvoiceIn,
    InvoiceOut,
    InvoicePageOut,
    PaymentIn,
    StockLevelOut,
    StockMovementIn,
    SuggestedLineOut,
)
from app.services import audit
from app.services import billing as billing_service
from app.services import inventory as inventory_service
from app.services import lab as lab_service
from app.services import pharmacy as pharmacy_service

# --- pharmacy (§3.9) ---------------------------------------------------------

pharmacy_router = APIRouter(prefix="/pharmacy", tags=["pharmacy"])


@pharmacy_router.get("/prescriptions", response_model=list[PrescriptionOut])
async def pharmacy_queue(
    status_filter: DispenseStatus | None = Query(default=None, alias="status"),
    ctx: TenantContext = Depends(require_feature("pharmacy", Permission.PHARMACY_READ)),
) -> list[PrescriptionOut]:
    """Reads `Prescription` directly. Nothing is copied into a pharmacy table."""
    rows = await pharmacy_service.pending_prescriptions(ctx.session, ctx.tenant_id, status=status_filter)
    return [PrescriptionOut.of(rx, ctx.tz) for rx in rows]


@pharmacy_router.post("/prescriptions/{prescription_id}/dispense", response_model=PrescriptionOut)
async def dispense(
    prescription_id: str,
    payload: DispenseIn,
    ctx: TenantContext = Depends(require_feature("pharmacy", Permission.PHARMACY_DISPENSE)),
) -> PrescriptionOut:
    """Updates the prescription and writes the stock movement in one
    transaction — the ledger and the dispensed quantity cannot disagree."""
    prescription = await pharmacy_service.dispense(
        ctx.session,
        ctx.tenant_id,
        parse_uuid(prescription_id, what="prescription"),
        payload,
        actor_id=ctx.actor_id,
    )
    await audit.record(
        ctx.session, action="pharmacy.dispense", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="prescription",
        entity_id=str(prescription.id),
        meta={"lines": [{"item": line.item_id, "qty": line.qty} for line in payload.lines]},
    )
    return PrescriptionOut.of(prescription, ctx.tz)


# --- lab (§3.10) -------------------------------------------------------------

lab_router = APIRouter(prefix="/lab", tags=["lab"])


@lab_router.get("/orders", response_model=LabOrderPageOut)
async def list_orders(
    status_filter: LabOrderStatus | None = Query(default=None, alias="status"),
    patient_id: str | None = None,
    unverified: bool = Query(default=False, description="Everything still in flight."),
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=0, ge=0, le=200, description="0 asks for the whole collection."),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="ordered_at"),
    order: Literal["asc", "desc"] = Query(default="desc"),
    ctx: TenantContext = Depends(require_feature("lab", Permission.LAB_READ)),
) -> LabOrderPageOut:
    """One page of the order book. `unverified` is the bench's own view — every
    order that has not yet been released into a patient record."""
    rows, total = await lab_service.list_orders(
        ctx.session,
        ctx.tenant_id,
        status=status_filter,
        patient_id=parse_uuid(patient_id, what="patient") if patient_id else None,
        unverified_only=unverified,
        search=search,
        limit=limit,
        offset=offset,
        sort=sort,
        descending=order == "desc",
    )
    return LabOrderPageOut(
        items=[LabOrderOut.of(o, ctx.tz) for o in rows], total=total, limit=limit, offset=offset
    )


@lab_router.get("/orders/{order_id}", response_model=LabOrderOut)
async def get_order(
    order_id: str,
    ctx: TenantContext = Depends(require_feature("lab", Permission.LAB_READ)),
) -> LabOrderOut:
    """One order, by id — so a screen showing a single order can stay truthful
    without holding the page it was opened from."""
    order = await lab_service.get_order(
        ctx.session, ctx.tenant_id, parse_uuid(order_id, what="lab order")
    )
    return LabOrderOut.of(order, ctx.tz)


@lab_router.post("/orders/{order_id}/start", response_model=LabOrderOut)
async def start_order(
    order_id: str,
    ctx: TenantContext = Depends(require_feature("lab", Permission.LAB_RESULT)),
) -> LabOrderOut:
    order = await lab_service.start_order(
        ctx.session, ctx.tenant_id, parse_uuid(order_id, what="lab order")
    )
    return LabOrderOut.of(order, ctx.tz)


@lab_router.post("/orders/{order_id}/results", response_model=LabOrderOut)
async def save_results(
    order_id: str,
    payload: LabResultsIn,
    ctx: TenantContext = Depends(require_feature("lab", Permission.LAB_RESULT)),
) -> LabOrderOut:
    order = await lab_service.save_results(
        ctx.session, ctx.tenant_id, parse_uuid(order_id, what="lab order"), payload, actor_id=ctx.actor_id
    )
    await audit.record(
        ctx.session, action="lab.result", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="lab_order",
        entity_id=str(order.id),
    )
    return LabOrderOut.of(order, ctx.tz)


@lab_router.patch("/orders/{order_id}", response_model=LabOrderOut)
async def verify_order(
    order_id: str,
    payload: dict,
    ctx: TenantContext = Depends(require_feature("lab", Permission.LAB_VERIFY)),
) -> LabOrderOut:
    """`{"status": "verified"}` — the transition that releases a result into
    the patient record."""
    if payload.get("status") != LabOrderStatus.VERIFIED.value:
        raise ValidationFailed("Only a move to `verified` is accepted here.")
    order = await lab_service.verify_order(
        ctx.session, ctx.tenant_id, parse_uuid(order_id, what="lab order"), actor_id=ctx.actor_id
    )
    await audit.record(
        ctx.session, action="lab.verify", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="lab_order",
        entity_id=str(order.id),
    )
    return LabOrderOut.of(order, ctx.tz)


# --- inventory (§3.11) -------------------------------------------------------

inventory_router = APIRouter(prefix="/inventory", tags=["inventory"])


@inventory_router.get("/items", response_model=list[InventoryItemOut])
async def list_items(
    ctx: TenantContext = Depends(require_feature("inventory", Permission.INVENTORY_READ)),
) -> list[InventoryItemOut]:
    rows = await inventory_service.list_items(ctx.session, ctx.tenant_id)
    return [InventoryItemOut.of(i) for i in rows]


@inventory_router.get("/batches", response_model=list[InventoryBatchOut])
async def list_batches(
    ctx: TenantContext = Depends(require_feature("inventory", Permission.INVENTORY_READ)),
) -> list[InventoryBatchOut]:
    rows = await inventory_service.list_batches(ctx.session, ctx.tenant_id)
    return [InventoryBatchOut.of(b) for b in rows]


@inventory_router.get("/transactions", response_model=list[InventoryTxOut])
async def list_transactions(
    item_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=2000),
    ctx: TenantContext = Depends(require_feature("inventory", Permission.INVENTORY_READ)),
) -> list[InventoryTxOut]:
    rows = await inventory_service.list_transactions(
        ctx.session,
        ctx.tenant_id,
        item_id=parse_uuid(item_id, what="stock item") if item_id else None,
        limit=limit,
    )
    return [InventoryTxOut.of(t, ctx.tz) for t in rows]


@inventory_router.get("/levels", response_model=list[StockLevelOut])
async def stock_levels(
    ctx: TenantContext = Depends(require_feature("inventory", Permission.INVENTORY_READ)),
) -> list[StockLevelOut]:
    """Stock on hand is the sum of the ledger, never a stored counter."""
    items = await inventory_service.list_items(ctx.session, ctx.tenant_id)
    levels = await inventory_service.stock_levels(ctx.session, ctx.tenant_id)
    return [
        StockLevelOut(
            item_id=str(item.id),
            on_hand=levels.get(item.id, 0),
            reorder_level=item.reorder_level,
            below_reorder=levels.get(item.id, 0) <= item.reorder_level,
        )
        for item in items
    ]


@inventory_router.get("/expiring", response_model=list[InventoryBatchOut])
async def expiring_batches(
    within_days: int = Query(default=90, ge=1, le=730),
    ctx: TenantContext = Depends(require_feature("inventory", Permission.INVENTORY_READ)),
) -> list[InventoryBatchOut]:
    rows = await inventory_service.near_expiry(
        ctx.session, ctx.tenant_id, today=ctx.today, within_days=within_days
    )
    return [InventoryBatchOut.of(b) for b in rows]


@inventory_router.post(
    "/transactions", response_model=InventoryTxOut, status_code=status.HTTP_201_CREATED
)
async def record_movement(
    payload: StockMovementIn,
    ctx: TenantContext = Depends(require_feature("inventory", Permission.INVENTORY_WRITE)),
) -> InventoryTxOut:
    expiry = date_t.fromisoformat(payload.expiry) if payload.expiry else None
    transaction = await inventory_service.record_movement(
        ctx.session,
        ctx.tenant_id,
        item_id=parse_uuid(payload.item_id, what="stock item"),
        tx_type=payload.type,
        qty=payload.qty,
        batch_no=payload.batch_no,
        actor_id=ctx.actor_id,
        note=payload.note,
        expiry=expiry,
    )
    await audit.record(
        ctx.session, action=f"inventory.{payload.type.value}", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="inventory_transaction", entity_id=str(transaction.id),
    )
    return InventoryTxOut.of(transaction, ctx.tz)


# --- billing (§3.12) ---------------------------------------------------------

billing_router = APIRouter(prefix="/billing", tags=["billing"])


@billing_router.get("/invoices", response_model=InvoicePageOut)
async def list_invoices(
    status_filter: InvoiceStatus | None = Query(default=None, alias="status"),
    patient_id: str | None = None,
    unpaid: bool = Query(default=False, description="Issued or part paid, with a balance."),
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=0, ge=0, le=200, description="0 asks for the whole collection."),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="issued_at"),
    order: Literal["asc", "desc"] = Query(default="desc"),
    ctx: TenantContext = Depends(require_feature("billing", Permission.BILLING_READ)),
) -> InvoicePageOut:
    """One page of the ledger, plus the counts behind the tabs. Ordering by
    total or balance is done on sums the database computes, so a page of ten is
    the ten the ledger means — not the ten a browser could see."""
    rows, total, counts = await billing_service.list_invoices(
        ctx.session,
        ctx.tenant_id,
        status=status_filter,
        patient_id=parse_uuid(patient_id, what="patient") if patient_id else None,
        unpaid_only=unpaid,
        search=search,
        limit=limit,
        offset=offset,
        sort=sort,
        descending=order == "desc",
    )
    return InvoicePageOut(
        items=[InvoiceOut.of(i, ctx.tz, billing_service.totals_for(i)) for i in rows],
        total=total,
        limit=limit,
        offset=offset,
        counts=counts,
    )


@billing_router.post("/invoices", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED)
async def create_invoice(
    payload: InvoiceIn,
    ctx: TenantContext = Depends(require_feature("billing", Permission.BILLING_WRITE)),
) -> InvoiceOut:
    invoice = await billing_service.create_invoice(ctx.session, ctx.tenant_id, payload, today=ctx.today)
    await audit.record(
        ctx.session, action="invoice.create", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="invoice",
        entity_id=str(invoice.id), meta={"number": invoice.number},
    )
    return InvoiceOut.of(invoice, ctx.tz, billing_service.totals_for(invoice))


@billing_router.get("/invoices/suggestions", response_model=list[SuggestedLineOut])
async def suggested_lines(
    encounter_id: str,
    ctx: TenantContext = Depends(require_feature("billing", Permission.BILLING_WRITE)),
) -> list[SuggestedLineOut]:
    lines = await billing_service.suggest_lines(
        ctx.session, ctx.tenant_id, encounter_id=parse_uuid(encounter_id, what="consultation")
    )
    return [SuggestedLineOut(**line) for line in lines]


@billing_router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: str,
    ctx: TenantContext = Depends(require_feature("billing", Permission.BILLING_READ)),
) -> InvoiceOut:
    invoice = await billing_service.get_invoice(
        ctx.session, ctx.tenant_id, parse_uuid(invoice_id, what="invoice")
    )
    return InvoiceOut.of(invoice, ctx.tz, billing_service.totals_for(invoice))


@billing_router.post("/invoices/{invoice_id}/issue", response_model=InvoiceOut)
async def issue_invoice(
    invoice_id: str,
    ctx: TenantContext = Depends(require_feature("billing", Permission.BILLING_WRITE)),
) -> InvoiceOut:
    invoice = await billing_service.issue_invoice(
        ctx.session, ctx.tenant_id, parse_uuid(invoice_id, what="invoice")
    )
    return InvoiceOut.of(invoice, ctx.tz, billing_service.totals_for(invoice))


@billing_router.post("/invoices/{invoice_id}/void", response_model=InvoiceOut)
async def void_invoice(
    invoice_id: str,
    ctx: TenantContext = Depends(require_feature("billing", Permission.BILLING_WRITE)),
) -> InvoiceOut:
    invoice = await billing_service.void_invoice(
        ctx.session, ctx.tenant_id, parse_uuid(invoice_id, what="invoice")
    )
    await audit.record(
        ctx.session, action="invoice.void", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="invoice",
        entity_id=str(invoice.id),
    )
    return InvoiceOut.of(invoice, ctx.tz, billing_service.totals_for(invoice))


@billing_router.post("/invoices/{invoice_id}/payments", response_model=InvoiceOut)
async def add_payment(
    invoice_id: str,
    payload: PaymentIn,
    ctx: TenantContext = Depends(require_feature("billing", Permission.BILLING_WRITE)),
) -> InvoiceOut:
    invoice = await billing_service.add_payment(
        ctx.session, ctx.tenant_id, parse_uuid(invoice_id, what="invoice"), payload, actor_id=ctx.actor_id
    )
    await audit.record(
        ctx.session, action="invoice.payment", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="invoice",
        entity_id=str(invoice.id), meta={"amount": payload.amount, "method": payload.method.value},
    )
    return InvoiceOut.of(invoice, ctx.tz, billing_service.totals_for(invoice))
