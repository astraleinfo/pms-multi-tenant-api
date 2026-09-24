"""§3.7 and §3.8 — consultations and the prescriptions written from them."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status

from app.api.deps import TenantContext, parse_uuid, require_feature, require_permission
from app.core.events import broker
from app.core.permissions import Permission
from app.models.enums import DispenseStatus, FieldEntity
from app.models.identity import User
from app.schemas.clinical import (
    EncounterIn,
    EncounterOut,
    EncounterPageOut,
    EncounterPatch,
    LabOrderIn,
    LabOrderOut,
    PrescriptionIn,
    PrescriptionOut,
    PrescriptionPageOut,
)
from app.schemas.tenancy import FieldDefinitionOut
from app.services import audit
from app.services import encounters as encounter_service
from app.services import lab as lab_service
from app.services import tenants as tenant_service
from app.services.pdf import build_consultation_pdf

router = APIRouter(prefix="/encounters", tags=["consultation"])


@router.get("/schema", response_model=list[FieldDefinitionOut])
async def encounter_schema(
    ctx: TenantContext = Depends(require_permission(Permission.ENCOUNTER_READ)),
) -> list[FieldDefinitionOut]:
    """Common plus this clinic's clinical-type fields — the clinical type comes
    from the tenant record, never from the query string."""
    rows = await tenant_service.effective_schema(ctx.session, ctx.tenant, FieldEntity.ENCOUNTER)
    return [FieldDefinitionOut.of(r) for r in rows]


@router.get("", response_model=EncounterPageOut)
async def list_encounters(
    patient_id: str | None = None,
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=0, ge=0, le=200, description="0 asks for the whole collection."),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="date"),
    order: Literal["asc", "desc"] = Query(default="desc"),
    ctx: TenantContext = Depends(require_permission(Permission.ENCOUNTER_READ)),
) -> EncounterPageOut:
    """One page of the case book — searching, sorting and paging all happen in
    the database, so the consultations screen never holds the clinic's history
    in order to show ten rows of it."""
    rows, total = await encounter_service.list_encounters(
        ctx.session,
        ctx.tenant_id,
        patient_id=parse_uuid(patient_id, what="patient") if patient_id else None,
        search=search,
        limit=limit,
        offset=offset,
        sort=sort,
        descending=order == "desc",
    )
    return EncounterPageOut(
        items=[EncounterOut.of(e) for e in rows], total=total, limit=limit, offset=offset
    )


@router.post("", response_model=EncounterOut, status_code=status.HTTP_201_CREATED)
async def create_encounter(
    payload: EncounterIn,
    tasks: BackgroundTasks,
    ctx: TenantContext = Depends(require_permission(Permission.ENCOUNTER_WRITE)),
) -> EncounterOut:
    encounter = await encounter_service.create_encounter(
        ctx.session, ctx.tenant, payload, doctor_id=ctx.actor_id, today=ctx.today
    )
    await audit.record(
        ctx.session, action="encounter.create", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="encounter",
        entity_id=str(encounter.id), meta={"signed": payload.sign},
    )
    if encounter.queue_entry_id and payload.sign:
        # Signing completes the queue entry, so the waiting room moves on.
        tasks.add_task(
            broker.publish,
            ctx.tenant_id,
            "queue.status_changed",
            {"entry_id": str(encounter.queue_entry_id), "status": "completed"},
        )
    return EncounterOut.of(encounter)


@router.get("/{encounter_id}", response_model=EncounterOut)
async def get_encounter(
    encounter_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.ENCOUNTER_READ)),
) -> EncounterOut:
    encounter = await encounter_service.get_encounter(
        ctx.session, ctx.tenant_id, parse_uuid(encounter_id, what="consultation")
    )
    return EncounterOut.of(encounter)


@router.patch("/{encounter_id}", response_model=EncounterOut)
async def update_encounter(
    encounter_id: str,
    payload: EncounterPatch,
    tasks: BackgroundTasks,
    ctx: TenantContext = Depends(require_permission(Permission.ENCOUNTER_WRITE)),
) -> EncounterOut:
    encounter = await encounter_service.update_encounter(
        ctx.session, ctx.tenant, parse_uuid(encounter_id, what="consultation"), payload
    )
    await audit.record(
        ctx.session, action="encounter.update", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="encounter",
        entity_id=str(encounter.id),
    )
    if encounter.queue_entry_id and payload.sign:
        tasks.add_task(
            broker.publish,
            ctx.tenant_id,
            "queue.status_changed",
            {"entry_id": str(encounter.queue_entry_id), "status": "completed"},
        )
    return EncounterOut.of(encounter)


@router.get("/{encounter_id}/consultation-pdf")
async def consultation_pdf(
    encounter_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.ENCOUNTER_READ)),
) -> Response:
    """The printable consultation record — the encounter, its prescription (if
    any) and the patient, rendered as a PDF. `inline` so it opens in a tab
    rather than forcing a download, which is what a print button wants."""
    eid = parse_uuid(encounter_id, what="consultation")
    encounter = await encounter_service.get_encounter(ctx.session, ctx.tenant_id, eid)
    prescription = await encounter_service.get_prescription_for_encounter(ctx.session, ctx.tenant_id, eid)
    doctor = await ctx.session.get(User, encounter.doctor_id)
    pdf_bytes = build_consultation_pdf(
        encounter,
        prescription,
        encounter.patient,
        tenant_name=ctx.tenant.name,
        doctor_name=doctor.name if doctor else "",
        tz=ctx.tz,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="consultation-{encounter_id}.pdf"'},
    )


# --- prescriptions (core, §3.8) ---------------------------------------------


@router.post(
    "/{encounter_id}/prescriptions",
    response_model=PrescriptionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_prescription(
    encounter_id: str,
    payload: PrescriptionIn,
    ctx: TenantContext = Depends(require_permission(Permission.PRESCRIPTION_WRITE)),
) -> PrescriptionOut:
    """Always available — prescribing does not depend on owning a pharmacy."""
    prescription = await encounter_service.create_prescription(
        ctx.session,
        ctx.tenant_id,
        parse_uuid(encounter_id, what="consultation"),
        payload,
        doctor_id=ctx.actor_id,
    )
    await audit.record(
        ctx.session, action="prescription.create", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="prescription",
        entity_id=str(prescription.id),
    )
    return PrescriptionOut.of(prescription, ctx.tz)


# --- lab orders from a consultation (§3.10, feature-gated) -------------------


@router.post("/{encounter_id}/lab-orders", response_model=list[LabOrderOut], status_code=201)
async def order_labs(
    encounter_id: str,
    payload: LabOrderIn,
    ctx: TenantContext = Depends(require_feature("lab", Permission.LAB_ORDER)),
) -> list[LabOrderOut]:
    orders = await lab_service.create_orders(
        ctx.session,
        ctx.tenant_id,
        parse_uuid(encounter_id, what="consultation"),
        payload,
        ordered_by=ctx.actor_id,
    )
    await audit.record(
        ctx.session, action="lab.order", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="encounter",
        entity_id=encounter_id, meta={"tests": [o.test_code for o in orders]},
    )
    return [LabOrderOut.of(o, ctx.tz) for o in orders]


@router.get("/{encounter_id}/lab-results", response_model=list[LabOrderOut])
async def encounter_lab_results(
    encounter_id: str,
    include_unverified: bool = Query(default=False),
    ctx: TenantContext = Depends(require_feature("lab", Permission.LAB_READ)),
) -> list[LabOrderOut]:
    """Verified results only by default — that is what "verified" is for."""
    orders = await lab_service.results_for_encounter(
        ctx.session,
        ctx.tenant_id,
        parse_uuid(encounter_id, what="consultation"),
        verified_only=not include_unverified,
    )
    return [LabOrderOut.of(o, ctx.tz, redact_unverified=True) for o in orders]


# --- prescription list -------------------------------------------------------

prescriptions_router = APIRouter(prefix="/prescriptions", tags=["consultation"])


@prescriptions_router.get("", response_model=PrescriptionPageOut)
async def list_prescriptions(
    status_filter: DispenseStatus | None = Query(default=None, alias="status"),
    patient_id: str | None = None,
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=0, ge=0, le=200, description="0 asks for the whole collection."),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="created_at"),
    order: Literal["asc", "desc"] = Query(default="desc"),
    ctx: TenantContext = Depends(require_permission(Permission.PRESCRIPTION_READ)),
) -> PrescriptionPageOut:
    """One page of prescriptions, plus the counts behind the tabs — which are
    counted in the database rather than by tallying rows the client happens to
    be holding."""
    rows, total, counts = await encounter_service.list_prescriptions(
        ctx.session,
        ctx.tenant_id,
        status=status_filter,
        patient_id=parse_uuid(patient_id, what="patient") if patient_id else None,
        search=search,
        limit=limit,
        offset=offset,
        sort=sort,
        descending=order == "desc",
    )
    return PrescriptionPageOut(
        items=[PrescriptionOut.of(rx, ctx.tz) for rx in rows],
        total=total,
        limit=limit,
        offset=offset,
        counts=counts,
    )


@prescriptions_router.get("/{prescription_id}", response_model=PrescriptionOut)
async def get_prescription(
    prescription_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.PRESCRIPTION_READ)),
) -> PrescriptionOut:
    rx = await encounter_service.get_prescription(
        ctx.session, ctx.tenant_id, parse_uuid(prescription_id, what="prescription")
    )
    return PrescriptionOut.of(rx, ctx.tz)
