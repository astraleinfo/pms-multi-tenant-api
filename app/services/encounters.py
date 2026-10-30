"""§3.7 and §3.8 — the consultation and the prescription written from it.

One `Encounter` table serves every discipline; the clinical type only decides
what `clinical_ext` carries and which extra fields the schema resolver returns.
Prescriptions belong to the consultation: Pharmacy reads them, it does not own them.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.clinical import (
    Encounter,
    LabOrder,
    Patient,
    Prescription,
    PrescriptionItem,
    QueueEntry,
)
from app.models.enums import DispenseStatus, EncounterStatus, FieldEntity, InvoiceStatus, QueueStatus
from app.models.ops import InventoryItem, Invoice, InvoiceLineItem
from app.models.tenancy import Tenant
from app.schemas.clinical import (
    EncounterIn,
    EncounterPatch,
    PrescriptionIn,
    PrescriptionItemIn,
    PrescriptionReplaceIn,
)
from app.services import paging
from app.services import tenants as tenant_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


# An ORDER BY is a query plan, not a free-text field: the client names one of
# these and nothing else reaches the database.
ENCOUNTER_SORTS = {
    "date": (Encounter.date,),
    "diagnosis": (Encounter.diagnosis,),
    "status": (Encounter.status,),
    "follow_up": (Encounter.follow_up_days,),
}


async def list_encounters(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    patient_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = paging.ALL,
    offset: int = 0,
    sort: str = "date",
    descending: bool = True,
) -> tuple[list[Encounter], int]:
    """One page of the case book, and how many encounters matched.

    `limit=0` returns them all, for the screens that hold a patient's whole
    history rather than a page of the clinic's.
    """
    stmt = select(Encounter).where(Encounter.tenant_id == tenant_id)
    if patient_id is not None:
        stmt = stmt.where(Encounter.patient_id == patient_id)
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.join(Encounter.patient).where(
            or_(
                Patient.full_name.ilike(like),
                Patient.mrn.ilike(like),
                Encounter.diagnosis.ilike(like),
                Encounter.chief_complaint.ilike(like),
            )
        )

    columns = ENCOUNTER_SORTS.get(sort, ENCOUNTER_SORTS["date"])
    order = [c.desc().nulls_last() if descending else c.asc().nulls_last() for c in columns]
    # The primary key breaks every tie: a day's worth of encounters share a
    # date, and without a total order one of them could appear on two pages.
    stmt = stmt.order_by(*order, Encounter.created_at.desc(), Encounter.id)

    return await paging.page(session, stmt, limit=limit, offset=offset)


async def get_encounter(session: AsyncSession, tenant_id: uuid.UUID, encounter_id: uuid.UUID) -> Encounter:
    row = (
        await session.execute(
            select(Encounter).where(Encounter.id == encounter_id, Encounter.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("No such consultation.")
    return row


async def _close_queue_entry(session: AsyncSession, encounter: Encounter) -> None:
    """Signing ends the visit, so the patient's token leaves the room.

    An encounter opened without its token (from "New encounter" or the
    patient record rather than from the queue) is matched to the patient's
    token still being seen that day; otherwise it would sit "in consultation"
    long after the visit was signed and billed.
    """
    entry: QueueEntry | None = None
    if encounter.queue_entry_id:
        entry = await session.get(QueueEntry, encounter.queue_entry_id)
    else:
        entry = (
            await session.execute(
                select(QueueEntry)
                .where(
                    QueueEntry.tenant_id == encounter.tenant_id,
                    QueueEntry.patient_id == encounter.patient_id,
                    QueueEntry.queue_date == encounter.date,
                    QueueEntry.status.in_((QueueStatus.CALLED, QueueStatus.IN_CONSULTATION)),
                )
                .order_by(QueueEntry.token_no)
                .limit(1)
            )
        ).scalar_one_or_none()
        if entry is not None:
            encounter.queue_entry_id = entry.id
    if entry is not None and entry.status != QueueStatus.COMPLETED:
        entry.status = QueueStatus.COMPLETED
        entry.completed_at = _now()


async def create_encounter(
    session: AsyncSession,
    tenant: Tenant,
    payload: EncounterIn,
    *,
    doctor_id: uuid.UUID,
    today: date,
) -> Encounter:
    patient = (
        await session.execute(
            select(Patient).where(
                Patient.id == uuid.UUID(payload.patient_id), Patient.tenant_id == tenant.id
            )
        )
    ).scalar_one_or_none()
    if patient is None:
        raise NotFound("No such patient.")

    queue_entry: QueueEntry | None = None
    if payload.queue_entry_id:
        queue_entry = (
            await session.execute(
                select(QueueEntry).where(
                    QueueEntry.id == uuid.UUID(payload.queue_entry_id),
                    QueueEntry.tenant_id == tenant.id,
                )
            )
        ).scalar_one_or_none()
        if queue_entry is None:
            raise NotFound("No such queue entry.")
        if queue_entry.patient_id != patient.id:
            raise Conflict("That queue entry belongs to a different patient.")

    schema = await tenant_service.effective_schema(session, tenant, FieldEntity.ENCOUNTER)
    clinical_ext = tenant_service.validate_dynamic_values(
        schema, FieldEntity.ENCOUNTER, payload.clinical_ext, clinical_type=tenant.clinical_type
    )

    encounter = Encounter(
        tenant_id=tenant.id,
        patient_id=patient.id,
        queue_entry_id=queue_entry.id if queue_entry else None,
        doctor_id=uuid.UUID(payload.doctor_id) if payload.doctor_id else doctor_id,
        date=payload.date or today,
        chief_complaint=payload.chief_complaint,
        notes=payload.notes,
        diagnosis=payload.diagnosis,
        follow_up_days=payload.follow_up_days,
        clinical_ext=clinical_ext,
        status=EncounterStatus.SIGNED if payload.sign else EncounterStatus.DRAFT,
        signed_at=_now() if payload.sign else None,
    )
    session.add(encounter)
    await session.flush()
    # A row just added is not a row just queried: `selectin` runs on load, so
    # the patient is fetched explicitly before this is serialised.
    await session.refresh(encounter, ["patient"])

    if payload.sign:
        patient.last_visit = encounter.date
        await _close_queue_entry(session, encounter)

    await session.flush()
    return encounter


#: Only a settled invoice closes the visit. Issued or part-paid, the bill can
#: still be voided and raised again to match a corrected consultation.
BILLED_STATUSES = (InvoiceStatus.PAID,)


async def billed_on(session: AsyncSession, tenant_id: uuid.UUID, encounter: Encounter) -> str | None:
    """The number of a paid invoice that bills this visit, if one does.

    Until then the consultation and its prescription stay open to correction;
    after it, they are what the patient was charged for and are not rewritten.
    An invoice bills the visit if it was raised from it, or if any of its lines
    settles the consultation, one of its prescription lines or one of its lab
    orders — the billing desk raises invoices without naming the encounter.
    """
    rx_items = (
        select(PrescriptionItem.id)
        .join(Prescription, Prescription.id == PrescriptionItem.prescription_id)
        .where(Prescription.tenant_id == tenant_id, Prescription.encounter_id == encounter.id)
    )
    labs = select(LabOrder.id).where(LabOrder.tenant_id == tenant_id, LabOrder.encounter_id == encounter.id)
    settles = select(InvoiceLineItem.invoice_id).where(
        InvoiceLineItem.tenant_id == tenant_id,
        or_(
            InvoiceLineItem.ref_id == encounter.id,
            InvoiceLineItem.ref_id.in_(rx_items),
            InvoiceLineItem.ref_id.in_(labs),
        ),
    )
    return (
        await session.execute(
            select(Invoice.number)
            .where(
                Invoice.tenant_id == tenant_id,
                Invoice.status.in_(BILLED_STATUSES),
                or_(Invoice.encounter_id == encounter.id, Invoice.id.in_(settles)),
            )
            .order_by(Invoice.issued_at, Invoice.number)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _require_unbilled(session: AsyncSession, tenant_id: uuid.UUID, encounter: Encounter) -> None:
    number = await billed_on(session, tenant_id, encounter)
    if number is not None:
        raise Conflict(f"Invoice {number} for this consultation has been paid, so it can no longer be changed.")


async def update_encounter(
    session: AsyncSession, tenant: Tenant, encounter_id: uuid.UUID, payload: EncounterPatch
) -> Encounter:
    encounter = await get_encounter(session, tenant.id, encounter_id)
    await _require_unbilled(session, tenant.id, encounter)

    data = payload.model_dump(exclude_unset=True)
    sign = data.pop("sign", None)

    if data.get("clinical_ext") is not None:
        schema = await tenant_service.effective_schema(session, tenant, FieldEntity.ENCOUNTER)
        merged = {**(encounter.clinical_ext or {}), **data.pop("clinical_ext")}
        encounter.clinical_ext = tenant_service.validate_dynamic_values(
            schema, FieldEntity.ENCOUNTER, merged, clinical_type=tenant.clinical_type
        )
    else:
        data.pop("clinical_ext", None)

    for key, value in data.items():
        setattr(encounter, key, value)

    # Correcting a signed consultation keeps the signature it already has.
    if sign and encounter.status != EncounterStatus.SIGNED:
        encounter.status = EncounterStatus.SIGNED
        encounter.signed_at = _now()
        patient = await session.get(Patient, encounter.patient_id)
        if patient is not None:
            patient.last_visit = encounter.date
        await _close_queue_entry(session, encounter)

    await session.flush()
    return encounter


# --- prescriptions (§3.8) ----------------------------------------------------


PRESCRIPTION_SORTS = {
    "created_at": (Prescription.issued_at,),
    "status": (Prescription.status,),
}


def prescription_status(items: list[PrescriptionItem]) -> DispenseStatus:
    """Where a prescription stands, judged only on the lines this clinic hands
    over. With none of those it is prescribed only: nothing waits on the
    pharmacy, and it never shows at the counter."""
    dispensed_here = [i for i in items if i.dispense]
    if not dispensed_here:
        return DispenseStatus.PRESCRIBE_ONLY
    if all(i.status == DispenseStatus.DISPENSED for i in dispensed_here):
        return DispenseStatus.DISPENSED
    if any(i.dispensed_qty > 0 for i in dispensed_here):
        return DispenseStatus.PARTIALLY_DISPENSED
    return DispenseStatus.PENDING


async def list_prescriptions(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    status: DispenseStatus | None = None,
    patient_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = paging.ALL,
    offset: int = 0,
    sort: str = "created_at",
    descending: bool = True,
) -> tuple[list[Prescription], int, dict[str, int]]:
    """One page of prescriptions, how many matched, and the tab counts.

    The counts are taken *before* the status filter, so "Pending 12" stays true
    while the Dispensed tab is the one on screen.
    """
    stmt = select(Prescription).where(Prescription.tenant_id == tenant_id)
    if patient_id is not None:
        stmt = stmt.where(Prescription.patient_id == patient_id)
    if search:
        like = f"%{search.strip()}%"
        medicines = select(PrescriptionItem.prescription_id).where(
            PrescriptionItem.tenant_id == tenant_id, PrescriptionItem.medicine.ilike(like)
        )
        stmt = stmt.join(Prescription.patient).where(
            or_(
                Patient.full_name.ilike(like),
                Patient.mrn.ilike(like),
                Prescription.id.in_(medicines),
            )
        )

    counts = await paging.counts_by(session, stmt, "status")
    if status is not None:
        stmt = stmt.where(Prescription.status == status)

    columns = PRESCRIPTION_SORTS.get(sort, PRESCRIPTION_SORTS["created_at"])
    order = [c.desc().nulls_last() if descending else c.asc().nulls_last() for c in columns]
    stmt = stmt.order_by(*order, Prescription.id)

    rows, total = await paging.page(session, stmt, limit=limit, offset=offset)
    return rows, total, counts


async def get_prescription(
    session: AsyncSession, tenant_id: uuid.UUID, prescription_id: uuid.UUID
) -> Prescription:
    row = (
        await session.execute(
            select(Prescription).where(
                Prescription.id == prescription_id, Prescription.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("No such prescription.")
    return row


async def get_prescription_for_encounter(
    session: AsyncSession, tenant_id: uuid.UUID, encounter_id: uuid.UUID
) -> Prescription | None:
    """The prescription written from this consultation, if any — for the
    printable record, which omits the section entirely when there is none."""
    return (
        await session.execute(
            select(Prescription).where(
                Prescription.encounter_id == encounter_id, Prescription.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()


async def create_prescription(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    encounter_id: uuid.UUID,
    payload: PrescriptionIn,
    *,
    doctor_id: uuid.UUID,
) -> Prescription:
    encounter = await get_encounter(session, tenant_id, encounter_id)
    await _require_unbilled(session, tenant_id, encounter)

    prescription = Prescription(
        tenant_id=tenant_id,
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        doctor_id=doctor_id,
        issued_at=_now(),
        status=DispenseStatus.PENDING,
    )
    session.add(prescription)
    await session.flush()
    added = await _add_items(session, tenant_id, prescription, payload.items)
    prescription.status = prescription_status(added)

    await session.flush()
    await session.refresh(prescription, ["items", "patient"])
    return prescription


async def replace_prescription(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    encounter_id: uuid.UUID,
    payload: PrescriptionReplaceIn,
    *,
    doctor_id: uuid.UUID,
) -> Prescription | None:
    """Rewrite what this consultation prescribes, before it is billed.

    Creates the prescription if there was none, and removes it if every line
    has been taken off. Once the pharmacy has handed anything over, the stock
    has moved and the prescription is a record of that, so it stays as it is.
    """
    encounter = await get_encounter(session, tenant_id, encounter_id)
    await _require_unbilled(session, tenant_id, encounter)

    existing = await get_prescription_for_encounter(session, tenant_id, encounter.id)
    if existing is None:
        if not payload.items:
            return None
        return await create_prescription(
            session, tenant_id, encounter.id, PrescriptionIn(items=payload.items), doctor_id=doctor_id
        )

    if any(item.dispensed_qty > 0 for item in existing.items):
        raise Conflict("The pharmacy has already dispensed from this prescription, so it can no longer be changed.")

    if not payload.items:
        await session.delete(existing)
        await session.flush()
        return None

    for item in list(existing.items):
        await session.delete(item)
    await session.flush()
    session.expire(existing, ["items"])
    added = await _add_items(session, tenant_id, existing, payload.items)
    existing.status = prescription_status(added)

    await session.flush()
    await session.refresh(existing, ["items", "patient"])
    return existing


async def _add_items(
    session: AsyncSession, tenant_id: uuid.UUID, prescription: Prescription, items: list[PrescriptionItemIn]
) -> list[PrescriptionItem]:
    """Write the lines. A line that does not say whether it is dispensed here
    follows the clinic's default; a stock line sent without a price takes the
    stock line's; a prescribe-only line keeps no price at all."""
    config = await tenant_service.get_config(session, tenant_id)
    added: list[PrescriptionItem] = []
    for position, item in enumerate(items):
        stock: InventoryItem | None = None
        if item.item_id:
            stock = (
                await session.execute(
                    select(InventoryItem).where(
                        InventoryItem.id == uuid.UUID(item.item_id),
                        InventoryItem.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if stock is None:
                raise NotFound("No such stock item.")

        dispense = item.dispense if item.dispense is not None else not config.prescribe_only_default
        unit_price: Decimal | None = None
        if dispense:
            if item.unit_price is not None:
                unit_price = _money(item.unit_price)
            elif stock is not None:
                unit_price = stock.unit_price

        row = PrescriptionItem(
            tenant_id=tenant_id,
            prescription_id=prescription.id,
            item_id=stock.id if stock is not None else None,
            position=position,
            medicine=item.medicine,
            strength=item.strength,
            dosage=item.dosage,
            frequency=item.frequency,
            duration=item.duration,
            instructions=item.instructions,
            qty=item.qty,
            dispensed_qty=0,
            status=DispenseStatus.PENDING if dispense else DispenseStatus.PRESCRIBE_ONLY,
            dispense=dispense,
            unit_price=unit_price,
        )
        session.add(row)
        added.append(row)
    return added


def _money(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
