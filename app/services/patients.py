"""§3.4 — patient registration and search.

`tenant_id` is stamped from the authenticated session. A `tenant_id` in the
request body is rejected outright by the schema (`extra="forbid"`), and even if
one arrived it would never be read.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.models.clinical import Patient
from app.models.enums import FieldEntity
from app.models.tenancy import Tenant
from app.schemas.clinical import PatientIn, PatientPatch
from app.services import tenants as tenant_service


def _search(stmt: Select, term: str) -> Select:
    like = f"%{term.strip()}%"
    return stmt.where(
        or_(Patient.full_name.ilike(like), Patient.mrn.ilike(like), Patient.phone.ilike(like))
    )


# Only these can be sorted on, and the client names one rather than sending an
# expression — an ORDER BY is a query plan, not a free-text field.
SORTABLE = {
    "full_name": Patient.full_name,
    "mrn": Patient.mrn,
    "dob": Patient.dob,
    "registered_at": Patient.registered_at,
    "last_visit": Patient.last_visit,
}


async def list_patients(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    search: str | None = None,
    limit: int = 25,
    offset: int = 0,
    sort: str = "registered_at",
    descending: bool = True,
    seen_on: date | None = None,
    registered_since: date | None = None,
    with_allergies: bool | None = None,
) -> tuple[list[Patient], int]:
    """One page of the register, and how many rows the query matched.

    The count runs under the same tenant filter and the same policy as the page
    itself, so a total can never describe rows the caller may not read.
    """
    column = SORTABLE.get(sort, Patient.registered_at)
    order = column.desc().nulls_last() if descending else column.asc().nulls_last()

    base = select(Patient).where(Patient.tenant_id == tenant_id)
    if search:
        base = _search(base, search)
    if seen_on is not None:
        base = base.where(Patient.last_visit == seen_on)
    if registered_since is not None:
        base = base.where(Patient.registered_at >= registered_since)
    if with_allergies:
        # "None recorded" is what the register writes when there are none.
        base = base.where(
            Patient.allergies.isnot(None),
            func.lower(Patient.allergies).notlike("none%"),
        )

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0

    rows = await session.execute(
        base.order_by(order, Patient.full_name, Patient.id).limit(limit).offset(offset)
    )
    return list(rows.scalars()), int(total)


async def get_patient(session: AsyncSession, tenant_id: uuid.UUID, patient_id: uuid.UUID) -> Patient:
    """A patient in another clinic is reported as *not found* — a 403 would
    confirm the row exists somewhere."""
    row = (
        await session.execute(
            select(Patient).where(Patient.id == patient_id, Patient.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("No such patient.")
    return row


async def create_patient(
    session: AsyncSession, tenant: Tenant, payload: PatientIn, *, today: date
) -> Patient:
    schema = await tenant_service.effective_schema(session, tenant, FieldEntity.PATIENT)
    clinical_ext = tenant_service.validate_dynamic_values(
        schema, FieldEntity.PATIENT, payload.clinical_ext
    )
    mrn = await tenant_service.next_mrn(session, tenant.id)

    patient = Patient(
        tenant_id=tenant.id,
        mrn=mrn,
        full_name=payload.full_name,
        dob=payload.dob,
        sex=payload.sex,
        phone=payload.phone,
        email=payload.email or None,
        address=payload.address or None,
        blood_group=payload.blood_group or None,
        allergies=payload.allergies or "None recorded",
        registered_at=today,
        last_visit=None,
        clinical_ext=clinical_ext,
        tags=["First visit"],
    )
    session.add(patient)
    await session.flush()
    return patient


async def update_patient(
    session: AsyncSession, tenant: Tenant, patient_id: uuid.UUID, payload: PatientPatch
) -> Patient:
    patient = await get_patient(session, tenant.id, patient_id)
    data = payload.model_dump(exclude_unset=True)

    if "clinical_ext" in data and data["clinical_ext"] is not None:
        schema = await tenant_service.effective_schema(session, tenant, FieldEntity.PATIENT)
        merged = {**(patient.clinical_ext or {}), **data.pop("clinical_ext")}
        patient.clinical_ext = tenant_service.validate_dynamic_values(
            schema, FieldEntity.PATIENT, merged
        )
    else:
        data.pop("clinical_ext", None)

    for key, value in data.items():
        setattr(patient, key, value)

    await session.flush()
    return patient
