"""Patient messaging — provider settings, the outbox, and sending.

Switching provider is a PATCH to `/v1/messages/config`, which is why the whole
feature needed one column rather than a deploy. Only a tenant admin may change
it (`tenant:config:write`); anyone who can send may read what went out.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select

from app.api.deps import TenantContext, parse_uuid, require_permission
from app.core.errors import Conflict, NotFound
from app.core.permissions import Permission
from app.models.clinical import Patient
from app.models.enums import MessageStatus, MessageTemplate
from app.models.messaging import Message
from app.schemas.messaging import (
    MessageOut,
    MessagePageOut,
    MessageSendIn,
    MessageTestIn,
    MessageTestOut,
    MessagingConfigIn,
    MessagingConfigOut,
)
from app.services import audit
from app.services.messaging import outbox, reminders, service
from app.services.messaging.crypto import encrypt_secret
from app.services.messaging.providers import available_providers, build_provider
from app.services.messaging.providers.base import Outbound, SendRefused, SendRetryable

router = APIRouter(tags=["messaging"])


@router.get("/messages/config", response_model=MessagingConfigOut)
async def read_config(
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_CONFIG_READ)),
) -> MessagingConfigOut:
    config = await service.config_for(ctx.session, ctx.tenant_id)
    return MessagingConfigOut.of(config, available_providers())


@router.patch("/messages/config", response_model=MessagingConfigOut)
async def update_config(
    payload: MessagingConfigIn,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_CONFIG_WRITE)),
) -> MessagingConfigOut:
    """Change how this clinic sends. Absent fields are left alone.

    `auth_token` omitted keeps the stored credential; sent empty clears it.
    """
    config = await service.config_for(ctx.session, ctx.tenant_id)
    data = payload.model_dump(exclude_unset=True)

    if "auth_token" in data:
        config.auth_token_encrypted = encrypt_secret(data.pop("auth_token") or "")
    for field, value in data.items():
        setattr(config, field, value)

    # Turning sending on with nothing behind it would queue messages that can
    # only fail, so it is refused at the edge instead.
    if config.enabled and config.provider != "console":
        if not config.sender_id:
            raise Conflict("Set a sender number before enabling messages.")
        if config.provider == "twilio" and not (config.account_sid and config.auth_token_encrypted):
            raise Conflict("Twilio needs an account SID and auth token before it can be enabled.")

    await ctx.session.flush()
    await audit.record(
        ctx.session, action="messaging.config.updated", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="messaging_config", entity_id=str(config.id),
        meta={"provider": str(config.provider), "enabled": config.enabled},
    )
    return MessagingConfigOut.of(config, available_providers())


@router.get("/messages", response_model=MessagePageOut)
async def list_messages(
    ctx: TenantContext = Depends(require_permission(Permission.MESSAGE_READ)),
    patient_id: str | None = Query(default=None),
    status_filter: MessageStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=0, le=200),
    offset: int = Query(default=0, ge=0),
) -> MessagePageOut:
    where = []
    if patient_id:
        where.append(Message.patient_id == parse_uuid(patient_id, what="patient"))
    if status_filter:
        where.append(Message.status == status_filter)

    total = (
        await ctx.session.execute(select(func.count(Message.id)).where(*where))
    ).scalar() or 0

    query = (
        select(Message, Patient.full_name)
        .join(Patient, Patient.id == Message.patient_id)
        .where(*where)
        .order_by(Message.created_at.desc())
        .offset(offset)
    )
    if limit:
        query = query.limit(limit)

    rows = (await ctx.session.execute(query)).all()
    return MessagePageOut(
        items=[MessageOut.of(m, ctx.tz, name) for m, name in rows],
        total=int(total),
    )


@router.post("/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
async def send_message(
    payload: MessageSendIn,
    ctx: TenantContext = Depends(require_permission(Permission.MESSAGE_SEND)),
) -> MessageOut:
    """Queue a message to a patient — the follow-up path."""
    patient = await ctx.session.get(Patient, parse_uuid(payload.patient_id, what="patient"))
    if patient is None or patient.tenant_id != ctx.tenant_id:
        raise NotFound("No such patient.")
    if not patient.phone:
        raise Conflict("That patient has no phone number on file.")

    if payload.template == MessageTemplate.CUSTOM and not (payload.body or "").strip():
        raise Conflict("A custom message needs a body.")

    message = await service.queue_message(
        ctx.session,
        tenant_id=ctx.tenant_id,
        patient=patient,
        template=payload.template,
        channel=payload.channel,
        body=payload.body,
    )
    if message is None:
        raise Conflict("Messaging is switched off for this clinic.")

    await ctx.session.flush()
    await outbox.nudge(str(message.id))
    await audit.record(
        ctx.session, action="message.queued", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="message", entity_id=str(message.id),
        meta={"template": str(payload.template)},
    )
    return MessageOut.of(message, ctx.tz, patient.full_name)


@router.post("/messages/{message_id}/retry", response_model=MessageOut)
async def retry_message(
    message_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.MESSAGE_SEND)),
) -> MessageOut:
    """Put a failed message back in the queue."""
    message = await ctx.session.get(Message, parse_uuid(message_id, what="message"))
    if message is None or message.tenant_id != ctx.tenant_id:
        raise NotFound("No such message.")
    if message.status != MessageStatus.FAILED:
        raise Conflict("Only a failed message can be retried.")

    message.attempts = 0
    message.last_error = ""
    if message.job_id:
        # A reminder: it goes back to Celery, under a new job, straight away.
        message.send_after = reminders.now()
        reminders.queue(ctx.session, message)
        await ctx.session.flush()
    else:
        message.status = MessageStatus.QUEUED
        await ctx.session.flush()
        await outbox.nudge(str(message.id))

    patient = await ctx.session.get(Patient, message.patient_id)
    return MessageOut.of(message, ctx.tz, patient.full_name if patient else "")


@router.delete("/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message(
    message_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.MESSAGE_SEND)),
) -> None:
    """Delete a message that has not gone out — a reminder, most often.

    Its job, if it has one, is revoked once the delete commits, and the row
    is gone. One being sent right now, or already sent, is the record of what
    the patient received and stays.
    """
    message = await ctx.session.get(Message, parse_uuid(message_id, what="message"))
    if message is None or message.tenant_id != ctx.tenant_id:
        raise NotFound("No such message.")
    if message.status in (MessageStatus.SENDING, MessageStatus.SENT):
        raise Conflict("That message has already gone out.")

    reminders.stop(ctx.session, message)
    await ctx.session.delete(message)
    await ctx.session.flush()
    await audit.record(
        ctx.session, action="message.deleted", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="message", entity_id=str(message.id),
        meta={"template": str(message.template), "status": str(message.status)},
    )


@router.post("/messages/test", response_model=MessageTestOut)
async def test_provider(
    payload: MessageTestIn,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_CONFIG_WRITE)),
) -> MessageTestOut:
    """Send one message immediately, bypassing the queue, to prove the
    credentials work. Nothing is written to the outbox — this is a wiring
    check, not a patient message."""
    config = await service.config_for(ctx.session, ctx.tenant_id)
    try:
        provider = build_provider(config)
        result = await provider.send(
            Outbound(to=payload.to, body=payload.body, channel="sms", sender_id=config.sender_id)
        )
    except (SendRefused, SendRetryable) as exc:
        return MessageTestOut(ok=False, provider=config.provider, detail=str(exc))
    return MessageTestOut(
        ok=True,
        provider=config.provider,
        detail="Sent.",
        provider_message_id=result.provider_message_id,
    )
