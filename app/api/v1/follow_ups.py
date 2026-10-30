"""Follow-up calls — the front desk ringing patients about upcoming bookings.

Reading the list is reading the diary (`appointment:read`); logging a call can
change a booking, so it is writing to it (`appointment:write`). Reception,
clinicians and the practice admin hold both; nobody needs a new permission to
use this.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import TenantContext, parse_uuid, require_permission
from app.api.v1.scheduling import announce_cancellation, announce_move
from app.core.permissions import Permission
from app.models.enums import FollowUpOutcome, FollowUpStatus
from app.schemas.clinical import (
    AppointmentOut,
    FollowUpCallIn,
    FollowUpCallOut,
    FollowUpOut,
    FollowUpPageOut,
)
from app.services import audit
from app.services import follow_ups as follow_up_service
from app.services.messaging import service as message_service

router = APIRouter(tags=["follow-ups"])


def _out(row: dict, tz: str) -> FollowUpOut:
    last = row["last_call"]
    return FollowUpOut(
        appointment=AppointmentOut.of(row["appointment"]),
        patient_phone=row["patient_phone"] or "",
        status=row["status"],
        attempts=row["attempts"],
        last_call=FollowUpCallOut.of(last, tz) if last is not None else None,
    )


def _caller(ctx: TenantContext) -> str:
    """Who rang, as the history shows it: a name, not an email address — and
    platform support named as itself, never as the clinic account it is using."""
    staff = ctx.principal.platform_user
    if staff is not None:
        return f"{staff.name} (platform support)"
    return ctx.user.name


@router.get("/follow-ups", response_model=FollowUpPageOut)
async def list_follow_ups(
    days: int = Query(default=follow_up_service.DEFAULT_DAYS, ge=1, le=follow_up_service.MAX_DAYS),
    day: date | None = Query(default=None, alias="date"),
    status_filter: FollowUpStatus | None = Query(default=None, alias="status"),
    doctor_id: str | None = None,
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=25, ge=0, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_READ)),
) -> FollowUpPageOut:
    """The call list: bookings from today to `days` ahead, in diary order."""
    rows, total, counts, today = await follow_up_service.list_follow_ups(
        ctx.session,
        ctx.tenant_id,
        today=ctx.today,
        days=days,
        day=day,
        status=status_filter,
        doctor_id=parse_uuid(doctor_id, what="clinician") if doctor_id else None,
        search=search,
        limit=limit,
        offset=offset,
    )
    return FollowUpPageOut(
        items=[_out(r, ctx.tz) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
        counts={s.value: counts.get(s.value, 0) for s in FollowUpStatus},
        today=today,
    )


@router.get("/follow-ups/{appointment_id}/calls", response_model=list[FollowUpCallOut])
async def list_calls(
    appointment_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_READ)),
) -> list[FollowUpCallOut]:
    """Every call about one booking, newest first — who rang, when, and what came of it."""
    calls = await follow_up_service.list_calls(
        ctx.session, ctx.tenant_id, parse_uuid(appointment_id, what="appointment")
    )
    return [FollowUpCallOut.of(c, ctx.tz) for c in calls]


@router.post(
    "/follow-ups/{appointment_id}/calls",
    response_model=FollowUpOut,
    status_code=status.HTTP_201_CREATED,
)
async def record_call(
    appointment_id: str,
    payload: FollowUpCallIn,
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_WRITE)),
) -> FollowUpOut:
    """Log a call and act on it: confirm the booking, move it, or let it go.

    A move is checked against the clinician's diary under the same lock as any
    other booking, so it answers 409 if the slot was taken while the patient
    was deciding — and logs nothing, so the desk can offer another.
    """
    call, appointment, was = await follow_up_service.record_call(
        ctx.session,
        ctx.tenant_id,
        parse_uuid(appointment_id, what="appointment"),
        payload,
        config=ctx.config,
        today=ctx.today,
        now=ctx.clock,
        called_by_id=ctx.actor_id,
        called_by_label=_caller(ctx),
    )
    await audit.record(
        ctx.session, action=f"follow_up.{payload.outcome.value}", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="appointment", entity_id=str(appointment.id),
        meta={"call_id": str(call.id), **({"from": call.moved_from, "to": call.moved_to} if was else {})},
    )

    # The patient hears about a move or a cancellation exactly as they would
    # had it been made from the diary.
    if was is not None:
        await announce_move(ctx, appointment, was)
    elif payload.outcome == FollowUpOutcome.DECLINED and payload.cancel_appointment:
        await message_service.cancel_pending_for_appointment(ctx.session, appointment.id)
        await announce_cancellation(ctx, appointment)

    row = await follow_up_service.get_follow_up(ctx.session, ctx.tenant_id, appointment.id)
    return _out(row, ctx.tz)
