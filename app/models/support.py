"""A clinic asking the platform for something.

A clinic asks for a walkthrough, or for help with anything else. The row is
the ask. Emailing the support desk is how the desk hears about it, and that
happens in the Celery worker, off the request: the row is written first, in the same transaction as
its audit entry, and the worker reads it back. A worker that dies mid-send, a
deploy, or a wiped broker delays the email; it never loses the request, because
the beat sweep re-queues anything still `queued`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin, uuid_pk
from app.models.enums import SupportRequestKind, SupportRequestStatus


class SupportRequest(Base, TenantScoped, TimestampMixin):
    __tablename__ = "support_requests"
    __extra_table_args__ = (
        # The sweep: queued and due, oldest first.
        Index("ix_support_requests_due", "status", "next_attempt_at"),
        Index("ix_support_requests_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[SupportRequestKind] = mapped_column(
        Enum(
            SupportRequestKind,
            name="support_request_kind",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # Denormalised on purpose: the email and the console both need to say who
    # asked, and a later rename or deletion must not rewrite that.
    requested_by_label: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    reply_to: Mapped[str] = mapped_column(String(254), nullable=False, default="")

    status: Mapped[SupportRequestStatus] = mapped_column(
        Enum(
            SupportRequestStatus,
            name="support_request_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=SupportRequestStatus.QUEUED,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
