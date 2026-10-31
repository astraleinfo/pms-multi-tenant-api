"""Follow-up calls — ringing patients about their upcoming appointments.

The call list is the diary seen from the front desk's telephone: every booking
still to come inside a window, the number to ring, and where its follow-up
stands. Nothing about a follow-up is stored twice. Its status is worked out from
two things that already exist — the appointment's own status, and the last
call in the log — so the list, its counts and the appointment can never
disagree about whether somebody is coming.

Booking a new slot on a call is the diary's own reschedule: the same overlap
check, under the same lock, sending the patient the same "moved" message. The
only thing added here is what a phone call needs and the diary does not ask —
that the new slot is inside the clinic's day.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import String, and_, case, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, ValidationFailed
from app.models.clinical import Appointment, FollowUpCall, Patient
from app.models.enums import (
    BOOKED_STATUSES,
    AppointmentStatus,
    FollowUpOutcome,
    FollowUpStatus,
)
from app.models.tenancy import TenantConfig
from app.schemas.clinical import AppointmentRescheduleIn, FollowUpCallIn
from app.services import scheduling as scheduling_service

#: How far ahead the list looks when nobody says.
DEFAULT_DAYS = 7
#: A consultation's follow-up can be months out ("review in 180 days"); once
#: it is booked, the list has to reach it. A year, and a day for leap years.
MAX_DAYS = 366


def _minutes(hhmm: str) -> int:
    hours, _, mins = hhmm.partition(":")
    return int(hours) * 60 + int(mins)


def _text(value: str):
    return literal(value, String)


def status_expression(appointment_status, last_outcome):
    """Where a follow-up stands, in SQL, so the list can filter and count on it.

    - A confirmed appointment is confirmed, whatever was said on the phone
      since: a later "no answer" does not take back the patient's yes.
    - With no calls, it is pending.
    - A yes that is followed by the booking being moved — from the diary, back
      to plain `scheduled` — is pending again: the patient agreed to a slot the
      appointment is no longer in.
    - Anything else is what the last call said.
    """
    agreed = (FollowUpOutcome.CONFIRMED, FollowUpOutcome.RESCHEDULED)
    return case(
        (appointment_status == AppointmentStatus.CONFIRMED, _text(FollowUpStatus.CONFIRMED.value)),
        (last_outcome.is_(None), _text(FollowUpStatus.PENDING.value)),
        (
            last_outcome.in_(agreed),
            case(
                (appointment_status == AppointmentStatus.SCHEDULED, _text(FollowUpStatus.PENDING.value)),
                else_=_text(FollowUpStatus.CONFIRMED.value),
            ),
        ),
        else_=cast(last_outcome, String),
    )


def _calls(tenant_id: uuid.UUID):
    """The last call on each appointment, and how many there have been."""
    latest = (
        select(FollowUpCall.appointment_id, FollowUpCall.outcome, FollowUpCall.id.label("call_id"))
        .where(FollowUpCall.tenant_id == tenant_id)
        .distinct(FollowUpCall.appointment_id)
        .order_by(FollowUpCall.appointment_id, FollowUpCall.created_at.desc(), FollowUpCall.id.desc())
        .subquery("latest_call")
    )
    attempts = (
        select(FollowUpCall.appointment_id, func.count().label("n"))
        .where(FollowUpCall.tenant_id == tenant_id)
        .group_by(FollowUpCall.appointment_id)
        .subquery("call_attempts")
    )
    return latest, attempts


def _rows(tenant_id: uuid.UUID):
    """Every booking with its phone number and follow-up figures — no window
    yet. Returns the statement and the status expression, so a caller can
    filter on the expression rather than on a label PostgreSQL cannot see."""
    latest, attempts = _calls(tenant_id)
    status = status_expression(Appointment.status, latest.c.outcome)
    stmt = (
        select(
            Appointment,
            Patient.phone.label("patient_phone"),
            status.label("follow_up_status"),
            func.coalesce(attempts.c.n, 0).label("attempts"),
            latest.c.call_id.label("last_call_id"),
        )
        .join(Patient, and_(Patient.id == Appointment.patient_id, Patient.tenant_id == tenant_id))
        .outerjoin(latest, latest.c.appointment_id == Appointment.id)
        .outerjoin(attempts, attempts.c.appointment_id == Appointment.id)
        .where(Appointment.tenant_id == tenant_id)
    )
    return stmt, status, attempts


async def _with_last_calls(
    session: AsyncSession, tenant_id: uuid.UUID, rows: list[Any]
) -> list[dict[str, Any]]:
    ids = [r.last_call_id for r in rows if r.last_call_id is not None]
    calls: dict[uuid.UUID, FollowUpCall] = {}
    if ids:
        found = await session.execute(
            select(FollowUpCall).where(FollowUpCall.tenant_id == tenant_id, FollowUpCall.id.in_(ids))
        )
        calls = {c.id: c for c in found.scalars()}
    return [
        {
            "appointment": r[0],
            "patient_phone": r.patient_phone,
            "status": r.follow_up_status,
            "attempts": int(r.attempts),
            "last_call": calls.get(r.last_call_id) if r.last_call_id else None,
        }
        for r in rows
    ]


async def list_follow_ups(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    today: date,
    days: int = DEFAULT_DAYS,
    day: date | None = None,
    status: FollowUpStatus | None = None,
    doctor_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int, dict[str, int], int]:
    """One page of the call list, how many matched, the counts per status, and
    how many are today.

    The window is today and the `days - 1` after it. A booking is on the list
    while it is still going to happen — and stays on it, once somebody has rung
    about it, whatever became of it, so a declined call does not vanish from
    the morning's work. The counts are taken before `day` and `status`, which
    are the tabs.
    """
    stmt, status_expr, attempts = _rows(tenant_id)
    stmt = stmt.where(
        Appointment.date >= today,
        Appointment.date < today + timedelta(days=days),
        or_(Appointment.status.in_(BOOKED_STATUSES), attempts.c.n.is_not(None)),
    )
    if doctor_id is not None:
        stmt = stmt.where(Appointment.doctor_id == doctor_id)
    if search:
        like = f"%{search.strip()}%"
        digits = re.sub(r"\D", "", search)
        phone_like = Patient.phone.ilike(like)
        if len(digits) >= 3 and digits != search.strip():
            phone_like = or_(phone_like, Patient.phone.ilike(f"%{digits}%"))
        stmt = stmt.where(
            or_(
                Patient.full_name.ilike(like),
                Patient.mrn.ilike(like),
                phone_like,
                Appointment.reason.ilike(like),
            )
        )

    window = stmt.order_by(None).subquery()
    counts = {
        str(value): int(n)
        for value, n in await session.execute(
            select(window.c.follow_up_status, func.count()).group_by(window.c.follow_up_status)
        )
    }
    today_count = select(func.count()).select_from(window).where(window.c.date == today)
    on_today = int((await session.execute(today_count)).scalar() or 0)

    if day is not None:
        stmt = stmt.where(Appointment.date == day)
    if status is not None:
        stmt = stmt.where(status_expr == status.value)

    total = int(
        (await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar()
        or 0
    )
    stmt = stmt.order_by(Appointment.date, Appointment.time, Appointment.id)
    if limit:
        stmt = stmt.limit(limit).offset(offset)
    rows = list((await session.execute(stmt)).all())
    return await _with_last_calls(session, tenant_id, rows), total, counts, on_today


async def get_follow_up(
    session: AsyncSession, tenant_id: uuid.UUID, appointment_id: uuid.UUID
) -> dict[str, Any]:
    """One booking's row, wherever it now sits — a call can move it out of
    the window the list was showing."""
    stmt, _, _ = _rows(tenant_id)
    row = (await session.execute(stmt.where(Appointment.id == appointment_id))).one_or_none()
    if row is None:
        # The same answer as any other id this clinic does not have.
        await scheduling_service.get_appointment(session, tenant_id, appointment_id)
    return (await _with_last_calls(session, tenant_id, [row]))[0]


async def list_calls(
    session: AsyncSession, tenant_id: uuid.UUID, appointment_id: uuid.UUID
) -> list[FollowUpCall]:
    """Every call made about one booking, newest first."""
    await scheduling_service.get_appointment(session, tenant_id, appointment_id)
    rows = await session.execute(
        select(FollowUpCall)
        .where(FollowUpCall.tenant_id == tenant_id, FollowUpCall.appointment_id == appointment_id)
        .order_by(FollowUpCall.created_at.desc(), FollowUpCall.id.desc())
    )
    return list(rows.scalars())


def require_bookable(
    config: TenantConfig, *, today: date, now: str, day: date, time: str, duration: int
) -> None:
    """A slot offered over the phone has to be one the patient can come to:
    not in the past, and inside the clinic's day. The diary does not ask the
    second — it is also where yesterday's walk-in is written up — so it is here.

    "Not in the past" is the diary's own rule (a start earlier than the current
    minute is refused), so the call list and the diary agree. There is no
    second, stricter rule for calls.
    """
    scheduling_service.require_start_not_past(day=day, time=time, today=today, now=now)
    start = _minutes(time)
    if start < _minutes(config.day_start) or start + duration > _minutes(config.day_end):
        raise ValidationFailed(
            f"The clinic books between {config.day_start} and {config.day_end}; "
            "that slot falls outside it."
        )


async def record_call(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    appointment_id: uuid.UUID,
    payload: FollowUpCallIn,
    *,
    config: TenantConfig,
    today: date,
    now: str,
    called_by_id: uuid.UUID | None,
    called_by_label: str,
) -> tuple[FollowUpCall, Appointment, dict[str, str] | None]:
    """Log one call and do what it says to the appointment.

    Returns the call, the appointment as it now is, and — if the call moved it
    — the slot it moved from, for the message that tells the patient.

    A refused move (the slot went while the patient was deciding) refuses the
    whole call: nothing is logged, and the desk picks another slot with the
    patient still on the line.
    """
    appointment = await scheduling_service.get_appointment(session, tenant_id, appointment_id)
    if appointment.status not in BOOKED_STATUSES:
        state = AppointmentStatus(appointment.status).value.replace("_", " ")
        raise Conflict(f"That appointment is already {state}, so there is nothing to follow up.")
    if appointment.date < today:
        raise Conflict("That appointment has already passed.")

    outcome = payload.outcome
    was: dict[str, str] | None = None
    moved_from = moved_to = None

    if outcome == FollowUpOutcome.CONFIRMED:
        appointment.status = AppointmentStatus.CONFIRMED

    elif outcome == FollowUpOutcome.RESCHEDULED:
        slot = payload.new_slot
        assert slot is not None  # the schema refuses a reschedule without one
        require_bookable(
            config, today=today, now=now, day=slot.date, time=slot.time,
            duration=slot.duration_min or appointment.duration_min,
        )
        moved_from = f"{appointment.date.isoformat()} {appointment.time}"
        appointment, was = await scheduling_service.reschedule_appointment(
            session,
            tenant_id,
            appointment.id,
            AppointmentRescheduleIn(
                date=slot.date, time=slot.time, doctor_id=slot.doctor_id, duration_min=slot.duration_min
            ),
            today=today,
            now=now,
        )
        # The patient chose this slot on the phone, so it is confirmed as booked.
        appointment.status = AppointmentStatus.CONFIRMED
        moved_to = f"{appointment.date.isoformat()} {appointment.time}"

    elif outcome in (FollowUpOutcome.RESCHEDULE_REQUIRED, FollowUpOutcome.DECLINED):
        if payload.cancel_appointment:
            appointment.status = AppointmentStatus.CANCELLED
        elif appointment.status == AppointmentStatus.CONFIRMED:
            # The patient has taken back a yes. Not a transition the diary
            # offers, which is why it is set here rather than through it.
            appointment.status = AppointmentStatus.SCHEDULED

    call = FollowUpCall(
        tenant_id=tenant_id,
        appointment_id=appointment.id,
        patient_id=appointment.patient_id,
        outcome=outcome,
        notes=payload.notes,
        called_by_id=called_by_id,
        called_by_label=called_by_label[:160],
        moved_from=moved_from,
        moved_to=moved_to,
    )
    session.add(call)
    await session.flush()
    return call, appointment, was
