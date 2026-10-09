"""§3.7 and §3.8 — the consultation and the prescription written from it.

One `Encounter` table serves every discipline; the clinical type only decides
what `clinical_ext` carries and which extra fields the schema resolver returns.
Prescriptions belong to the consultation: Pharmacy reads them, it does not own them.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.clinical import Encounter, Patient, Prescription, PrescriptionItem, QueueEntry
from app.models.enums import DispenseStatus, EncounterStatus, FieldEntity, QueueStatus
from app.models.ops import InventoryItem
from app.models.tenancy import Tenant
from app.schemas.clinical import EncounterIn, EncounterPatch, PrescriptionIn
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
        if queue_entry is not None and queue_entry.status != QueueStatus.COMPLETED:
            queue_entry.status = QueueStatus.COMPLETED
            queue_entry.completed_at = _now()

    await session.flush()
    return encounter


async def update_encounter(
    session: AsyncSession, tenant: Tenant, encounter_id: uuid.UUID, payload: EncounterPatch
) -> Encounter:
    encounter = await get_encounter(session, tenant.id, encounter_id)
    if encounter.status == EncounterStatus.SIGNED:
        raise Conflict("A signed consultation cannot be edited.")

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

    if sign:
        encounter.status = EncounterStatus.SIGNED
        encounter.signed_at = _now()
        patient = await session.get(Patient, encounter.patient_id)
        if patient is not None:
            patient.last_visit = encounter.date
        if encounter.queue_entry_id:
            entry = await session.get(QueueEntry, encounter.queue_entry_id)
            if entry is not None and entry.status != QueueStatus.COMPLETED:
                entry.status = QueueStatus.COMPLETED
                entry.completed_at = _now()

    await session.flush()
    return encounter


# --- prescriptions (§3.8) ----------------------------------------------------


PRESCRIPTION_SORTS = {
    "created_at": (Prescription.issued_at,),
    "status": (Prescription.status,),
}


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

    for position, item in enumerate(payload.items):
        stock_item_id: uuid.UUID | None = None
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
            stock_item_id = stock.id

        session.add(
            PrescriptionItem(
                tenant_id=tenant_id,
                prescription_id=prescription.id,
                item_id=stock_item_id,
                position=position,
                medicine=item.medicine,
                strength=item.strength,
                dosage=item.dosage,
                frequency=item.frequency,
                duration=item.duration,
                instructions=item.instructions,
                qty=item.qty,
                dispensed_qty=0,
                status=DispenseStatus.PENDING,
            )
        )

    await session.flush()
    await session.refresh(prescription, ["items", "patient"])
    return prescription
