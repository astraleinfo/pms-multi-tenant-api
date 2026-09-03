"""§3.4 and §3.13 — the patient register and one patient's whole story."""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import TenantContext, parse_uuid, require_permission
from app.core.permissions import Permission
from app.schemas.clinical import (
    HistoryEventOut,
    PatientIn,
    PatientOut,
    PatientPageOut,
    PatientPatch,
)
from app.services import audit, history
from app.services import patients as patient_service

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
