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

`due` is the sweep's half. Anything still `queued` whose time has come is
handed back to the queue, which is what makes the broker a delivery mechanism
rather than the record: wipe Redis and nothing is lost, only delayed.

Like the message worker, nothing here reads across clinics in one query. The
sweep walks the tenant registry and binds each clinic in turn, so row-level
security and the ORM guard apply to the worker exactly as they do to a request.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.errors import Conflict, NotFound
from app.db.session import set_tenant
from app.models.enums import SupportRequestKind, SupportRequestStatus, TenantFeatureStatus
from app.models.identity import User
from app.models.support import SupportRequest
from app.models.tenancy import Feature, Tenant, TenantConfig, TenantFeature
from app.services import audit, mailer

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
    feature_key: str | None,
    message: str,
    ip: str = "",
    user_agent: str = "",
) -> tuple[SupportRequest, bool]:
    """Record the ask. Returns the row and whether it is new.

    The same person asking for the same thing again inside the dedupe window
    gets their earlier request back rather than a second email — a double click
    or an impatient retry should not page the desk twice.
    """
    if feature_key is not None:
        feature = await session.get(Feature, feature_key)
        if feature is None:
            raise NotFound("No such module.")
        if kind == SupportRequestKind.ACTIVATION:
            if feature.is_core:
                raise Conflict(f"{feature.name} is a core module; every clinic already has it.")
            current = (
                await session.execute(
                    select(TenantFeature.status).where(
                        TenantFeature.tenant_id == tenant.id,
                        TenantFeature.feature_key == feature_key,
                    )
                )
            ).scalar_one_or_none()
            if current == TenantFeatureStatus.ACTIVE:
                raise Conflict(f"{feature.name} is already enabled for this clinic.")

    since = datetime.now(timezone.utc) - timedelta(minutes=settings.support_request_dedupe_minutes)
    earlier = (
        await session.execute(
            select(SupportRequest)
            .where(
                SupportRequest.requested_by_id == user.id,
                SupportRequest.kind == kind,
                SupportRequest.feature_key.is_(None)
                if feature_key is None
                else SupportRequest.feature_key == feature_key,
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
        feature_key=feature_key,
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
        ip=ip, user_agent=user_agent, meta={"feature_key": feature_key},
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


def backoff(attempts: int) -> datetime:
    """Exponential, capped at an hour. `attempts` counts the one that just failed."""
    seconds = min(settings.support_request_retry_base_seconds * (2 ** max(0, attempts - 1)), 3600)
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _console_url(tenant_id: uuid.UUID) -> str:
    return f"{settings.admin_origin}/platform/tenants/{tenant_id}"


async def _email_for(session: AsyncSession, row: SupportRequest) -> mailer.Email:
    tenant = await session.get(Tenant, row.tenant_id)
    feature = await session.get(Feature, row.feature_key) if row.feature_key else None
    # Read, never created: `tenants.get_config` would insert a missing row, and
    # sending an email is no reason to write clinic configuration.
    currency = (
        await session.execute(
            select(TenantConfig.currency).where(TenantConfig.tenant_id == row.tenant_id)
        )
    ).scalar_one_or_none()
    price = None
    if feature and feature.price_monthly:
        # The ISO code, not a symbol: it reads the same in every mail client.
        price = f"{currency.value if currency else ''} {float(feature.price_monthly):,.0f}".strip()
    return mailer.support_request(
        to=settings.support_email,
        kind=row.kind.value,
        clinic=tenant.name if tenant else "A clinic",
        slug=tenant.slug if tenant else "",
        module=feature.name if feature else None,
        module_key=feature.key if feature else None,
        price=price,
        requested_by=row.requested_by_label or "Unknown",
        reply_to=row.reply_to or None,
        message=row.message,
        console_url=_console_url(row.tenant_id),
        reference=str(row.id),
    )


async def deliver(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, request_id: uuid.UUID
) -> str:
    """Email the desk about one request, once.

    Returns what happened: `sent`, `retry`, `failed`, `skipped` (no longer
    queued, or not yet due), or `missing` (not visible — the creating
    transaction has not committed, another worker holds the lock, or it is
    gone). The caller decides whether `missing` is worth another look; the
    sweep will find the row either way.

    The row is locked for the length of the send. If the worker is killed
    mid-send the transaction dies with it, the lock is released, the row is
    still `queued`, and the redelivered job (or the sweep) sends it. That is
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
            if row.next_attempt_at > datetime.now(timezone.utc):
                return "skipped"

            email = await _email_for(session, row)
            row.attempts += 1
            try:
                await mailer.send(email)
            except mailer.MailError as exc:
                row.last_error = str(exc)[:2000]
                if row.attempts >= settings.support_request_max_attempts:
                    row.status = SupportRequestStatus.FAILED
                    outcome = "failed"
                    log.error("support request %s failed for good: %s", row.id, exc)
                else:
                    row.next_attempt_at = backoff(row.attempts)
                    outcome = "retry"
                    log.warning("support request %s deferred (attempt %s): %s", row.id, row.attempts, exc)
                if outcome == "failed":
                    await audit.record(
                        session, action="support.request_email", tenant_id=tenant_id,
                        entity="support_request", entity_id=str(row.id), outcome="failed",
                        meta={"attempts": row.attempts, "error": row.last_error[:300]},
                    )
                return outcome

            row.status = SupportRequestStatus.SENT
            row.sent_at = datetime.now(timezone.utc)
            row.last_error = ""
            await audit.record(
                session, action="support.request_email", tenant_id=tenant_id,
                entity="support_request", entity_id=str(row.id),
                meta={"to": settings.support_email, "attempts": row.attempts},
            )
            log.info("support request %s emailed to %s", row.id, settings.support_email)
            return "sent"


async def due(
    factory: async_sessionmaker[AsyncSession], *, limit: int = 100
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Every `(tenant_id, request_id)` still queued and due, clinic by clinic."""
    now = datetime.now(timezone.utc)
    async with factory() as session:
        tenant_ids = list((await session.execute(select(Tenant.id))).scalars())

    found: list[tuple[uuid.UUID, uuid.UUID]] = []
    for tenant_id in tenant_ids:
        async with factory() as session:
            async with session.begin():
                await set_tenant(session, tenant_id)
                ids = (
                    await session.execute(
                        select(SupportRequest.id)
                        .where(
                            SupportRequest.status == SupportRequestStatus.QUEUED,
                            SupportRequest.next_attempt_at <= now,
                        )
                        .order_by(SupportRequest.next_attempt_at)
                        .limit(limit)
                    )
                ).scalars()
                found.extend((tenant_id, i) for i in ids)
        if len(found) >= limit:
            break
    return found[:limit]
