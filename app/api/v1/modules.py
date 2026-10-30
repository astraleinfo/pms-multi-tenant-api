"""The operational modules: pharmacy, lab, inventory and billing.

Every clinic has all of them. Each route here sits behind `require_permission`,
so what decides who reaches it is the caller's role — the front end hiding a
nav item is a courtesy, not a control.
"""

from __future__ import annotations

from datetime import date as date_t
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import Response

from app.api.deps import TenantContext, parse_uuid, require_permission
from app.core.config import settings
from app.core.errors import NotFound, PermissionDenied, ValidationFailed
from app.core.permissions import Permission, role_has
from app.core.rate_limit import Rate, limiter
from app.models.enums import BillingDepartment, DispenseStatus, InvoiceStatus, LabOrderStatus
from app.schemas.clinical import (
    DispenseIn,
    LabAttachmentOut,
    LabOrderOut,
    LabOrderPageOut,
    LabResultsIn,
    PrescriptionOut,
)
from app.schemas.ops import (
    AlreadyBilledLineOut,
    ChargeOut,
    CounterPaymentIn,
    CounterPaymentOut,
    DepartmentAccountOut,
    FinalSettlementIn,
    FinalSettlementOut,
    ImportJobOut,
    InventoryBatchOut,
    InventoryItemIn,
    InventoryItemOut,
    InventoryItemPatch,
    InventoryTxOut,
    InvoiceEmailIn,
    InvoiceEmailOut,
    InvoiceIn,
    InvoiceOut,
    InvoicePageOut,
    PatientAccountOut,
    PaymentIn,
    RaiseInvoiceIn,
    StockLevelOut,
    StockMovementIn,
    SuggestedLineOut,
    SuggestedLinesOut,
)
from app.services import accounts as account_service
from app.services import audit, import_jobs, inventory_io, invoice_email
from app.services import billing as billing_service
from app.services import imports as import_service
from app.services import inventory as inventory_service
from app.services import lab as lab_service
from app.services import patients as patient_service
from app.services import pharmacy as pharmacy_service
from app.services import tenants as tenant_service
from app.services.documents import build_invoice_pdf

# --- pharmacy (§3.9) ---------------------------------------------------------

pharmacy_router = APIRouter(prefix="/pharmacy", tags=["pharmacy"])


@pharmacy_router.get("/prescriptions", response_model=list[PrescriptionOut])
async def pharmacy_queue(
    status_filter: DispenseStatus | None = Query(default=None, alias="status"),
    ctx: TenantContext = Depends(require_permission(Permission.PHARMACY_READ)),
) -> list[PrescriptionOut]:
    """Reads `Prescription` directly. Nothing is copied into a pharmacy table."""
    rows = await pharmacy_service.pending_prescriptions(ctx.session, ctx.tenant_id, status=status_filter)
    return [PrescriptionOut.of(rx, ctx.tz) for rx in rows]


@pharmacy_router.post("/prescriptions/{prescription_id}/dispense", response_model=PrescriptionOut)
async def dispense(
    prescription_id: str,
    payload: DispenseIn,
    ctx: TenantContext = Depends(require_permission(Permission.PHARMACY_DISPENSE)),
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
    ctx: TenantContext = Depends(require_permission(Permission.LAB_READ)),
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
    ctx: TenantContext = Depends(require_permission(Permission.LAB_READ)),
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
    ctx: TenantContext = Depends(require_permission(Permission.LAB_RESULT)),
) -> LabOrderOut:
    order = await lab_service.start_order(
        ctx.session, ctx.tenant_id, parse_uuid(order_id, what="lab order")
    )
    return LabOrderOut.of(order, ctx.tz)


@lab_router.post("/orders/{order_id}/results", response_model=LabOrderOut)
async def save_results(
    order_id: str,
    payload: LabResultsIn,
    ctx: TenantContext = Depends(require_permission(Permission.LAB_RESULT)),
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
    ctx: TenantContext = Depends(require_permission(Permission.LAB_VERIFY)),
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


@lab_router.get("/orders/{order_id}/attachments", response_model=list[LabAttachmentOut])
async def list_attachments(
    order_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.LAB_READ)),
) -> list[LabAttachmentOut]:
    """What has been filed against this order — names and sizes, not the files."""
    rows = await lab_service.list_attachments(
        ctx.session, ctx.tenant_id, parse_uuid(order_id, what="lab order")
    )
    return [LabAttachmentOut.of(row, ctx.tz) for row in rows]


@lab_router.post(
    "/orders/{order_id}/attachments",
    response_model=LabAttachmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment(
    order_id: str,
    file: UploadFile = File(..., description="The report: a PDF, an image, or a CSV."),
    note: str | None = Form(default=None),
    ctx: TenantContext = Depends(require_permission(Permission.LAB_RESULT)),
) -> LabAttachmentOut:
    """Attach a scan, a machine printout or a signed report to an order.

    The file is read into the request's transaction and stored as a tenant-owned
    row, so it is behind the same row-level security as the order itself — and
    a verified order refuses new paper, because verification is what released
    the result somebody has already read.
    """
    data = await file.read()
    attachment = await lab_service.add_attachment(
        ctx.session,
        ctx.tenant_id,
        parse_uuid(order_id, what="lab order"),
        filename=file.filename or "report",
        content_type=file.content_type or "application/octet-stream",
        data=data,
        note=note,
        actor_id=ctx.actor_id,
    )
    await audit.record(
        ctx.session, action="lab.attachment.add", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="lab_attachment",
        entity_id=str(attachment.id),
        meta={"order": order_id, "filename": attachment.filename, "bytes": attachment.size_bytes},
    )
    return LabAttachmentOut.of(attachment, ctx.tz)


@lab_router.get("/orders/{order_id}/attachments/{attachment_id}")
async def download_attachment(
    order_id: str,
    attachment_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.LAB_READ)),
) -> Response:
    """The file itself. Reached through its order, so the tenant check that
    protects the order protects the file."""
    attachment = await lab_service.get_attachment(
        ctx.session,
        ctx.tenant_id,
        parse_uuid(order_id, what="lab order"),
        parse_uuid(attachment_id, what="attachment"),
    )
    return Response(
        content=await lab_service.attachment_bytes(attachment),
        media_type=attachment.content_type,
        headers={
            # `inline` so a report opens in the tab it was asked for; the name
            # was stripped of anything that could steer a save dialogue when it
            # was stored.
            "Content-Disposition": f'inline; filename="{attachment.filename}"',
            "Cache-Control": "private, no-store",
        },
    )


@lab_router.delete(
    "/orders/{order_id}/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_attachment(
    order_id: str,
    attachment_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.LAB_RESULT)),
) -> None:
    await lab_service.delete_attachment(
        ctx.session,
        ctx.tenant_id,
        parse_uuid(order_id, what="lab order"),
        parse_uuid(attachment_id, what="attachment"),
    )
    await audit.record(
        ctx.session, action="lab.attachment.remove", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="lab_attachment", entity_id=attachment_id, meta={"order": order_id},
    )


# --- inventory (§3.11) -------------------------------------------------------

inventory_router = APIRouter(prefix="/inventory", tags=["inventory"])


@inventory_router.get("/items", response_model=list[InventoryItemOut])
async def list_items(
    ctx: TenantContext = Depends(require_permission(Permission.INVENTORY_READ)),
) -> list[InventoryItemOut]:
    rows = await inventory_service.list_items(ctx.session, ctx.tenant_id)
    return [InventoryItemOut.of(i) for i in rows]


@inventory_router.post(
    "/items", response_model=InventoryItemOut, status_code=status.HTTP_201_CREATED
)
async def create_item(
    payload: InventoryItemIn,
    ctx: TenantContext = Depends(require_permission(Permission.INVENTORY_WRITE)),
) -> InventoryItemOut:
    """Open a stock line.

    It starts at nothing on hand — the ledger is what puts stock on a shelf, so
    the first purchase comes next and this is only the label.
    """
    item = await inventory_service.create_item(
        ctx.session,
        ctx.tenant_id,
        sku=payload.sku,
        name=payload.name,
        form=payload.form,
        strength=payload.strength,
        unit=payload.unit,
        category=payload.category,
        reorder_level=payload.reorder_level,
        unit_price=payload.unit_price,
        cost_price=payload.cost_price,
        mrp=payload.mrp,
        margin_pct=payload.margin_pct,
        pricing_mode=payload.pricing_mode,
        manufacturer=payload.manufacturer,
        description=payload.description,
        hsn_code=payload.hsn_code,
    )
    await audit.record(
        ctx.session, action="inventory.item.create", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="inventory_item", entity_id=str(item.id),
        meta={"sku": item.sku, "pricing_mode": item.pricing_mode.value},
    )
    return InventoryItemOut.of(item)


@inventory_router.patch("/items/{item_id}", response_model=InventoryItemOut)
async def update_item(
    item_id: str,
    payload: InventoryItemPatch,
    ctx: TenantContext = Depends(require_permission(Permission.INVENTORY_WRITE)),
) -> InventoryItemOut:
    """Edit a stock line — including how it is priced.

    The selling price is re-derived from whatever the mode now says: change the
    cost of a line priced on margin and the price follows, without anyone
    working it out again.
    """
    item = await inventory_service.update_item(
        ctx.session,
        ctx.tenant_id,
        parse_uuid(item_id, what="stock item"),
        payload.model_dump(exclude_unset=True),
    )
    await audit.record(
        ctx.session, action="inventory.item.update", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="inventory_item", entity_id=str(item.id),
        meta={"sku": item.sku, "unit_price": float(item.unit_price)},
    )
    return InventoryItemOut.of(item)


@inventory_router.get("/batches", response_model=list[InventoryBatchOut])
async def list_batches(
    ctx: TenantContext = Depends(require_permission(Permission.INVENTORY_READ)),
) -> list[InventoryBatchOut]:
    rows = await inventory_service.list_batches(ctx.session, ctx.tenant_id)
    return [InventoryBatchOut.of(b) for b in rows]


@inventory_router.get("/transactions", response_model=list[InventoryTxOut])
async def list_transactions(
    item_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=2000),
    ctx: TenantContext = Depends(require_permission(Permission.INVENTORY_READ)),
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
    ctx: TenantContext = Depends(require_permission(Permission.INVENTORY_READ)),
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
    within_days: int | None = Query(default=None, ge=1, le=730),
    ctx: TenantContext = Depends(require_permission(Permission.INVENTORY_READ)),
) -> list[InventoryBatchOut]:
    days = within_days if within_days is not None else ctx.config.expiry_alert_days
    rows = await inventory_service.near_expiry(
        ctx.session, ctx.tenant_id, today=ctx.today, within_days=days
    )
    return [InventoryBatchOut.of(b) for b in rows]


@inventory_router.post(
    "/transactions", response_model=InventoryTxOut, status_code=status.HTTP_201_CREATED
)
async def record_movement(
    payload: StockMovementIn,
    ctx: TenantContext = Depends(require_permission(Permission.INVENTORY_WRITE)),
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
        # A delivery is when the supplier's figures are in front of somebody.
        # Sent, they re-price the line; left out, it keeps the price it had.
        cost_price=payload.cost_price,
        mrp=payload.mrp,
        margin_pct=payload.margin_pct,
        unit_price=payload.unit_price,
        pricing_mode=payload.pricing_mode,
    )
    await audit.record(
        ctx.session, action=f"inventory.{payload.type.value}", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="inventory_transaction", entity_id=str(transaction.id),
    )
    return InventoryTxOut.of(transaction, ctx.tz)


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _spreadsheet(content: bytes, name: str) -> Response:
    return Response(
        content=content,
        media_type=XLSX,
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


@inventory_router.get("/import/template")
async def inventory_import_template(
    ctx: TenantContext = Depends(require_permission(Permission.INVENTORY_WRITE)),
) -> Response:
    """The sample sheet: every field a stock line has, the fixed choices as
    dropdowns, an example per pricing mode, and a sheet saying what each is."""
    content = inventory_io.template(clinic=ctx.tenant.name)
    return _spreadsheet(content, f"{ctx.tenant.slug}-inventory-import.xlsx")


@inventory_router.post(
    "/import", response_model=ImportJobOut, status_code=status.HTTP_202_ACCEPTED
)
async def inventory_import(
    tasks: BackgroundTasks,
    file: UploadFile = File(..., description="The filled-in sample sheet (.xlsx), an export, or a .csv."),
    ctx: TenantContext = Depends(require_permission(Permission.INVENTORY_WRITE)),
) -> ImportJobOut:
    """Hand a sheet of stock lines to the worker, and answer at once.

    The worker checks every row, writes them all or none — a SKU the clinic has
    is an update, a new one a new line, a quantity received a purchase — and
    deletes the upload whichever way it went. Poll the job for the outcome.
    """
    content = await file.read(import_service.MAX_FILE_BYTES + 1)
    job = await import_jobs.queue(
        ctx.session,
        ctx.tenant_id,
        inventory_io.KIND,
        filename=file.filename or "",
        content=content,
        actor=import_jobs.Actor(ctx.actor_id, ctx.principal.label, ctx.actor_kind),
    )
    # Imported here so the API process only touches Celery on this route, and
    # published after the response — the session commits first, so the worker
    # finds the row, and a slow broker never holds the upload's answer.
    from app.worker.tasks import enqueue_import

    tasks.add_task(enqueue_import, ctx.tenant_id, job.id)
    return ImportJobOut.of(job, ctx.tz)


@inventory_router.get("/import/jobs/latest", response_model=ImportJobOut | None)
async def latest_inventory_import(
    ctx: TenantContext = Depends(require_permission(Permission.INVENTORY_WRITE)),
) -> ImportJobOut | None:
    """The clinic's most recent import, so a page opened mid-import can pick it up."""
    job = await import_jobs.latest(ctx.session, ctx.tenant_id, inventory_io.KIND)
    return ImportJobOut.of(job, ctx.tz) if job else None


@inventory_router.get("/import/jobs/{job_id}", response_model=ImportJobOut)
async def inventory_import_job(
    job_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.INVENTORY_WRITE)),
) -> ImportJobOut:
    job = await import_jobs.get_job(
        ctx.session, ctx.tenant_id, parse_uuid(job_id, what="import"), inventory_io.KIND
    )
    if job is None:
        raise NotFound("No such import.")
    return ImportJobOut.of(job, ctx.tz)


@inventory_router.post("/import/jobs/{job_id}/retry", response_model=ImportJobOut)
async def retry_inventory_import(
    job_id: str,
    tasks: BackgroundTasks,
    ctx: TenantContext = Depends(require_permission(Permission.INVENTORY_WRITE)),
) -> ImportJobOut:
    """Hand a stuck import to the worker again — one still queued well after
    its upload, whose hand-off to the broker was lost. `409` for any other."""
    job = await import_jobs.get_job(
        ctx.session, ctx.tenant_id, parse_uuid(job_id, what="import"), inventory_io.KIND
    )
    if job is None:
        raise NotFound("No such import.")
    await import_jobs.retry(ctx.session, job)
    from app.worker.tasks import enqueue_import

    tasks.add_task(enqueue_import, ctx.tenant_id, job.id)
    return ImportJobOut.of(job, ctx.tz)


@inventory_router.get("/export")
async def inventory_export(
    ctx: TenantContext = Depends(require_permission(Permission.INVENTORY_READ)),
) -> Response:
    """Every stock line in the import's own layout, with what is on hand, and
    the batches on the shelf. Edited, it goes straight back in as an import."""
    items = await inventory_service.list_items(ctx.session, ctx.tenant_id)
    levels = await inventory_service.stock_levels(ctx.session, ctx.tenant_id)
    batches = await inventory_service.list_batches(ctx.session, ctx.tenant_id)
    content = inventory_io.export(
        items, levels, batches, today=ctx.today, alert_days=ctx.config.expiry_alert_days
    )
    return _spreadsheet(content, f"{ctx.tenant.slug}-inventory-{ctx.today.isoformat()}.xlsx")


# --- billing (§3.12) ---------------------------------------------------------

billing_router = APIRouter(prefix="/billing", tags=["billing"])


@billing_router.get("/invoices", response_model=InvoicePageOut)
async def list_invoices(
    status_filter: InvoiceStatus | None = Query(default=None, alias="status"),
    department: BillingDepartment | None = Query(
        default=None, description="Which counter raised it: clinic, lab or pharmacy."
    ),
    patient_id: str | None = None,
    unpaid: bool = Query(default=False, description="Issued or part paid, with a balance."),
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=0, ge=0, le=200, description="0 asks for the whole collection."),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="issued_at"),
    order: Literal["asc", "desc"] = Query(default="desc"),
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_READ)),
) -> InvoicePageOut:
    """One page of the ledger, plus the counts behind the tabs. Ordering by
    total or balance is done on sums the database computes, so a page of ten is
    the ten the ledger means — not the ten a browser could see.

    `department` narrows it to one counter — the lab's own book, or the
    pharmacy's — and the counts narrow with it."""
    rows, total, counts = await billing_service.list_invoices(
        ctx.session,
        ctx.tenant_id,
        status=status_filter,
        department=department,
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
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_WRITE)),
) -> InvoiceOut:
    invoice = await billing_service.create_invoice(ctx.session, ctx.tenant_id, payload, today=ctx.today)
    await audit.record(
        ctx.session, action="invoice.create", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="invoice",
        entity_id=str(invoice.id), meta={"number": invoice.number},
    )
    return InvoiceOut.of(invoice, ctx.tz, billing_service.totals_for(invoice))


@billing_router.get("/invoices/suggestions", response_model=SuggestedLinesOut)
async def suggested_lines(
    encounter_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_WRITE)),
) -> SuggestedLinesOut:
    result = await billing_service.suggest_lines(
        ctx.session, ctx.tenant_id, encounter_id=parse_uuid(encounter_id, what="consultation")
    )
    return SuggestedLinesOut(
        lines=[SuggestedLineOut(**line) for line in result["lines"]],
        already_billed=[AlreadyBilledLineOut(**line) for line in result["already_billed"]],
    )


@billing_router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_READ)),
) -> InvoiceOut:
    invoice = await billing_service.get_invoice(
        ctx.session, ctx.tenant_id, parse_uuid(invoice_id, what="invoice")
    )
    return InvoiceOut.of(invoice, ctx.tz, billing_service.totals_for(invoice))


@billing_router.get("/invoices/{invoice_id}/pdf")
async def invoice_pdf(
    invoice_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_READ)),
) -> Response:
    """The invoice as a printable PDF, on the clinic's letterhead.

    Built from the same row and the same `totals_for` the screen reads, so the
    paper and the screen cannot disagree about what is owed. `inline`, so a
    print button opens it in a tab rather than forcing a download.
    """
    invoice = await billing_service.get_invoice(
        ctx.session, ctx.tenant_id, parse_uuid(invoice_id, what="invoice")
    )
    content = build_invoice_pdf(
        invoice,
        billing_service.totals_for(invoice),
        clinic=ctx.tenant.name,
        config=ctx.config,
        currency=ctx.config.currency.value if ctx.config.currency else "",
        logo=await tenant_service.logo_for(ctx.session, ctx.tenant_id),
    )
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="invoice-{_filename_safe(invoice.number)}.pdf"',
            "Cache-Control": "private, no-store",
        },
    )


def _filename_safe(text: str) -> str:
    """An invoice number is the clinic's own prefix plus a sequence; this keeps
    a header value to characters that cannot break out of the quotes."""
    return "".join(ch for ch in text if ch.isalnum() or ch in "-_") or "invoice"


@billing_router.post("/invoices/{invoice_id}/send-email", response_model=InvoiceEmailOut)
async def email_invoice(
    invoice_id: str,
    payload: InvoiceEmailIn,
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_WRITE)),
) -> InvoiceEmailOut:
    """Email the invoice to its patient, the same PDF the print button opens
    attached, worded as the clinic set it up in Settings.

    Billing work that messages a patient, so it needs both: every role can
    bill, but only the roles that may message patients may send one. Sent
    synchronously — the person at the desk is waiting to hear whether it went.

    Budgeted per member of staff (`INVOICE_EMAIL_RATE_LIMIT`): each one leaves
    the platform's address carrying a patient's bill to any address typed, so
    one session cannot send an unbounded number. Every attempt counts.
    """
    if not role_has(ctx.user.role, Permission.MESSAGE_SEND):
        raise PermissionDenied("Your role does not allow that.")
    limiter.check(f"invoice-email:{ctx.user.id}", Rate.parse(settings.invoice_email_rate_limit))
    invoice = await billing_service.get_invoice(
        ctx.session, ctx.tenant_id, parse_uuid(invoice_id, what="invoice")
    )
    # Refused before the PDF is drawn: a void or draft bill is never sent, and
    # neither is one with nobody to send it to.
    to = invoice_email.check_sendable(invoice, payload.to)
    totals = billing_service.totals_for(invoice)
    currency = ctx.config.currency.value if ctx.config.currency else ""
    pdf = build_invoice_pdf(
        invoice, totals, clinic=ctx.tenant.name, config=ctx.config, currency=currency,
        logo=await tenant_service.logo_for(ctx.session, ctx.tenant_id),
    )
    status_, to, detail = await invoice_email.send(
        ctx.session, invoice=invoice, totals=totals, pdf=pdf, to=to,
        filename=f"invoice-{_filename_safe(invoice.number)}.pdf", clinic=ctx.tenant.name,
        config=ctx.config, currency=currency, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label,
    )
    return InvoiceEmailOut(
        status=status_, to=to, detail=detail, invoice=InvoiceOut.of(invoice, ctx.tz, totals)
    )


@billing_router.post("/invoices/{invoice_id}/issue", response_model=InvoiceOut)
async def issue_invoice(
    invoice_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_WRITE)),
) -> InvoiceOut:
    invoice = await billing_service.issue_invoice(
        ctx.session, ctx.tenant_id, parse_uuid(invoice_id, what="invoice")
    )
    return InvoiceOut.of(invoice, ctx.tz, billing_service.totals_for(invoice))


@billing_router.post("/invoices/{invoice_id}/void", response_model=InvoiceOut)
async def void_invoice(
    invoice_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_WRITE)),
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
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_WRITE)),
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


# --- a patient's account (§3.12) ---------------------------------------------
#
# One patient, three counters. The clinic, the lab and the pharmacy each keep
# their own invoices and each is settled on its own — paying for your medicine
# at the window is not paying for your scan.


@billing_router.get("/patients/{patient_id}/account", response_model=PatientAccountOut)
async def patient_account(
    patient_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_READ)),
) -> PatientAccountOut:
    """What this patient owes, and what nobody has billed them for yet.

    The unbilled half is worked out from what the clinic already recorded —
    medicine handed over, tests ordered, consultations held — minus whatever an
    invoice line already points at. It is the question the front desk asks when
    somebody comes to pay on the way out.
    """
    uid = parse_uuid(patient_id, what="patient")
    patient = await patient_service.get_patient(ctx.session, ctx.tenant_id, uid)
    accounts = await account_service.account(ctx.session, ctx.tenant_id, uid)

    departments = {
        department.value: DepartmentAccountOut(
            department=department,
            outstanding=entry.outstanding,
            unbilled_total=entry.unbilled_total,
            due=entry.due,
            paid=entry.paid,
            unbilled=[ChargeOut.of(charge) for charge in entry.unbilled],
            invoices=[
                InvoiceOut.of(invoice, ctx.tz, billing_service.totals_for(invoice))
                for invoice in entry.invoices
            ],
        )
        for department, entry in accounts.items()
    }
    return PatientAccountOut(
        patient_id=str(patient.id),
        patient_name=patient.full_name,
        patient_mrn=patient.mrn,
        departments=departments,
        total_due=round(sum(entry.due for entry in accounts.values()), 2),
    )


@billing_router.get("/patients/{patient_id}/charges", response_model=list[ChargeOut])
async def unbilled_charges(
    patient_id: str,
    department: BillingDepartment | None = Query(default=None),
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_READ)),
) -> list[ChargeOut]:
    """Just the unbilled half, optionally for one counter — what the dispensing
    window shows when it asks for money."""
    charges = await account_service.unbilled_charges(
        ctx.session,
        ctx.tenant_id,
        parse_uuid(patient_id, what="patient"),
        department=department,
    )
    return [ChargeOut.of(charge) for charge in charges]


@billing_router.post(
    "/patients/{patient_id}/invoices", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED
)
async def raise_counter_invoice(
    patient_id: str,
    payload: RaiseInvoiceIn,
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_WRITE)),
) -> InvoiceOut:
    """Put one counter's outstanding charges onto an invoice.

    Each line points back at what it bills, so the same box of tablets cannot
    be charged for twice however many times this is called.
    """
    invoice = await account_service.raise_invoice(
        ctx.session,
        ctx.tenant_id,
        parse_uuid(patient_id, what="patient"),
        department=payload.department,
        today=ctx.today,
        issue=payload.issue,
        ref_ids=payload.ref_ids,
    )
    await audit.record(
        ctx.session, action="invoice.create", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="invoice",
        entity_id=str(invoice.id),
        meta={"number": invoice.number, "department": payload.department.value, "counter": True},
    )
    return InvoiceOut.of(invoice, ctx.tz, billing_service.totals_for(invoice))


@billing_router.post("/patients/{patient_id}/final-settlement", response_model=FinalSettlementOut)
async def final_settlement(
    patient_id: str,
    payload: FinalSettlementIn,
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_WRITE)),
) -> FinalSettlementOut:
    """The patient is leaving: one bill for everything still owing, and one
    payment for it. What a counter was paid at its window is not asked for
    again; a counter bill nobody paid is folded into this one."""
    uid = parse_uuid(patient_id, what="patient")
    unpaid_before = {
        invoice.id: invoice.number
        for invoice in await billing_service.foldable_invoices(ctx.session, ctx.tenant_id, uid)
    }
    invoice = await account_service.raise_final_invoice(ctx.session, ctx.tenant_id, uid, today=ctx.today)
    folded = [number for inv_id, number in unpaid_before.items() if invoice is not None and inv_id != invoice.id]
    if invoice is not None:
        await audit.record(
            ctx.session, action="invoice.create", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
            actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="invoice",
            entity_id=str(invoice.id),
            meta={"number": invoice.number, "final": True, "folded": folded},
        )

    owed = await account_service.outstanding(ctx.session, ctx.tenant_id, uid)
    paid = 0.0
    if payload.collect and owed > 0.005:
        settled = await account_service.settle(
            ctx.session,
            ctx.tenant_id,
            uid,
            department=None,
            amount=owed,
            method=payload.method,
            reference=payload.reference,
            actor_id=ctx.actor_id,
        )
        paid = owed
        await audit.record(
            ctx.session, action="invoice.payment", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
            actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="patient",
            entity_id=str(uid),
            meta={"amount": owed, "method": payload.method.value, "final": True,
                  "invoices": [str(i.id) for i in settled]},
        )

    if invoice is not None:
        invoice = await billing_service.get_invoice(ctx.session, ctx.tenant_id, invoice.id)
        await ctx.session.refresh(invoice, ["payments", "line_items"])
    return FinalSettlementOut(
        invoice=InvoiceOut.of(invoice, ctx.tz, billing_service.totals_for(invoice)) if invoice else None,
        folded=folded,
        paid=paid,
        remaining=await account_service.outstanding(ctx.session, ctx.tenant_id, uid),
    )


@billing_router.post("/patients/{patient_id}/payments", response_model=CounterPaymentOut)
async def take_counter_payment(
    patient_id: str,
    payload: CounterPaymentIn,
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_WRITE)),
) -> CounterPaymentOut:
    """Take one amount at one counter, oldest invoice first.

    A patient hands over one sum at one window; which invoices it clears is the
    ledger's problem rather than theirs.
    """
    uid = parse_uuid(patient_id, what="patient")
    settled = await account_service.settle(
        ctx.session,
        ctx.tenant_id,
        uid,
        department=payload.department,
        amount=payload.amount,
        method=payload.method,
        reference=payload.reference,
        actor_id=ctx.actor_id,
    )
    await audit.record(
        ctx.session, action="invoice.payment", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="patient",
        entity_id=str(uid),
        meta={
            "amount": payload.amount,
            "method": payload.method.value,
            "department": payload.department.value,
            "invoices": [str(invoice.id) for invoice in settled],
        },
    )

    accounts = await account_service.account(ctx.session, ctx.tenant_id, uid)
    return CounterPaymentOut(
        department=payload.department,
        paid=payload.amount,
        remaining=accounts[payload.department].outstanding,
        invoices=[
            InvoiceOut.of(invoice, ctx.tz, billing_service.totals_for(invoice))
            for invoice in settled
        ],
    )
