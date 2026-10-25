"""§3.12 — a patient's account, counter by counter.

Two questions the front desk and the dispensing window both ask, and which no
single invoice can answer:

*   **What has this patient had that nobody has billed yet?** Medicine handed
    over, tests ordered, a consultation held. Each of those is already recorded
    somewhere; a charge is outstanding until an invoice line points at it
    (`InvoiceLineItem.ref_id`), which is what stops the same box of tablets
    being charged for twice.
*   **What do they still owe, and to whom?** The lab, the pharmacy and the
    clinic each keep their own invoices, so each is settled on its own — a
    patient paying for their medicine at the window has not thereby paid for
    their scan.

Both answers are assembled here rather than in a route, because the pharmacy
window and the billing desk need exactly the same arithmetic and must never
disagree about it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict
from app.models.clinical import Encounter, LabOrder, Prescription, PrescriptionItem
from app.models.enums import (
    BillingDepartment,
    InvoiceStatus,
    LineItemSource,
    PaymentMethod,
    ServiceKind,
)
from app.models.ops import InventoryItem, Invoice
from app.schemas.ops import InvoiceIn, InvoiceLineItemIn, PaymentIn
from app.services import billing as billing_service
from app.services import patients as patient_service
from app.services import pricing as pricing_service
from app.services import tenants as tenant_service


@dataclass
class Charge:
    """Something the patient has had, which nobody has billed yet."""

    source: LineItemSource
    department: BillingDepartment
    description: str
    qty: int
    unit_price: float
    tax_pct: float
    ref_id: str
    #: false when the clinic's tariff has nothing to say about it — the line
    #: still appears, at zero, saying so
    priced: bool = True

    @property
    def total(self) -> float:
        gross = self.qty * self.unit_price
        return round(gross + gross * self.tax_pct / 100, 2)


@dataclass
class DepartmentAccount:
    """One counter's half of a patient's account."""

    department: BillingDepartment
    unbilled: list[Charge] = field(default_factory=list)
    invoices: list[Invoice] = field(default_factory=list)
    outstanding: float = 0.0
    #: what this counter has already taken, over every invoice it ever raised.
    #: The final settlement subtracts nothing — money paid at the window simply
    #: never appears in `due` — but the desk still needs to see that it happened,
    #: or a counter that is square looks the same as one that never charged.
    paid: float = 0.0

    @property
    def unbilled_total(self) -> float:
        return round(sum(charge.total for charge in self.unbilled), 2)

    @property
    def due(self) -> float:
        """Everything the counter would ask for right now — what is on an
        invoice and unpaid, plus what has not been put on one yet."""
        return round(self.outstanding + self.unbilled_total, 2)


async def _billed_ref_qty(
    session: AsyncSession, tenant_id: uuid.UUID, source: LineItemSource, ref_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """How much of each referenced row has already been billed."""
    details = await billing_service.billed_ref_lines(session, tenant_id, source, ref_ids)
    return {ref_id: sum(entry["qty"] for entry in entries) for ref_id, entries in details.items()}


async def _pharmacy_charges(
    session: AsyncSession, tenant_id: uuid.UUID, patient_id: uuid.UUID, *, tax: float
) -> list[Charge]:
    """Medicine actually handed over, less whatever has been billed for it.

    Dispensed, not prescribed: a patient pays for what they walked out with, so
    a partly dispensed line is a partial charge and the rest follows when the
    rest is handed over.
    """
    items = list(
        (
            await session.execute(
                select(PrescriptionItem)
                .join(Prescription, Prescription.id == PrescriptionItem.prescription_id)
                .where(
                    PrescriptionItem.tenant_id == tenant_id,
                    Prescription.patient_id == patient_id,
                    PrescriptionItem.dispensed_qty > 0,
                )
                .order_by(Prescription.issued_at, PrescriptionItem.position)
            )
        ).scalars()
    )
    if not items:
        return []

    billed = await _billed_ref_qty(
        session, tenant_id, LineItemSource.PHARMACY, [item.id for item in items]
    )

    charges: list[Charge] = []
    for item in items:
        outstanding = item.dispensed_qty - billed.get(item.id, 0)
        if outstanding <= 0:
            continue

        unit_price = 0.0
        priced = False
        if item.item_id:
            stock = await session.get(InventoryItem, item.item_id)
            if stock is not None and stock.tenant_id == tenant_id:
                unit_price = float(stock.unit_price)
                priced = True

        charges.append(
            Charge(
                source=LineItemSource.PHARMACY,
                department=BillingDepartment.PHARMACY,
                description=f"{item.medicine} {item.strength}".strip(),
                qty=outstanding,
                unit_price=unit_price,
                tax_pct=tax,
                ref_id=str(item.id),
                priced=priced,
            )
        )
    return charges


async def _lab_charges(
    session: AsyncSession, tenant_id: uuid.UUID, patient_id: uuid.UUID, *, tax: float
) -> list[Charge]:
    """Tests the bench has taken on, priced by their own code."""
    orders = list(
        (
            await session.execute(
                select(LabOrder)
                .where(LabOrder.tenant_id == tenant_id, LabOrder.patient_id == patient_id)
                .order_by(LabOrder.ordered_at)
            )
        ).scalars()
    )
    if not orders:
        return []

    billed = await _billed_ref_qty(
        session, tenant_id, LineItemSource.LAB, [order.id for order in orders]
    )

    charges: list[Charge] = []
    for order in orders:
        if billed.get(order.id, 0) > 0:
            continue
        tariff = await pricing_service.price_for(
            session, tenant_id, kind=ServiceKind.LAB, code=order.test_code
        )
        charges.append(
            Charge(
                source=LineItemSource.LAB,
                department=BillingDepartment.LAB,
                description=f"Lab — {order.test_name}",
                qty=1,
                unit_price=float(tariff.price) if tariff else 0.0,
                tax_pct=float(tariff.tax_pct) if tariff else tax,
                ref_id=str(order.id),
                priced=tariff is not None,
            )
        )
    return charges


async def _consultation_charges(
    session: AsyncSession, tenant_id: uuid.UUID, patient_id: uuid.UUID, *, tax: float
) -> list[Charge]:
    """One consultation fee per visit the clinician has signed."""
    encounters = list(
        (
            await session.execute(
                select(Encounter)
                .where(Encounter.tenant_id == tenant_id, Encounter.patient_id == patient_id)
                .order_by(Encounter.date)
            )
        ).scalars()
    )
    if not encounters:
        return []

    billed = await _billed_ref_qty(
        session, tenant_id, LineItemSource.CONSULTATION, [e.id for e in encounters]
    )

    charges: list[Charge] = []
    for encounter in encounters:
        if billed.get(encounter.id, 0) > 0:
            continue
        price, encounter_tax, priced = await pricing_service.resolve_consultation_price(
            session, tenant_id, encounter.doctor_id
        )
        charges.append(
            Charge(
                source=LineItemSource.CONSULTATION,
                department=BillingDepartment.CLINIC,
                description=f"Consultation — {encounter.date.isoformat()}",
                qty=1,
                unit_price=float(price),
                tax_pct=float(encounter_tax),
                ref_id=str(encounter.id),
                priced=priced,
            )
        )
    return charges


async def unbilled_charges(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    patient_id: uuid.UUID,
    *,
    department: BillingDepartment | None = None,
) -> list[Charge]:
    """Everything this patient has had that no invoice covers yet."""
    config = await tenant_service.get_config(session, tenant_id)
    tax = float(config.tax_pct)

    charges: list[Charge] = []
    if department in (None, BillingDepartment.CLINIC):
        charges += await _consultation_charges(session, tenant_id, patient_id, tax=tax)
    if department in (None, BillingDepartment.LAB):
        charges += await _lab_charges(session, tenant_id, patient_id, tax=tax)
    if department in (None, BillingDepartment.PHARMACY):
        charges += await _pharmacy_charges(session, tenant_id, patient_id, tax=tax)
    return charges


async def _unpaid_invoices(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    patient_id: uuid.UUID,
    *,
    department: BillingDepartment | None = None,
) -> list[Invoice]:
    """Issued or part paid, oldest first — the order money is applied in."""
    stmt = (
        select(Invoice)
        .where(
            Invoice.tenant_id == tenant_id,
            Invoice.patient_id == patient_id,
            Invoice.status.in_(billing_service.UNPAID_STATUSES),
        )
        .order_by(Invoice.issued_at, Invoice.number)
    )
    if department is not None:
        stmt = stmt.where(Invoice.department == department)
    return list((await session.execute(stmt)).scalars())


async def _counter_invoices(
    session: AsyncSession, tenant_id: uuid.UUID, patient_id: uuid.UUID
) -> list[Invoice]:
    """Every bill this patient has that still counts — a void one is a mistake
    the clinic undid, so it is neither owed nor paid."""
    stmt = (
        select(Invoice)
        .where(
            Invoice.tenant_id == tenant_id,
            Invoice.patient_id == patient_id,
            Invoice.status != InvoiceStatus.VOID,
        )
        .order_by(Invoice.issued_at, Invoice.number)
    )
    return list((await session.execute(stmt)).scalars())


async def account(
    session: AsyncSession, tenant_id: uuid.UUID, patient_id: uuid.UUID
) -> dict[BillingDepartment, DepartmentAccount]:
    """The whole picture: what each counter is still owed, and what it has not
    put on an invoice yet."""
    await patient_service.get_patient(session, tenant_id, patient_id)

    accounts = {
        department: DepartmentAccount(department=department) for department in BillingDepartment
    }

    for charge in await unbilled_charges(session, tenant_id, patient_id):
        accounts[charge.department].unbilled.append(charge)

    for invoice in await _counter_invoices(session, tenant_id, patient_id):
        figures = billing_service.totals_for(invoice)
        entry = accounts[BillingDepartment(invoice.department)]
        entry.paid = round(entry.paid + figures["paid"], 2)
        if invoice.status in billing_service.UNPAID_STATUSES:
            entry.invoices.append(invoice)
            entry.outstanding = round(entry.outstanding + figures["balance"], 2)

    return accounts


async def raise_invoice(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    patient_id: uuid.UUID,
    *,
    department: BillingDepartment,
    today: date,
    issue: bool = True,
    ref_ids: list[str] | None = None,
) -> Invoice:
    """Put one counter's outstanding charges onto an invoice.

    `ref_ids` narrows it to particular charges — the dispensing window billing
    exactly what it just handed over, rather than everything the pharmacy has
    ever been owed for.
    """
    charges = await unbilled_charges(session, tenant_id, patient_id, department=department)
    if ref_ids is not None:
        wanted = set(ref_ids)
        charges = [charge for charge in charges if charge.ref_id in wanted]

    if not charges:
        raise Conflict("There is nothing left to bill at that counter.")

    invoice = await billing_service.create_invoice(
        session,
        tenant_id,
        InvoiceIn(
            patient_id=str(patient_id),
            department=department,
            issue=issue,
            line_items=[
                InvoiceLineItemIn(
                    description=charge.description,
                    source=charge.source,
                    qty=charge.qty,
                    unit_price=charge.unit_price,
                    tax_pct=charge.tax_pct,
                    ref_id=charge.ref_id,
                )
                for charge in charges
            ],
        ),
        today=today,
    )
    return invoice


async def raise_final_invoice(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    patient_id: uuid.UUID,
    *,
    today: date,
) -> Invoice | None:
    """Everything this patient still owes for, on one bill.

    What a counter was paid for at its window stays on that counter's paid bill
    and is not asked for again. Everything else — the consultation, medicine or
    tests put "on the bill", and any counter bill issued but never paid — goes
    onto this one, so the patient settles the visit once. A bill already part
    paid is left as it is; its balance is still owed and is collected with this.
    """
    await patient_service.get_patient(session, tenant_id, patient_id)

    # The unpaid bills' lines are carried over exactly as they were charged;
    # creating the new bill then withdraws them (`create_invoice` folds any
    # unpaid bill it fully covers).
    carried = [
        InvoiceLineItemIn(
            description=line.description,
            source=line.source,
            qty=line.qty,
            unit_price=float(line.unit_price),
            tax_pct=float(line.tax_pct),
            ref_id=str(line.ref_id),
        )
        for invoice in await billing_service.foldable_invoices(session, tenant_id, patient_id)
        for line in sorted(invoice.line_items, key=lambda line: line.position)
    ]
    fresh = [
        InvoiceLineItemIn(
            description=charge.description,
            source=charge.source,
            qty=charge.qty,
            unit_price=charge.unit_price,
            tax_pct=charge.tax_pct,
            ref_id=charge.ref_id,
        )
        for charge in await unbilled_charges(session, tenant_id, patient_id)
    ]
    if not carried and not fresh:
        return None
    return await billing_service.create_invoice(
        session,
        tenant_id,
        InvoiceIn(patient_id=str(patient_id), issue=True, line_items=fresh + carried),
        today=today,
    )


async def outstanding(session: AsyncSession, tenant_id: uuid.UUID, patient_id: uuid.UUID) -> float:
    """What is owed on bills already issued, across every counter."""
    invoices = await _unpaid_invoices(session, tenant_id, patient_id)
    return round(sum(billing_service.totals_for(invoice)["balance"] for invoice in invoices), 2)


async def settle(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    patient_id: uuid.UUID,
    *,
    department: BillingDepartment | None,
    amount: float,
    method: PaymentMethod,
    reference: str | None,
    actor_id: uuid.UUID,
) -> list[Invoice]:
    """Take one payment against one counter, oldest invoice first.

    A patient hands over one amount at one window; which invoices it clears is
    the ledger's problem, not theirs. No department means every counter — the
    final settlement at the desk. Anything beyond what that counter is owed
    is refused rather than left sitting as a credit — an overpayment is nearly
    always a typed figure, and a credit nobody can see is worse than a refusal.
    """
    invoices = await _unpaid_invoices(session, tenant_id, patient_id, department=department)
    if not invoices:
        raise Conflict("That counter is not owed anything.")

    owed = round(
        sum(billing_service.totals_for(invoice)["balance"] for invoice in invoices), 2
    )
    remaining = pricing_service.money(amount)
    if float(remaining) - owed > 0.005:
        raise Conflict(
            f"That is more than the {owed:.2f} outstanding at that counter.",
            details={"outstanding": owed},
        )

    settled: list[Invoice] = []
    for invoice in invoices:
        if remaining <= 0:
            break
        balance = pricing_service.money(billing_service.totals_for(invoice)["balance"])
        if balance <= 0:
            continue
        part = min(balance, remaining)
        await billing_service.add_payment(
            session,
            tenant_id,
            invoice.id,
            PaymentIn(amount=float(part), method=method, reference=reference),
            actor_id=actor_id,
        )
        remaining -= part
        settled.append(invoice)

    return settled


async def receipt_lines(
    session: AsyncSession, tenant_id: uuid.UUID, invoice_id: uuid.UUID
) -> tuple[Invoice, dict[str, float]]:
    """One invoice and its arithmetic, for the slip handed over the counter."""
    invoice = await billing_service.get_invoice(session, tenant_id, invoice_id)
    return invoice, billing_service.totals_for(invoice)
