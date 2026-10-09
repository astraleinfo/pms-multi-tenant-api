"""§3.4 and §3.13 — the patient register and one patient's whole story."""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select

from app.api.deps import TenantContext, parse_uuid, require_permission
from app.core.permissions import Permission, role_has
from app.models.clinical import Appointment, Encounter, LabOrder, Prescription
from app.models.enums import BOOKED_STATUSES, FieldEntity
from app.models.identity import User
from app.schemas.clinical import (
    HistoryEventOut,
    PatientIn,
    PatientNoteIn,
    PatientNoteOut,
    PatientOut,
    PatientPageOut,
    PatientPatch,
)
from app.services import audit, history
from app.services import patient_notes as note_service
from app.services import patients as patient_service
from app.services import tenants as tenant_service
from app.services.pdf import build_patient_record_pdf

router = APIRouter(prefix="/patients", tags=["patients"])

CURRENCY_SYMBOL = {"GBP": "£", "EUR": "€", "USD": "$", "INR": "₹"}


@router.get("", response_model=PatientPageOut)
async def list_patients(
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="registered_at"),
    order: Literal["asc", "desc"] = Query(default="desc"),
    seen_on: date | None = Query(default=None, description="Patients whose last visit was this day."),
    registered_since: date | None = Query(default=None),
    with_allergies: bool | None = Query(default=None),
    ctx: TenantContext = Depends(require_permission(Permission.PATIENT_READ)),
) -> PatientPageOut:
    """One page of the register.

    Searching, sorting and paging all happen in the database, under this
    clinic's row-level policy — the client never receives rows it then throws
    away, and a clinic with fifty thousand patients costs the same as one with
    fifty.

    `tenant_id` is taken from the session, never from the query string.
    """
    rows, total = await patient_service.list_patients(
        ctx.session,
        ctx.tenant_id,
        search=search,
        limit=limit,
        offset=offset,
        sort=sort,
        descending=order == "desc",
        seen_on=seen_on,
        registered_since=registered_since,
        with_allergies=with_allergies,
    )
    return PatientPageOut(
        items=[PatientOut.of(p) for p in rows], total=total, limit=limit, offset=offset
    )


@router.post("", response_model=PatientOut, status_code=status.HTTP_201_CREATED)
async def create_patient(
    payload: PatientIn,
    ctx: TenantContext = Depends(require_permission(Permission.PATIENT_WRITE)),
) -> PatientOut:
    patient = await patient_service.create_patient(ctx.session, ctx.tenant, payload, today=ctx.today)
    await audit.record(
        ctx.session, action="patient.create", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="patient",
        entity_id=str(patient.id),
    )
    return PatientOut.of(patient)


@router.get("/{patient_id}", response_model=PatientOut)
async def get_patient(
    patient_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.PATIENT_READ)),
) -> PatientOut:
    patient = await patient_service.get_patient(
        ctx.session, ctx.tenant_id, parse_uuid(patient_id, what="patient")
    )
    return PatientOut.of(patient)


@router.patch("/{patient_id}", response_model=PatientOut)
async def update_patient(
    patient_id: str,
    payload: PatientPatch,
    ctx: TenantContext = Depends(require_permission(Permission.PATIENT_WRITE)),
) -> PatientOut:
    patient = await patient_service.update_patient(
        ctx.session, ctx.tenant, parse_uuid(patient_id, what="patient"), payload
    )
    await audit.record(
        ctx.session, action="patient.update", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="patient",
        entity_id=str(patient.id),
    )
    return PatientOut.of(patient)


@router.get("/{patient_id}/history", response_model=list[HistoryEventOut])
async def patient_history(
    patient_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.PATIENT_READ)),
) -> list[HistoryEventOut]:
    patient = await patient_service.get_patient(
        ctx.session, ctx.tenant_id, parse_uuid(patient_id, what="patient")
    )
    return await history.patient_history(
        ctx.session,
        ctx.tenant_id,
        patient,
        tz=ctx.tz,
        currency_symbol=CURRENCY_SYMBOL.get(str(ctx.config.currency), "₹"),
    )


# --- notes --------------------------------------------------------------------
# Clinical notes are read and written under the consultation's permissions: the
# same people who may read and record an encounter may read and write a note.


def _author(ctx: TenantContext) -> str:
    """The name the note is signed with — never an email address, and platform
    support named as itself rather than as the clinic account it is using."""
    staff = ctx.principal.platform_user
    return f"{staff.name} (platform support)" if staff is not None else ctx.user.name


@router.get("/{patient_id}/notes", response_model=list[PatientNoteOut])
async def list_patient_notes(
    patient_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.ENCOUNTER_READ)),
) -> list[PatientNoteOut]:
    """Every note on this patient, newest first — corrections included, so the
    record shows what was said as well as what it was corrected to."""
    notes = await note_service.list_notes(
        ctx.session, ctx.tenant_id, parse_uuid(patient_id, what="patient")
    )
    return [PatientNoteOut.of(n, ctx.tz) for n in notes]


@router.post("/{patient_id}/notes", response_model=PatientNoteOut, status_code=status.HTTP_201_CREATED)
async def add_patient_note(
    patient_id: str,
    payload: PatientNoteIn,
    ctx: TenantContext = Depends(require_permission(Permission.ENCOUNTER_WRITE)),
) -> PatientNoteOut:
    """Add a note. There is no edit and no delete: a saved note is corrected by
    writing another that names it in `amends_id`."""
    note = await note_service.add_note(
        ctx.session, ctx.tenant_id, parse_uuid(patient_id, what="patient"), payload,
        author=ctx.user, author_label=_author(ctx),
    )
    await audit.record(
        ctx.session, action="patient_note.correct" if note.amends_id else "patient_note.add",
        tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind, actor_id=ctx.actor_id,
        actor_label=ctx.principal.label, entity="patient", entity_id=str(note.patient_id),
        meta={"note_id": str(note.id), **({"amends": str(note.amends_id)} if note.amends_id else {})},
    )
    return PatientNoteOut.of(note, ctx.tz)


#: the patient's own columns, printed by the record itself — the schema lists them too
_PRINTED_AS_COLUMNS = {
    "full_name", "dob", "sex", "phone", "email", "address", "blood_group", "allergies", "weight",
}


@router.get("/{patient_id}/record.pdf")
async def patient_record_pdf(
    patient_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.PATIENT_READ)),
) -> Response:
    """The patient's file as a printable PDF, on the clinic's letterhead.

    A clinical record, so billing is not on it. Lab orders are printed only for
    a role that may read them — left out on the server, so a role without them
    is never sent what it may not see. `inline`, so a print button opens it in a tab.
    """
    patient = await patient_service.get_patient(
        ctx.session, ctx.tenant_id, parse_uuid(patient_id, what="patient")
    )
    tid, pid = ctx.tenant_id, patient.id

    async def rows(model, *order):  # noqa: ANN001, ANN202 - a local query helper
        stmt = select(model).where(model.tenant_id == tid, model.patient_id == pid).order_by(*order)
        return list((await ctx.session.execute(stmt)).scalars())

    names = {
        u.id: u.name
        for u in (await ctx.session.execute(select(User).where(User.tenant_id == tid))).scalars()
    }
    schema = await tenant_service.effective_schema(ctx.session, ctx.tenant, FieldEntity.PATIENT)
    ext = patient.clinical_ext or {}
    fields = [
        (d.label, ext[d.field_key])
        for d in schema
        if d.field_key not in _PRINTED_AS_COLUMNS
        and ext.get(d.field_key) not in (None, "", [])
        and not isinstance(ext.get(d.field_key), dict)
    ]
    upcoming = [
        a for a in await rows(Appointment, Appointment.date, Appointment.time)
        if a.date >= ctx.today and a.status in BOOKED_STATUSES
    ]
    role = ctx.user.role
    content = build_patient_record_pdf(
        patient,
        clinic=ctx.tenant.name,
        config=ctx.config,
        logo=await tenant_service.logo_for(ctx.session, tid),
        fields=fields,
        encounters=await rows(Encounter, Encounter.date.desc()),
        prescriptions=await rows(Prescription, Prescription.issued_at.desc()),
        lab_orders=(
            await rows(LabOrder, LabOrder.ordered_at.desc())
            if role_has(role, Permission.LAB_READ) else None
        ),
        # Notes go on paper only for a role that may read them on screen.
        notes=(
            await note_service.list_notes(ctx.session, tid, pid)
            if role_has(role, Permission.ENCOUNTER_READ) else None
        ),
        appointments=upcoming,
        names=names,
        today=ctx.today,
        tz=ctx.tz,
        printed_by=ctx.principal.label,
    )
    safe = "".join(ch for ch in patient.mrn if ch.isalnum() or ch in "-_") or "patient"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="patient-record-{safe}.pdf"',
            "Cache-Control": "private, no-store",
        },
    )
