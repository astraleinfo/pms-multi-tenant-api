"""Queueing patient messages, and the rules about when not to.

Every path into the outbox comes through here, so the guards live in one place:

*   nothing is queued for a clinic that has not enabled messaging;
*   nothing is queued for a patient with no phone number;
*   automatic messages respect their per-template switch;
*   a reminder for a slot that has already passed is pointless, so it is not
    queued at all rather than queued and immediately stale.

Reminders are then handed to `reminders`, which decides whether one waits as
`scheduled` or goes to a Celery job now.

`config_for` creates the row on first use, so a clinic is never missing one —
disabled and unconfigured, which is the safe shape.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from datetime import time as time_t

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clinical import Appointment, Patient
from app.models.enums import MessageChannel, MessageStatus, MessageTemplate
from app.models.identity import User
from app.models.messaging import Message, MessagingConfig
from app.models.tenancy import Tenant, TenantConfig
from app.schemas.common import zone
from app.services.messaging import reminders, templates
from app.services.platform import Timings, get_timings

log = logging.getLogger("pms.messaging")


async def config_for(session: AsyncSession, tenant_id: uuid.UUID) -> MessagingConfig:
    """This clinic's messaging setup, created disabled if it has none yet."""
    row = (
        await session.execute(select(MessagingConfig).where(MessagingConfig.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        row = MessagingConfig(tenant_id=tenant_id)
        session.add(row)
        await session.flush()
    return row


async def _variables(
    session: AsyncSession, tenant_id: uuid.UUID, patient: Patient, appointment: Appointment | None
) -> dict[str, str]:
    tenant = await session.get(Tenant, tenant_id)
    doctor_name = ""
    if appointment is not None:
        doctor = await session.get(User, appointment.doctor_id)
        doctor_name = doctor.name if doctor else ""
    phone = (
        await session.execute(
            select(TenantConfig.contact_phone).where(TenantConfig.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    return {
        "patient_name": (patient.full_name or "").split(" ")[0],
        "clinic_name": tenant.name if tenant else "",
        "clinic_phone": phone or "",
        "doctor_name": doctor_name,
        "weekday": appointment.date.strftime("%a") if appointment else "",
        "date": appointment.date.strftime("%d %b %Y") if appointment else "",
        "time": appointment.time if appointment else "",
    }


def _reminder_send_after(appointment: Appointment, tz_name: str, timings: Timings) -> datetime:
    """`reminder_lead_hours` before the slot, in the clinic's own timezone."""
    hour, minute = (int(p) for p in appointment.time.split(":"))
    local = datetime.combine(appointment.date, time_t(hour, minute), tzinfo=zone(tz_name))
    return local.astimezone(timezone.utc) - timedelta(hours=timings.reminder_lead_hours)


async def queue_message(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    patient: Patient,
    template: MessageTemplate,
    appointment: Appointment | None = None,
    body: str | None = None,
    channel: MessageChannel | None = None,
    send_after: datetime | None = None,
    extra: dict[str, str] | None = None,
) -> Message | None:
    """Put one message in the outbox, or return None with a logged reason.

    Returning None rather than raising is deliberate: booking an appointment
    must not fail because the clinic has not set a provider up.
    """
    config = await config_for(session, tenant_id)
    if not config.enabled:
        return None
    if not patient.phone:
        log.info("no message queued: patient %s has no phone number", patient.id)
        return None
    if template == MessageTemplate.BOOKING_CONFIRMATION and not config.send_booking_confirmation:
        return None
    if template == MessageTemplate.APPOINTMENT_REMINDER and not config.send_appointment_reminder:
        return None
    # A change to a booked slot is not optional in the same way a marketing
    # reminder is: if the clinic sends at all, the patient is told it moved.

    if template == MessageTemplate.CUSTOM:
        text = " ".join((body or "").split())[: templates.MAX_BODY]
        if not text:
            raise ValueError("A custom message needs a body.")
    else:
        variables = await _variables(session, tenant_id, patient, appointment)
        variables.update(extra or {})
        text = templates.render(template, variables)

    when = send_after or datetime.now(timezone.utc)
    reminder = template == MessageTemplate.APPOINTMENT_REMINDER and appointment is not None
    if reminder:
        timings = await get_timings(session)
        tenant = await session.get(Tenant, tenant_id)
        when = _reminder_send_after(appointment, tenant.timezone, timings)
        # A slot inside the lead window - or already gone - gets no reminder.
        if when <= datetime.now(timezone.utc):
            return None

    message = Message(
        tenant_id=tenant_id,
        patient_id=patient.id,
        appointment_id=appointment.id if appointment else None,
        channel=channel or MessageChannel.SMS,
        template=template,
        status=MessageStatus.QUEUED,
        to_address=patient.phone[:40],
        body=text,
        send_after=when,
    )
    session.add(message)
    await session.flush()
    if reminder:
        reminders.place(session, message)
    return message


async def retime_reminder(
    session: AsyncSession, *, tenant_id: uuid.UUID, patient: Patient, appointment: Appointment
) -> Message | None:
    """The appointment moved: move its reminder with it.

    The reminder still waiting is re-timed in place — its job revoked, its
    text re-rendered for the new slot, and then scheduled or queued afresh with
    a new job id. With none waiting (the old slot was too close for one, or it
    was cancelled) a new one is queued. One that has already gone out is left
    as the record of what was sent, and the new slot gets no second reminder.
    """
    rows = (
        (
            await session.execute(
                select(Message).where(
                    Message.appointment_id == appointment.id,
                    Message.template == MessageTemplate.APPOINTMENT_REMINDER,
                    Message.status != MessageStatus.CANCELLED,
                )
            )
        )
        .scalars()
        .all()
    )
    live = next((m for m in rows if m.status in reminders.LIVE), None)
    if live is None:
        if rows:  # sending, sent or failed: the unique index allows no other
            return None
        return await queue_message(
            session,
            tenant_id=tenant_id,
            patient=patient,
            template=MessageTemplate.APPOINTMENT_REMINDER,
            appointment=appointment,
        )

    reminders.stop(session, live)
    config = await config_for(session, tenant_id)
    timings = await get_timings(session)
    when = _reminder_send_after(appointment, (await session.get(Tenant, tenant_id)).timezone, timings)
    if not (config.enabled and config.send_appointment_reminder and patient.phone) or (
        when <= datetime.now(timezone.utc)
    ):
        live.status = MessageStatus.CANCELLED
        return None

    live.body = templates.render(
        MessageTemplate.APPOINTMENT_REMINDER,
        await _variables(session, tenant_id, patient, appointment),
    )
    live.send_after = when
    reminders.place(session, live)
    return live


async def cancel_pending_for_appointment(
    session: AsyncSession,
    appointment_id: uuid.UUID,
    *,
    keep: tuple[MessageTemplate, ...] = (),
) -> int:
    """A cancelled slot must not still send a reminder tomorrow morning.

    Scheduled and queued alike are cancelled, and a queued reminder's job is
    revoked once this commits. `keep` names templates to leave alone.
    """
    where = [Message.appointment_id == appointment_id, Message.status.in_(reminders.LIVE)]
    if keep:
        where.append(Message.template.not_in(keep))
    rows = (await session.execute(select(Message).where(*where))).scalars().all()
    for row in rows:
        reminders.cancel(session, row)
    return len(rows)
