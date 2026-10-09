"""Message and provider-settings payloads.

The credential rule is visible in the shape: `MessagingConfigOut` carries
`auth_token_set` — a boolean — and never the token. There is no route by which
a stored secret leaves this process.
"""

from __future__ import annotations

from pydantic import Field

from app.models.enums import (
    MessageChannel,
    MessageProvider,
    MessageStatus,
    MessageTemplate,
)
from app.schemas.common import ApiModel, IdStr, InputModel, stamp


class MessagingConfigOut(ApiModel):
    provider: MessageProvider
    enabled: bool
    send_booking_confirmation: bool
    sender_id: str
    account_sid: str
    auth_token_set: bool
    available_providers: list[str]

    @classmethod
    def of(cls, config, providers: list[str]) -> MessagingConfigOut:
        return cls(
            provider=config.provider,
            enabled=config.enabled,
            send_booking_confirmation=config.send_booking_confirmation,
            sender_id=config.sender_id,
            account_sid=config.account_sid,
            auth_token_set=bool(config.auth_token_encrypted),
            available_providers=providers,
        )


class MessagingConfigIn(InputModel):
    """Every field optional: the settings screen sends what changed.

    `auth_token` omitted leaves the stored one alone; sent empty clears it.
    """

    provider: MessageProvider | None = None
    enabled: bool | None = None
    send_booking_confirmation: bool | None = None
    sender_id: str | None = Field(default=None, max_length=40)
    account_sid: str | None = Field(default=None, max_length=120)
    auth_token: str | None = Field(default=None, max_length=200)


class MessageOut(ApiModel):
    id: str
    patient_id: str
    patient_name: str
    appointment_id: str | None
    channel: MessageChannel
    template: MessageTemplate
    status: MessageStatus
    to_address: str
    body: str
    attempts: int
    last_error: str
    provider: MessageProvider | None
    #: when it is due to go out
    send_after: str | None
    sent_at: str | None
    created_at: str

    @classmethod
    def of(cls, m, tz: str, patient_name: str = "") -> MessageOut:
        return cls(
            id=str(m.id),
            patient_id=str(m.patient_id),
            patient_name=patient_name,
            appointment_id=str(m.appointment_id) if m.appointment_id else None,
            channel=m.channel,
            template=m.template,
            status=m.status,
            to_address=m.to_address,
            body=m.body,
            attempts=m.attempts,
            last_error=m.last_error,
            provider=m.provider,
            send_after=stamp(m.send_after, tz) if m.send_after else None,
            sent_at=stamp(m.sent_at, tz) if m.sent_at else None,
            created_at=stamp(m.created_at, tz) or "",
        )


class MessageSendIn(InputModel):
    """An ad-hoc message — the follow-up case. `body` is required for `custom`
    and ignored for the others, which render themselves."""

    patient_id: IdStr
    template: MessageTemplate = MessageTemplate.FOLLOW_UP
    channel: MessageChannel = MessageChannel.SMS
    body: str | None = Field(default=None, max_length=1200)


class MessagePageOut(ApiModel):
    items: list[MessageOut]
    total: int


class MessageTestIn(InputModel):
    """Send one message to a number of your choosing, to prove the wiring."""

    to: str = Field(min_length=4, max_length=40)
    body: str = Field(default="Test message from your clinic.", max_length=480)


class MessageTestOut(ApiModel):
    ok: bool
    provider: MessageProvider
    detail: str
    provider_message_id: str = ""
