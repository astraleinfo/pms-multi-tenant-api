"""Patient notes — what clinicians write about a patient between and beyond visits.

A note is part of the clinical record, and a record is added to, never
rewritten: there is no edit and no delete here. A mistake is put right by a
correction — a new note that points at the one it corrects — written by the
same person, so the record shows both what was said and that it was taken back.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, PermissionDenied
from app.models.clinical import PatientNote
from app.models.identity import User
from app.schemas.clinical import PatientNoteIn
from app.services import patients as patient_service


async def list_notes(
    session: AsyncSession, tenant_id: uuid.UUID, patient_id: uuid.UUID
) -> list[PatientNote]:
    """Every note on one patient, newest first."""
    await patient_service.get_patient(session, tenant_id, patient_id)
    rows = await session.execute(
        select(PatientNote)
        .where(PatientNote.tenant_id == tenant_id, PatientNote.patient_id == patient_id)
        .order_by(PatientNote.created_at.desc(), PatientNote.id.desc())
    )
    return list(rows.scalars())


async def add_note(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    patient_id: uuid.UUID,
    payload: PatientNoteIn,
    *,
    author: User,
    author_label: str,
) -> PatientNote:
    patient = await patient_service.get_patient(session, tenant_id, patient_id)

    if payload.amends_id is not None:
        earlier = (
            await session.execute(
                select(PatientNote).where(
                    PatientNote.tenant_id == tenant_id,
                    PatientNote.id == uuid.UUID(payload.amends_id),
                    PatientNote.patient_id == patient.id,
                )
            )
        ).scalar_one_or_none()
        if earlier is None:
            raise NotFound("No such note on this patient.")
        # A correction speaks for the person who wrote the original. Anyone
        # else with something to add writes a note of their own.
        if earlier.author_id != author.id:
            raise PermissionDenied("Only the note's author can correct it. Add a note of your own instead.")

    note = PatientNote(
        tenant_id=tenant_id,
        patient_id=patient.id,
        body=payload.body,
        author_id=author.id,
        author_label=author_label[:160],
        author_role=author.role,
        amends_id=uuid.UUID(payload.amends_id) if payload.amends_id else None,
    )
    session.add(note)
    await session.flush()
    await session.refresh(note)
    return note
