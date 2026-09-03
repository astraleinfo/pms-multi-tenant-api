"""Patient messaging: who sends, and what is waiting to go out.

Two tables, with different jobs.

`messaging_configs` is how a clinic is wired up — which provider, with which
credentials. There is one row per tenant. Credentials are encrypted at rest and
never leave the process: the API returns whether a secret is *set*, never its
value.

`messages` is the outbox, and it is the source of truth for delivery. Redis
only carries a wake-up nudge, so a lost Redis delays a send but never drops
one — the worker's sweep finds anything the nudge missed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin, uuid_pk
from app.models.enums import (
    MessageChannel,
    MessageProvider,
    MessageStatus,
    MessageTemplate,
)


def _enum(cls, name: str):
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class MessagingConfig(Base, TenantScoped, TimestampMixin):
    """How this clinic sends. Switching provider is one column."""

    __tablename__ = "messaging_configs"
    __extra_table_args__ = (
        UniqueConstraint("tenant_id", name="uq_messaging_config_tenant"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()

    provider: Mapped[MessageProvider] = mapped_column(
        _enum(MessageProvider, "message_provider"),
        nullable=False,
        default=MessageProvider.CONSOLE,
    )
    # Nothing is sent at all while this is false — the safe default for a
    # clinic that has not finished setting its provider up.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Which templates send themselves. A clinic that wants to confirm bookings
    # but not chase reminders just turns one off.
    send_booking_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    send_appointment_reminder: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    sender_id: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    """The from-number or alphanumeric sender the provider sends as."""

    account_sid: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    """Twilio account SID. Not a secret on its own, so it is stored as it is."""

    auth_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """Fernet ciphertext. Never returned by the API, in any form."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<MessagingConfig {self.provider} enabled={self.enabled}>"


class Message(Base, TenantScoped, TimestampMixin):
    """One outbound message. The row is the queue."""

    __tablename__ = "messages"
    __extra_table_args__ = (
        # The worker's sweep: due, queued, oldest first.
        Index("ix_messages_due", "status", "send_after"),
        Index("ix_messages_tenant_created", "tenant_id", "created_at"),
        # At most one *live* confirmation and one *live* reminder per
        # appointment, so a retry cannot double-send either. Notices about a
        # change ("moved", "cancelled") are events and may repeat; a cancelled
        # reminder frees its slot so a reschedule can queue a fresh one.
        Index(
            "uq_messages_appointment_template",
            "appointment_id",
            "template",
            unique=True,
            postgresql_where=text(
                "appointment_id IS NOT NULL "
                "AND template IN ('booking_confirmation', 'appointment_reminder') "
                "AND status <> 'cancelled'"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()

    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )

    channel: Mapped[MessageChannel] = mapped_column(
        _enum(MessageChannel, "message_channel"), nullable=False, default=MessageChannel.SMS
    )
    template: Mapped[MessageTemplate] = mapped_column(
        _enum(MessageTemplate, "message_template"), nullable=False
    )
    status: Mapped[MessageStatus] = mapped_column(
        _enum(MessageStatus, "message_status"), nullable=False, default=MessageStatus.QUEUED
    )

    to_address: Mapped[str] = mapped_column(String(40), nullable=False)
    """Frozen at enqueue time: a later edit to the patient must not silently
    redirect a message that was already approved."""

    body: Mapped[str] = mapped_column(Text, nullable=False)
    """Rendered at enqueue time, for the same reason."""

    send_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")

    provider: Mapped[MessageProvider | None] = mapped_column(
        _enum(MessageProvider, "message_provider"), nullable=True
    )
    """Which adapter actually sent it — recorded on the message, so switching
    provider later does not rewrite what happened."""

    provider_message_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Message {self.template} {self.status} -> {self.to_address}>"
