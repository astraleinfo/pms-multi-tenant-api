"""Support requests: a clinic asking the platform desk for something.

Two halves, deliberately apart:

*   `create` runs inside the request. It writes the row and its audit entry in
    the request's own transaction, and that is the whole of what the clinic
    waits for — "your request is in" is true the moment the row commits.
*   `deliver` runs in the Celery worker. It re-reads the row under a lock,
    emails the desk, and records the outcome. It is idempotent: a request that
    is not `queued` any more is left alone, so a redelivered job (the worker
    was killed mid-send, a deploy restarted it) cannot email the desk twice
    for one that already went out.

Nothing re-sends on its own. One attempt per hand-off: a send that fails marks
the request `failed`, and a request whose hand-off to the broker was lost stays
`queued`. Either way it shows as `retryable`, and the desk sends it again from
the console with `retry`. The row is the record, so a wiped broker loses
nothing — it only waits for someone to press the button.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.errors import Conflict
from app.db.session import set_tenant
from app.models.enums import SupportRequestKind, SupportRequestStatus
from app.models.identity import User
from app.models.support import SupportRequest
from app.models.tenancy import Tenant
from app.services import audit, mailer, platform_logo
from app.services import platform as platform_service

log = logging.getLogger("pms.support")


# --- the request half ---------------------------------------------------------


async def create(
    session: AsyncSession,
    *,
    tenant: Tenant,
    user: User,
    actor_kind: str,
    actor_label: str,
    kind: SupportRequestKind,
    message: str,
    ip: str = "",
    user_agent: str = "",
) -> tuple[SupportRequest, bool]:
    """Record the ask. Returns the row and whether it is new.

    The same person asking for the same thing again inside the dedupe window
    gets their earlier request back rather than a second email — a double click
    or an impatient retry should not page the desk twice.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=settings.support_request_dedupe_minutes)
    earlier = (
        await session.execute(
            select(SupportRequest)
            .where(
                SupportRequest.requested_by_id == user.id,
                SupportRequest.kind == kind,
                SupportRequest.created_at >= since,
            )
            .order_by(SupportRequest.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    # A general request is only a repeat if it says the same thing.
    if earlier is not None and (kind != SupportRequestKind.GENERAL or earlier.message == message):
        return earlier, False

    row = SupportRequest(
        tenant_id=tenant.id,
        kind=kind,
        message=message,
        requested_by_id=user.id,
        requested_by_label=actor_label,
        reply_to=user.email or "",
        status=SupportRequestStatus.QUEUED,
    )
    session.add(row)
    await session.flush()

    await audit.record(
        session, action=f"support.request_{kind.value}", tenant_id=tenant.id, actor_kind=actor_kind,
        actor_id=user.id, actor_label=actor_label, entity="support_request", entity_id=str(row.id),
        ip=ip, user_agent=user_agent,
    )
    return row, True


async def list_for_tenant(
    session: AsyncSession, tenant_id: uuid.UUID, *, limit: int = 50
) -> list[SupportRequest]:
    rows = await session.execute(
        select(SupportRequest)
        .where(SupportRequest.tenant_id == tenant_id)
        .order_by(SupportRequest.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars())


# --- the worker half ----------------------------------------------------------


#: How long a request may sit `queued` after its hand-off before it counts as
#: lost. Longer than the enqueue's countdown, so it never races a hand-off that
#: is still on its way.
STUCK_AFTER = timedelta(seconds=30)


def retryable(row: SupportRequest) -> bool:
    """Failed, or queued long enough that its hand-off must have been lost."""
    if row.status == SupportRequestStatus.FAILED:
        return True
    if row.status != SupportRequestStatus.QUEUED:
        return False
    cutoff = datetime.now(timezone.utc) - STUCK_AFTER
    # `created_at` first: a request this new is never stuck, and the check then
    # never reaches `updated_at`, which a fresh insert may not have loaded.
    return row.created_at <= cutoff and row.updated_at <= cutoff


async def retry(session: AsyncSession, row: SupportRequest) -> SupportRequest:
    """Put a failed or stuck request back in line. The caller hands it to the
    worker once this transaction has committed."""
    if not retryable(row):
        raise Conflict("This request is not stuck — it is on its way or already sent.")
    row.status = SupportRequestStatus.QUEUED
    # Set here rather than left to `onupdate`, so the response can read it
    # without another round trip; it is also what restarts the stuck clock.
    row.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return row


def _console_url(tenant_id: uuid.UUID) -> str:
    return f"{settings.admin_origin}/platform/tenants/{tenant_id}"


async def _email_for(session: AsyncSession, row: SupportRequest) -> mailer.Email:
    tenant = await session.get(Tenant, row.tenant_id)
    desk = await platform_service.get_platform_settings(session)
    return mailer.support_request(
        to=desk.support_email,
        kind=row.kind.value,
        clinic=tenant.name if tenant else "A clinic",
        slug=tenant.slug if tenant else "",
        requested_by=row.requested_by_label or "Unknown",
        reply_to=row.reply_to or None,
        message=row.message,
        console_url=_console_url(row.tenant_id),
        reference=str(row.id),
        mark=await platform_logo.email_mark(session),
    )


async def deliver(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, request_id: uuid.UUID
) -> str:
    """Email the desk about one request, once.

    Returns what happened: `sent`, `failed`, `skipped` (no longer queued), or
    `missing` (not visible — the creating transaction has not committed,
    another worker holds the lock, or it is gone). The caller decides whether
    `missing` is worth another look.

    The row is locked for the length of the send. If the worker is killed
    mid-send the transaction dies with it, the lock is released, the row is
    still `queued`, and the redelivered job sends it. That is
    at-least-once: a kill after the SMTP handover but before the commit can
    send the desk a duplicate, which is the right side to err on.
    """
    async with factory() as session:
        async with session.begin():
            await set_tenant(session, tenant_id)
            row = (
                await session.execute(
                    select(SupportRequest)
                    .where(SupportRequest.id == request_id)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                return "missing"
            if row.status != SupportRequestStatus.QUEUED:
                return "skipped"

            email = await _email_for(session, row)
            row.attempts += 1
            try:
                await mailer.send(email)
            except mailer.MailError as exc:
                # No automatic retry: the console shows it as failed and the
                # desk sends it again from there.
                row.last_error = str(exc)[:2000]
                row.status = SupportRequestStatus.FAILED
                log.error("support request %s failed (attempt %s): %s", row.id, row.attempts, exc)
                await audit.record(
                    session, action="support.request_email", tenant_id=tenant_id,
                    entity="support_request", entity_id=str(row.id), outcome="failed",
                    meta={"attempts": row.attempts, "error": row.last_error[:300]},
                )
                return "failed"

            row.status = SupportRequestStatus.SENT
            row.sent_at = datetime.now(timezone.utc)
            row.last_error = ""
            await audit.record(
                session, action="support.request_email", tenant_id=tenant_id,
                entity="support_request", entity_id=str(row.id),
                meta={"to": email.to, "attempts": row.attempts},
            )
            log.info("support request %s emailed to %s", row.id, email.to)
            return "sent"
