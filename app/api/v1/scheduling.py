"""§3.5 and §3.6 — appointments, check-in, the queue, and the waiting-room
display."""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status

from app.api.deps import TenantContext, current_tenant, parse_uuid, require_permission
from app.core.errors import NotFound
from app.core.events import broker
from app.core.permissions import Permission
from app.models.clinical import Patient
from app.models.identity import User
from app.schemas.clinical import (
    AppointmentIn,
    AppointmentOut,
    AppointmentPageOut,
    AppointmentStatusIn,
    AvailabilityOut,
    DisplayOut,
    DisplayRowOut,
    QueueEntryOut,
    QueueStatusIn,
    WalkInIn,
)
from app.services import audit
from app.services import scheduling as scheduling_service

router = APIRouter(tags=["scheduling"])


def announce(tasks: BackgroundTasks, ctx: TenantContext, entry, event: str) -> None:
    """Tell this clinic's open screens that the queue moved.

    Queued as a background task, so it runs *after* the request transaction has
    committed: nothing is announced that could still be rolled back. The payload
    is a hint, not a copy — clients re-read `GET /v1/queue`, so there is only
    ever one description of the queue.
    """
    tasks.add_task(
        broker.publish,
        ctx.tenant_id,
        event,
        {
            "entry_id": str(entry.id),
            "token_no": entry.token_no,
            "status": str(entry.status),
            "queue_date": entry.queue_date.isoformat(),
        },
    )


@router.get("/appointments", response_model=AppointmentPageOut)
async def list_appointments(
    day: date | None = Query(default=None, alias="date"),
    doctor_id: str | None = None,
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=0, ge=0, le=200, description="0 asks for the whole collection."),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="when"),
    order: Literal["asc", "desc"] = Query(default="asc"),
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_READ)),
) -> AppointmentPageOut:
    """One page of the diary — searched, sorted and sliced in the database.

    The day and week grids are not pages and ask for `limit=0`; the list view
    asks for ten rows and gets ten rows.
    """
    rows, total = await scheduling_service.list_appointments(
        ctx.session,
        ctx.tenant_id,
        day=day,
        doctor_id=parse_uuid(doctor_id, what="clinician") if doctor_id else None,
        search=search,
        limit=limit,
        offset=offset,
        sort=sort,
        descending=order == "desc",
    )
    return AppointmentPageOut(
        items=[AppointmentOut.of(a) for a in rows], total=total, limit=limit, offset=offset
    )


@router.get("/appointments/availability", response_model=AvailabilityOut)
async def availability(
    doctor_id: str,
    day: date = Query(alias="date"),
    time: str = Query(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$"),
    duration_min: int = Query(default=30, ge=5, le=480),
    ignore_id: str | None = None,
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_READ)),
) -> AvailabilityOut:
    """The check behind the booking dialog: overlap against this clinician's
    existing bookings, in this clinic only."""
    conflicts = await scheduling_service.conflicts_for(
        ctx.session,
        ctx.tenant_id,
        doctor_id=parse_uuid(doctor_id, what="clinician"),
        day=day,
        time=time,
        duration_min=duration_min,
        ignore_id=parse_uuid(ignore_id, what="appointment") if ignore_id else None,
    )
    return AvailabilityOut(
        available=not conflicts, conflicts=[AppointmentOut.of(a) for a in conflicts]
    )


@router.post("/appointments", response_model=AppointmentOut, status_code=status.HTTP_201_CREATED)
async def create_appointment(
    payload: AppointmentIn,
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_WRITE)),
) -> AppointmentOut:
    appointment = await scheduling_service.create_appointment(ctx.session, ctx.tenant_id, payload)
    await audit.record(
        ctx.session, action="appointment.create", tenant_id=ctx.tenant_id, actor_kind="user",
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="appointment",
        entity_id=str(appointment.id),
    )
    return AppointmentOut.of(appointment)


@router.patch("/appointments/{appointment_id}/status", response_model=AppointmentOut)
async def set_appointment_status(
    appointment_id: str,
    payload: AppointmentStatusIn,
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_WRITE)),
) -> AppointmentOut:
    appointment = await scheduling_service.set_appointment_status(
        ctx.session, ctx.tenant_id, parse_uuid(appointment_id, what="appointment"), payload.status
    )
    await audit.record(
        ctx.session, action=f"appointment.{payload.status.value}", tenant_id=ctx.tenant_id,
        actor_kind="user", actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="appointment", entity_id=str(appointment.id),
    )
    return AppointmentOut.of(appointment)


@router.post("/appointments/{appointment_id}/checkin", response_model=QueueEntryOut)
async def check_in(
    appointment_id: str,
    tasks: BackgroundTasks,
    ctx: TenantContext = Depends(require_permission(Permission.QUEUE_WRITE)),
) -> QueueEntryOut:
    """Issues the next token for this clinic, today (§3.6)."""
    entry = await scheduling_service.check_in(
        ctx.session, ctx.tenant_id, parse_uuid(appointment_id, what="appointment"), today=ctx.today
    )
    await audit.record(
        ctx.session, action="queue.checkin", tenant_id=ctx.tenant_id, actor_kind="user",
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="queue_entry",
        entity_id=str(entry.id), meta={"token_no": entry.token_no},
    )
    announce(tasks, ctx, entry, "queue.checked_in")
    return QueueEntryOut.of(entry, ctx.tz)


@router.get("/queue", response_model=list[QueueEntryOut])
async def list_queue(
    day: date | None = Query(default=None, alias="date"),
    ctx: TenantContext = Depends(require_permission(Permission.QUEUE_READ)),
) -> list[QueueEntryOut]:
    rows = await scheduling_service.list_queue(ctx.session, ctx.tenant_id, day=day)
    return [QueueEntryOut.of(q, ctx.tz) for q in rows]


@router.post("/queue/walkin", response_model=QueueEntryOut, status_code=status.HTTP_201_CREATED)
async def add_walk_in(
    payload: WalkInIn,
    tasks: BackgroundTasks,
    ctx: TenantContext = Depends(require_permission(Permission.QUEUE_WRITE)),
) -> QueueEntryOut:
    entry = await scheduling_service.add_walk_in(ctx.session, ctx.tenant_id, payload, today=ctx.today)
    await audit.record(
        ctx.session, action="queue.walkin", tenant_id=ctx.tenant_id, actor_kind="user",
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="queue_entry",
        entity_id=str(entry.id), meta={"token_no": entry.token_no},
    )
    announce(tasks, ctx, entry, "queue.walk_in")
    return QueueEntryOut.of(entry, ctx.tz)


@router.patch("/queue/{entry_id}/status", response_model=QueueEntryOut)
async def set_queue_status(
    entry_id: str,
    payload: QueueStatusIn,
    tasks: BackgroundTasks,
    ctx: TenantContext = Depends(require_permission(Permission.QUEUE_WRITE)),
) -> QueueEntryOut:
    entry = await scheduling_service.set_queue_status(
        ctx.session, ctx.tenant_id, parse_uuid(entry_id, what="queue entry"), payload.status
    )
    announce(tasks, ctx, entry, "queue.status_changed")
    return QueueEntryOut.of(entry, ctx.tz)


@router.get("/queue/display", response_model=DisplayOut)
async def waiting_room_display(ctx: TenantContext = Depends(current_tenant)) -> DisplayOut:
    """The screen in the waiting room.

    Still behind a session — the screen signs in once — but it is given a
    first name and an initial and nothing else, because the room can read it.
    """
    entries = await scheduling_service.list_queue(ctx.session, ctx.tenant_id, day=ctx.today)
    serving, waiting = scheduling_service.ordered_for_display(
        entries, urgent_first=ctx.config.urgent_first
    )

    patient_ids = {e.patient_id for e in serving + waiting}
    doctor_ids = {e.doctor_id for e in serving + waiting}
    names: dict = {}
    doctors: dict = {}
    if patient_ids:
        from sqlalchemy import select

        rows = (
            await ctx.session.execute(
                select(Patient.id, Patient.full_name).where(
                    Patient.tenant_id == ctx.tenant_id, Patient.id.in_(patient_ids)
                )
            )
        ).all()
        names = {pid: full for pid, full in rows}
        rows = (
            await ctx.session.execute(
                select(User.id, User.name).where(
                    User.tenant_id == ctx.tenant_id, User.id.in_(doctor_ids)
                )
            )
        ).all()
        doctors = {uid: name for uid, name in rows}

    def row(entry) -> DisplayRowOut:
        return DisplayRowOut(
            token_no=entry.token_no,
            status=entry.status,
            display_name=scheduling_service.short_name(names.get(entry.patient_id, "")),
            doctor=doctors.get(entry.doctor_id, "—"),
            priority=entry.priority,
        )

    return DisplayOut(
        tenant=ctx.tenant.name,
        now_serving=[row(e) for e in serving],
        waiting=[row(e) for e in waiting],
    )


@router.get("/queue/{entry_id}", response_model=QueueEntryOut)
async def get_queue_entry(
    entry_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.QUEUE_READ)),
) -> QueueEntryOut:
    entry = await scheduling_service.get_queue_entry(
        ctx.session, ctx.tenant_id, parse_uuid(entry_id, what="queue entry")
    )
    if entry is None:
        raise NotFound("No such queue entry.")
    return QueueEntryOut.of(entry, ctx.tz)
