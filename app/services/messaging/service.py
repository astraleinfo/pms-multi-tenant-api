"""Queueing patient messages, and the rules about when not to.

Every path into the outbox comes through here, so the guards live in one place:

*   nothing is queued for a clinic that has not enabled messaging;
*   nothing is queued for a patient with no phone number;
*   automatic messages respect their per-template switch.

`config_for` creates the row on first use, so a clinic is never missing one —
disabled and unconfigured, which is the safe shape.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clinical import Appointment, Patient
from app.models.enums import MessageChannel, MessageStatus, MessageTemplate
from app.models.identity import User
from app.models.messaging import Message, MessagingConfig
from app.models.tenancy import Tenant, TenantConfig
from app.services.messaging import templates

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
    # A change to a booked slot is not optional: if the clinic sends at all,
    # the patient is told it moved.

    if template == MessageTemplate.CUSTOM:
        text = " ".join((body or "").split())[: templates.MAX_BODY]
        if not text:
            raise ValueError("A custom message needs a body.")
    else:
        variables = await _variables(session, tenant_id, patient, appointment)
        variables.update(extra or {})
        text = templates.render(template, variables)

    message = Message(
        tenant_id=tenant_id,
        patient_id=patient.id,
        appointment_id=appointment.id if appointment else None,
        channel=channel or MessageChannel.SMS,
        template=template,
        status=MessageStatus.QUEUED,
        to_address=patient.phone[:40],
        body=text,
        send_after=send_after or datetime.now(timezone.utc),
    )
    session.add(message)
    await session.flush()
    return message


async def cancel_pending_for_appointment(session: AsyncSession, appointment_id: uuid.UUID) -> int:
    """Anything still waiting to go out names a slot that is no longer
    happening, so it is cancelled rather than sent."""
    rows = (
        (
            await session.execute(
                select(Message).where(
                    Message.appointment_id == appointment_id,
                    Message.status == MessageStatus.QUEUED,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = MessageStatus.CANCELLED
    return len(rows)
