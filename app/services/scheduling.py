"""§3.5 and §3.6 — appointments, check-in, and the queue state machine.

Every lookup that crosses entities (an appointment's patient, a queue entry's
doctor) is loaded through a tenant-scoped query, so a well-formed id belonging
to another clinic behaves exactly like an id that does not exist.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.models.clinical import Appointment, Patient, QueueEntry
from app.models.enums import (
    APPOINTMENT_TRANSITIONS,
    BOOKED_STATUSES,
    QUEUE_TRANSITIONS,
    AppointmentStatus,
    QueuePriority,
    QueueStatus,
)
from app.models.identity import User
from app.schemas.clinical import AppointmentIn, AppointmentRescheduleIn, WalkInIn
from app.services import paging
from app.services import tenants as tenant_service


def _minutes(hhmm: str) -> int:
    hours, _, mins = hhmm.partition(":")
    return int(hours) * 60 + int(mins)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _require_patient(session: AsyncSession, tenant_id: uuid.UUID, patient_id: uuid.UUID) -> Patient:
    row = (
        await session.execute(
            select(Patient).where(Patient.id == patient_id, Patient.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("No such patient.")
    return row


async def _require_doctor(session: AsyncSession, tenant_id: uuid.UUID, doctor_id: uuid.UUID) -> User:
    row = (
        await session.execute(
            select(User).where(User.id == doctor_id, User.tenant_id == tenant_id, User.role == "doctor")
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("No such clinician.")
    return row


# Only these can be sorted on, and the client names one rather than sending an
# expression — an ORDER BY is a query plan, not a free-text field.
APPOINTMENT_SORTS = {
    "when": (Appointment.date, Appointment.time),
    "reason": (Appointment.reason,),
    "created_via": (Appointment.created_via,),
    "status": (Appointment.status,),
}


async def list_appointments(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    day: date | None = None,
    doctor_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = paging.ALL,
    offset: int = 0,
    sort: str = "when",
    descending: bool = False,
) -> tuple[list[Appointment], int]:
    """One page of the diary, and how many bookings matched.

    `limit=0` returns the lot — which is what the day and week grids ask for,
    since a grid is not a page.
    """
    stmt = select(Appointment).where(Appointment.tenant_id == tenant_id)
    if day is not None:
        stmt = stmt.where(Appointment.date == day)
    if doctor_id is not None:
        stmt = stmt.where(Appointment.doctor_id == doctor_id)
    if search:
        like = f"%{search.strip()}%"
        # The clinician is a join rather than a column, because the search box
        # offers to match on their name.
        clinicians = select(User.id).where(User.tenant_id == tenant_id, User.name.ilike(like))
        stmt = stmt.join(Appointment.patient).where(
            or_(
                Patient.full_name.ilike(like),
                Patient.mrn.ilike(like),
                Appointment.reason.ilike(like),
                Appointment.doctor_id.in_(clinicians),
            )
        )

    columns = APPOINTMENT_SORTS.get(sort, APPOINTMENT_SORTS["when"])
    order = [c.desc().nulls_last() if descending else c.asc().nulls_last() for c in columns]
    # The primary key breaks every tie: without a total order, two rows that
    # sort the same could both land on page one and neither on page two.
    stmt = stmt.order_by(*order, Appointment.id)

    return await paging.page(session, stmt, limit=limit, offset=offset)


async def get_appointment(
    session: AsyncSession, tenant_id: uuid.UUID, appointment_id: uuid.UUID
) -> Appointment:
    row = (
        await session.execute(
            select(Appointment).where(
                Appointment.id == appointment_id, Appointment.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("No such appointment.")
    return row


async def conflicts_for(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    doctor_id: uuid.UUID,
    day: date,
    time: str,
    duration_min: int,
    ignore_id: uuid.UUID | None = None,
) -> list[Appointment]:
    """§3.5 — the availability check. Overlap, not equality: a 09:00 half-hour
    slot conflicts with a 09:15 booking."""
    start = _minutes(time)
    end = start + duration_min

    stmt = select(Appointment).where(
        Appointment.tenant_id == tenant_id,
        Appointment.doctor_id == doctor_id,
        Appointment.date == day,
        Appointment.status.notin_([AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW]),
    )
    if ignore_id is not None:
        stmt = stmt.where(Appointment.id != ignore_id)

    rows = list((await session.execute(stmt)).scalars())
    return [
        a
        for a in rows
        if start < _minutes(a.time) + a.duration_min and _minutes(a.time) < end
    ]


async def hold_diary(
    session: AsyncSession, tenant_id: uuid.UUID, doctor_id: uuid.UUID, day: date
) -> None:
    """Take this clinician's diary for this day until the transaction ends.

    The availability check reads, then the booking writes, so two desks booking
    the same free slot at the same moment would both see it free. This lock
    puts them in line: the second waits for the first to commit, and its check
    then sees the first booking and refuses. It is scoped to one clinician's
    day, so it never makes a booking for anyone else wait.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"diary:{tenant_id}:{doctor_id}:{day.isoformat()}"},
    )


async def create_appointment(
    session: AsyncSession, tenant_id: uuid.UUID, payload: AppointmentIn
) -> Appointment:
    patient = await _require_patient(session, tenant_id, uuid.UUID(payload.patient_id))
    doctor = await _require_doctor(session, tenant_id, uuid.UUID(payload.doctor_id))

    await hold_diary(session, tenant_id, doctor.id, payload.date)
    clashes = await conflicts_for(
        session,
        tenant_id,
        doctor_id=doctor.id,
        day=payload.date,
        time=payload.time,
        duration_min=payload.duration_min,
    )
    if clashes:
        raise Conflict(
            "That clinician is already booked then.",
            details={"conflicts": [{"id": str(c.id), "time": c.time, "reason": c.reason} for c in clashes]},
        )

    appointment = Appointment(
        tenant_id=tenant_id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        date=payload.date,
        time=payload.time,
        duration_min=payload.duration_min,
        reason=payload.reason,
        status=AppointmentStatus.SCHEDULED,
        created_via=payload.created_via,
    )
    session.add(appointment)
    await session.flush()
    # A row just added is not a row just queried: `selectin` runs on load, so
    # the patient is fetched explicitly before this is serialised.
    await session.refresh(appointment, ["patient"])
    return appointment


async def reschedule_appointment(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    appointment_id: uuid.UUID,
    payload: AppointmentRescheduleIn,
) -> tuple[Appointment, dict[str, str]]:
    """Move an appointment, and say what it moved from.

    Returns the appointment and the slot it used to occupy, because the message
    that tells the patient has to name both — "moved from Tuesday 9am to
    Thursday 2pm" is the only version of that message worth sending.

    A finished appointment is not moved: once someone has been seen or marked
    absent, the record is history rather than a plan.
    """
    appointment = await get_appointment(session, tenant_id, appointment_id)

    if appointment.status in (
        AppointmentStatus.COMPLETED,
        AppointmentStatus.CHECKED_IN,
        AppointmentStatus.NO_SHOW,
    ):
        raise Conflict("That appointment can no longer be moved.")

    was = {
        "date": appointment.date.strftime("%d %b %Y"),
        "time": appointment.time,
    }

    doctor_id = (
        uuid.UUID(payload.doctor_id) if payload.doctor_id else appointment.doctor_id
    )
    if payload.doctor_id:
        doctor = await _require_doctor(session, tenant_id, doctor_id)
        doctor_id = doctor.id

    day = payload.date or appointment.date
    time = payload.time or appointment.time
    duration = payload.duration_min or appointment.duration_min

    if (day, time, doctor_id, duration) == (
        appointment.date, appointment.time, appointment.doctor_id, appointment.duration_min
    ):
        raise Conflict("That is the slot it is already in.")

    # `ignore_id` keeps the appointment from clashing with itself when only the
    # duration or the clinician changes.
    await hold_diary(session, tenant_id, doctor_id, day)
    clashes = await conflicts_for(
        session,
        tenant_id,
        doctor_id=doctor_id,
        day=day,
        time=time,
        duration_min=duration,
        ignore_id=appointment.id,
    )
    if clashes:
        raise Conflict(
            "That clinician is already booked then.",
            details={
                "conflicts": [
                    {"id": str(c.id), "time": c.time, "reason": c.reason} for c in clashes
                ]
            },
        )

    appointment.doctor_id = doctor_id
    appointment.date = day
    appointment.time = time
    appointment.duration_min = duration
    if payload.reason is not None:
        appointment.reason = payload.reason
    # Moving a slot puts it back in the diary as scheduled, whatever it was.
    appointment.status = AppointmentStatus.SCHEDULED

    await session.flush()
    await session.refresh(appointment, ["patient"])
    return appointment, was


async def set_appointment_status(
    session: AsyncSession, tenant_id: uuid.UUID, appointment_id: uuid.UUID, status: AppointmentStatus
) -> Appointment:
    appointment = await get_appointment(session, tenant_id, appointment_id)
    current = AppointmentStatus(appointment.status)
    if status != current and status not in APPOINTMENT_TRANSITIONS[current]:
        raise Conflict(f"An appointment cannot go from {current.value} to {status.value}.")
    appointment.status = status
    await session.flush()
    return appointment


# --- the queue ---------------------------------------------------------------


async def _next_token(session: AsyncSession, tenant_id: uuid.UUID, day: date) -> int:
    """Per tenant, per day. The config row is locked first, so two front desks
    checking someone in at the same moment cannot both take token 7."""
    await tenant_service.lock_config(session, tenant_id)
    highest = (
        await session.execute(
            select(func.max(QueueEntry.token_no)).where(
                QueueEntry.tenant_id == tenant_id, QueueEntry.queue_date == day
            )
        )
    ).scalar()
    return (highest or 0) + 1


async def list_queue(
    session: AsyncSession, tenant_id: uuid.UUID, *, day: date | None = None
) -> list[QueueEntry]:
    stmt = select(QueueEntry).where(QueueEntry.tenant_id == tenant_id)
    if day is not None:
        stmt = stmt.where(QueueEntry.queue_date == day)
    stmt = stmt.order_by(QueueEntry.queue_date.desc(), QueueEntry.token_no)
    return list((await session.execute(stmt)).scalars())


async def get_queue_entry(session: AsyncSession, tenant_id: uuid.UUID, entry_id: uuid.UUID) -> QueueEntry:
    row = (
        await session.execute(
            select(QueueEntry).where(QueueEntry.id == entry_id, QueueEntry.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("No such queue entry.")
    return row


async def check_in(
    session: AsyncSession, tenant_id: uuid.UUID, appointment_id: uuid.UUID, *, today: date
) -> QueueEntry:
    appointment = await get_appointment(session, tenant_id, appointment_id)
    if appointment.status not in BOOKED_STATUSES:
        raise Conflict(f"That appointment is already {appointment.status.value.replace('_', ' ')}.")

    existing = (
        await session.execute(
            select(QueueEntry).where(
                QueueEntry.tenant_id == tenant_id, QueueEntry.appointment_id == appointment.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict("That appointment has already been checked in.")

    entry = QueueEntry(
        tenant_id=tenant_id,
        patient_id=appointment.patient_id,
        appointment_id=appointment.id,
        doctor_id=appointment.doctor_id,
        queue_date=today,
        token_no=await _next_token(session, tenant_id, today),
        status=QueueStatus.WAITING,
        priority=QueuePriority.ROUTINE,
        reason=appointment.reason,
        checked_in_at=_now(),
    )
    appointment.status = AppointmentStatus.CHECKED_IN
    session.add(entry)
    await session.flush()
    await session.refresh(entry, ["patient"])
    return entry


async def add_walk_in(
    session: AsyncSession, tenant_id: uuid.UUID, payload: WalkInIn, *, today: date
) -> QueueEntry:
    """A walk-in has no appointment row at all (§3.5)."""
    patient = await _require_patient(session, tenant_id, uuid.UUID(payload.patient_id))
    doctor = await _require_doctor(session, tenant_id, uuid.UUID(payload.doctor_id))

    entry = QueueEntry(
        tenant_id=tenant_id,
        patient_id=patient.id,
        appointment_id=None,
        doctor_id=doctor.id,
        queue_date=today,
        token_no=await _next_token(session, tenant_id, today),
        status=QueueStatus.WAITING,
        priority=payload.priority,
        reason=payload.reason,
        checked_in_at=_now(),
    )
    session.add(entry)
    await session.flush()
    await session.refresh(entry, ["patient"])
    return entry


async def set_queue_status(
    session: AsyncSession, tenant_id: uuid.UUID, entry_id: uuid.UUID, status: QueueStatus
) -> QueueEntry:
    entry = await get_queue_entry(session, tenant_id, entry_id)
    current = QueueStatus(entry.status)
    if status == current:
        return entry
    if status not in QUEUE_TRANSITIONS[current]:
        raise Conflict(f"A queue entry cannot go from {current.value} to {status.value}.")

    now = _now()
    entry.status = status
    if status == QueueStatus.CALLED and entry.called_at is None:
        entry.called_at = now
    elif status == QueueStatus.IN_CONSULTATION:
        entry.started_at = now
    elif status == QueueStatus.COMPLETED:
        entry.completed_at = now

    if status in (QueueStatus.NO_SHOW, QueueStatus.CANCELLED) and entry.appointment_id:
        appointment = await get_appointment(session, tenant_id, entry.appointment_id)
        target = (
            AppointmentStatus.NO_SHOW if status == QueueStatus.NO_SHOW else AppointmentStatus.CANCELLED
        )
        if target in APPOINTMENT_TRANSITIONS[AppointmentStatus(appointment.status)]:
            appointment.status = target

    await session.flush()
    return entry


def ordered_for_display(entries: list[QueueEntry], *, urgent_first: bool) -> tuple[list, list]:
    serving = sorted(
        [e for e in entries if e.status in (QueueStatus.CALLED, QueueStatus.IN_CONSULTATION)],
        key=lambda e: e.token_no,
    )
    waiting = [e for e in entries if e.status == QueueStatus.WAITING]
    if urgent_first:
        waiting.sort(key=lambda e: (0 if e.priority == QueuePriority.URGENT else 1, e.token_no))
    else:
        waiting.sort(key=lambda e: e.token_no)
    return serving, waiting


def short_name(full_name: str) -> str:
    """`Amelia Fitzgerald` → `Amelia F.` — the most a public screen may show."""
    parts = [p for p in full_name.split() if p]
    if not parts:
        return "—"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0]}."


def require_time_shape(value: str) -> str:
    try:
        hours, mins = value.split(":")
        if not (0 <= int(hours) <= 23 and 0 <= int(mins) <= 59):
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ValidationFailed("A time must look like 09:30.") from exc
    return value
