"""§3.12 — invoices and payments.

Totals are always derived from the line items and the payments, so a status can
never contradict the money: `draft → issued → partially_paid → paid`, with
`void` reachable from anywhere.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.clinical import Encounter, LabOrder, Patient, Prescription
from app.models.enums import (
    DEPARTMENT_OF_SOURCE,
    INVOICE_TRANSITIONS,
    BillingDepartment,
    InvoiceStatus,
    LineItemSource,
    ServiceKind,
)
from app.models.ops import InventoryItem, Invoice, InvoiceLineItem, Payment
from app.schemas.ops import InvoiceIn, PaymentIn
from app.services import paging
from app.services import pricing as pricing_service
from app.services import tenants as tenant_service

CENTS = Decimal("0.01")


def _money(value: Decimal | float | int) -> Decimal:
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def totals_for(invoice: Invoice) -> dict[str, float]:
    subtotal = Decimal("0")
    tax = Decimal("0")
    for line in invoice.line_items:
        line_total = _money(Decimal(line.qty) * Decimal(line.unit_price))
        subtotal += line_total
        tax += _money(line_total * Decimal(line.tax_pct) / Decimal(100))
    total = _money(subtotal + tax)
    paid = _money(sum((Decimal(p.amount) for p in invoice.payments), Decimal("0")))
    balance = total - paid
    return {
        "subtotal": float(_money(subtotal)),
        "tax": float(_money(tax)),
        "total": float(total),
        "paid": float(paid),
        "balance": float(balance if balance > 0 else Decimal("0")),
    }


def status_after_payment(invoice: Invoice) -> InvoiceStatus:
    if invoice.status in (InvoiceStatus.VOID, InvoiceStatus.DRAFT):
        return InvoiceStatus(invoice.status)
    figures = totals_for(invoice)
    if figures["paid"] <= 0:
        return InvoiceStatus.ISSUED
    if figures["paid"] + 0.005 < figures["total"]:
        return InvoiceStatus.PARTIALLY_PAID
    return InvoiceStatus.PAID


#: What an invoice is worth and what is still owed on it, as SQL — so the ledger
#: can be ordered by money the database adds up, not money a browser adds up
#: over rows it happens to be holding. It is the same arithmetic as
#: `totals_for`, which stays the one answer a client is ever shown.
def _billed_sql():
    line = InvoiceLineItem.qty * InvoiceLineItem.unit_price
    return (
        select(func.coalesce(func.sum(line + line * InvoiceLineItem.tax_pct / 100), 0))
        .where(InvoiceLineItem.invoice_id == Invoice.id)
        .scalar_subquery()
    )


def _paid_sql():
    return (
        select(func.coalesce(func.sum(Payment.amount), 0))
        .where(Payment.invoice_id == Invoice.id)
        .scalar_subquery()
    )


def _invoice_sorts() -> dict[str, tuple]:
    billed, paid = _billed_sql(), _paid_sql()
    return {
        "issued_at": (Invoice.issued_at, Invoice.number),
        "number": (Invoice.number,),
        "status": (Invoice.status,),
        "total": (billed,),
        "balance": (billed - paid,),
    }


#: The tabs that are a question about money rather than a status value.
UNPAID_STATUSES = (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID)


def department_for(lines) -> BillingDepartment:
    """Which counter an invoice belongs to, from what is on it.

    Lab and pharmacy bill separately (§3.12), so an invoice made only of lab
    lines is the lab's. A mixed one is the clinic's — somebody at the front
    desk composed it deliberately, and splitting it behind their back would
    lose the invoice they meant to raise.
    """
    departments = {DEPARTMENT_OF_SOURCE[LineItemSource(line.source)] for line in lines}
    if len(departments) == 1:
        return departments.pop()
    return BillingDepartment.CLINIC


async def list_invoices(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    status: InvoiceStatus | None = None,
    department: BillingDepartment | None = None,
    patient_id: uuid.UUID | None = None,
    unpaid_only: bool = False,
    search: str | None = None,
    limit: int = 0,
    offset: int = 0,
    sort: str = "issued_at",
    descending: bool = True,
) -> tuple[list[Invoice], int, dict[str, int]]:
    """One page of the ledger, how many invoices matched, and the tab counts.

    The counts are taken before the status filter, so the tabs keep describing
    the whole ledger while one of them is on screen. A department narrows both
    — the lab's tabs are about the lab's invoices, not the clinic's.
    """
    stmt = select(Invoice).where(Invoice.tenant_id == tenant_id)
    if patient_id is not None:
        stmt = stmt.where(Invoice.patient_id == patient_id)
    if department is not None:
        stmt = stmt.where(Invoice.department == department)
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.join(Invoice.patient).where(
            or_(
                Invoice.number.ilike(like),
                Patient.full_name.ilike(like),
                Patient.mrn.ilike(like),
            )
        )

    counts = await paging.counts_by(session, stmt, "status")
    counts["unpaid"] = sum(counts.get(str(s), 0) for s in UNPAID_STATUSES)
    # Prefixed, so a department count can never collide with a status count in
    # the one dictionary the tabs read.
    for value, n in (await paging.counts_by(session, stmt, "department")).items():
        counts[f"department:{value}"] = n

    if status is not None:
        stmt = stmt.where(Invoice.status == status)
    if unpaid_only:
        stmt = stmt.where(Invoice.status.in_(UNPAID_STATUSES))

    columns = _invoice_sorts().get(sort) or _invoice_sorts()["issued_at"]
    order = [c.desc().nulls_last() if descending else c.asc().nulls_last() for c in columns]
    stmt = stmt.order_by(*order, Invoice.id)

    rows, total = await paging.page(session, stmt, limit=limit, offset=offset)
    return rows, total, counts


async def get_invoice(session: AsyncSession, tenant_id: uuid.UUID, invoice_id: uuid.UUID) -> Invoice:
    row = (
        await session.execute(
            select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("No such invoice.")
    return row


async def create_invoice(
    session: AsyncSession, tenant_id: uuid.UUID, payload: InvoiceIn, *, today: date
) -> Invoice:
    patient = (
        await session.execute(
            select(Patient).where(
                Patient.id == uuid.UUID(payload.patient_id), Patient.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if patient is None:
        raise NotFound("No such patient.")

    encounter_id: uuid.UUID | None = None
    if payload.encounter_id:
        encounter = (
            await session.execute(
                select(Encounter).where(
                    Encounter.id == uuid.UUID(payload.encounter_id), Encounter.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if encounter is None:
            raise NotFound("No such consultation.")
        if encounter.patient_id != patient.id:
            raise Conflict("That consultation belongs to a different patient.")
        encounter_id = encounter.id

    config = await tenant_service.get_config(session, tenant_id)
    number = await tenant_service.next_invoice_number(session, tenant_id)

    invoice = Invoice(
        tenant_id=tenant_id,
        patient_id=patient.id,
        encounter_id=encounter_id,
        number=number,
        status=InvoiceStatus.ISSUED if payload.issue else InvoiceStatus.DRAFT,
        department=payload.department or department_for(payload.line_items),
        issued_at=today,
        due_at=today + timedelta(days=config.payment_terms_days),
    )
    session.add(invoice)
    await session.flush()

    for position, line in enumerate(payload.line_items):
        session.add(
            InvoiceLineItem(
                tenant_id=tenant_id,
                invoice_id=invoice.id,
                position=position,
                description=line.description,
                source=line.source,
                qty=line.qty,
                unit_price=_money(line.unit_price),
                tax_pct=_money(line.tax_pct),
                # What this line settles, where it settles something already
                # recorded. Without it the charge stays outstanding and would
                # be offered again at the next visit to the desk.
                ref_id=uuid.UUID(line.ref_id) if line.ref_id else None,
            )
        )

    await session.flush()
    await session.refresh(invoice, ["line_items", "payments", "patient"])
    return invoice


async def issue_invoice(session: AsyncSession, tenant_id: uuid.UUID, invoice_id: uuid.UUID) -> Invoice:
    invoice = await get_invoice(session, tenant_id, invoice_id)
    current = InvoiceStatus(invoice.status)
    if current != InvoiceStatus.DRAFT:
        raise Conflict(f"That invoice is already {current.value.replace('_', ' ')}.")
    if not invoice.line_items:
        raise Conflict("An invoice needs at least one line before it can be issued.")
    invoice.status = InvoiceStatus.ISSUED
    invoice.status = status_after_payment(invoice)
    await session.flush()
    return invoice


async def void_invoice(session: AsyncSession, tenant_id: uuid.UUID, invoice_id: uuid.UUID) -> Invoice:
    invoice = await get_invoice(session, tenant_id, invoice_id)
    current = InvoiceStatus(invoice.status)
    if InvoiceStatus.VOID not in INVOICE_TRANSITIONS[current]:
        raise Conflict("That invoice cannot be voided.")
    invoice.status = InvoiceStatus.VOID
    invoice.voided_at = _now()
    await session.flush()
    return invoice


async def add_payment(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    invoice_id: uuid.UUID,
    payload: PaymentIn,
    *,
    actor_id: uuid.UUID,
) -> Invoice:
    invoice = await get_invoice(session, tenant_id, invoice_id)
    if invoice.status == InvoiceStatus.VOID:
        raise Conflict("A void invoice cannot take a payment.")

    figures = totals_for(invoice)
    amount = _money(payload.amount)
    if float(amount) - figures["balance"] > 0.005:
        raise Conflict(
            f"That is more than the {figures['balance']:.2f} outstanding.",
            details={"balance": figures["balance"]},
        )

    session.add(
        Payment(
            tenant_id=tenant_id,
            invoice_id=invoice.id,
            amount=amount,
            method=payload.method,
            at=_now(),
            reference=payload.reference,
            taken_by=actor_id,
        )
    )
    if invoice.status == InvoiceStatus.DRAFT:
        invoice.status = InvoiceStatus.ISSUED

    await session.flush()
    await session.refresh(invoice, ["payments", "line_items"])
    invoice.status = status_after_payment(invoice)
    await session.flush()
    return invoice


async def suggest_lines(
    session: AsyncSession, tenant_id: uuid.UUID, *, encounter_id: uuid.UUID
) -> list[dict]:
    """§3.12 — what this visit would bill for: the consultation itself, then
    anything the lab and the pharmacy did for it.

    Every figure comes from something the clinic set: the tariff a practice
    admin keeps for consultations and scans, and the selling price on the stock
    line for anything dispensed. Where nothing has been set the line still
    appears, priced at zero and saying so — which is the honest answer to "what
    do you charge for this?" before anyone has said.
    """
    encounter = (
        await session.execute(
            select(Encounter).where(Encounter.id == encounter_id, Encounter.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if encounter is None:
        raise NotFound("No such consultation.")

    config = await tenant_service.get_config(session, tenant_id)
    default_tax = float(config.tax_pct)
    lines: list[dict] = []

    def _line(
        description: str,
        source: LineItemSource,
        qty: int,
        unit_price: float,
        tax_pct: float,
        priced: bool,
    ) -> dict:
        return {
            "description": description,
            "source": source.value,
            "department": DEPARTMENT_OF_SOURCE[source].value,
            "qty": qty,
            "unit_price": unit_price,
            "tax_pct": tax_pct,
            "priced": priced,
        }

    consultation = await pricing_service.price_for(
        session,
        tenant_id,
        kind=ServiceKind.CONSULTATION,
        code=pricing_service.CONSULTATION_CODE,
    )
    lines.append(
        _line(
            "Consultation",
            LineItemSource.CONSULTATION,
            1,
            float(consultation.price) if consultation else 0.0,
            float(consultation.tax_pct) if consultation else default_tax,
            consultation is not None,
        )
    )

    orders = (
        await session.execute(
            select(LabOrder).where(LabOrder.tenant_id == tenant_id, LabOrder.encounter_id == encounter.id)
        )
    ).scalars()
    for order in orders:
        # Each scan carries its own amount: the tariff is keyed by test code,
        # so an ultrasound and a blood count are not the same money.
        tariff = await pricing_service.price_for(
            session, tenant_id, kind=ServiceKind.LAB, code=order.test_code
        )
        lines.append(
            _line(
                f"Lab — {order.test_name}",
                LineItemSource.LAB,
                1,
                float(tariff.price) if tariff else 0.0,
                float(tariff.tax_pct) if tariff else default_tax,
                tariff is not None,
            )
        )

    prescriptions = (
        await session.execute(
            select(Prescription).where(
                Prescription.tenant_id == tenant_id, Prescription.encounter_id == encounter.id
            )
        )
    ).scalars()
    for prescription in prescriptions:
        for item in prescription.items:
            if item.dispensed_qty <= 0:
                continue
            unit_price = 0.0
            priced = False
            if item.item_id:
                stock = await session.get(InventoryItem, item.item_id)
                if stock is not None and stock.tenant_id == tenant_id:
                    unit_price = float(stock.unit_price)
                    priced = True
            lines.append(
                _line(
                    f"{item.medicine} {item.strength}".strip(),
                    LineItemSource.PHARMACY,
                    item.dispensed_qty,
                    unit_price,
                    default_tax,
                    priced,
                )
            )

    return lines
