"""Appointment reminders, and the Celery jobs that send them.

A reminder is a `messages` row with `template = appointment_reminder`. Its
`status` and `job_id` say where it has got to:

    scheduled   its day has not come. There is no job and nothing in the
                broker; the midnight run will queue it.
    queued      a job is in the broker, due at `send_after`, and `job_id` is
                that job's Celery task id.
    sending     the job has claimed it and is talking to the provider.
    sent        delivered.
    failed      refused by the carrier, or out of retries.
    cancelled   the appointment was cancelled or missed; its job was revoked.

Only a reminder due *today* is ever in the broker — today on the PMS's clock,
`PMS_TIMEZONE`. The midnight run (`promote`) queues each day's reminders as
that day starts, and a reminder created or moved onto today is queued at once,
without waiting for it. A reminder for a later day stays `scheduled`.

The row decides; the job only triggers. A job acts only while the row still
names it and is still `queued`. That one check is what makes the awkward cases
safe: a revoke the worker never heard about (revokes live in worker memory
only), a job delivered twice (Redis hands an ETA job out again every visibility
timeout), a job left over from a reschedule. None of them can send, because the
row has moved on without them.

The broker is never the only record. `sweep` finds a reminder still `queued`
well after its time — its job was lost to a broker restart, or never
published because Redis was down — and gives it a new job. It does the same
for one still `scheduled` after its time, which a missed midnight run left
behind: late is better than never.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from datetime import time as time_t

from sqlalchemy import and_, event, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.db.session import set_tenant
from app.models.enums import MessageStatus, TenantStatus
from app.models.messaging import Message
from app.models.tenancy import Tenant
from app.schemas.common import zone

log = logging.getLogger("pms.reminders")

#: A reminder in one of these still has something ahead of it.
LIVE = (MessageStatus.SCHEDULED, MessageStatus.QUEUED)

#: A queued reminder this far past its send time has lost its job.
LOST_AFTER = timedelta(minutes=5)


def now() -> datetime:
    """The clock every decision here reads — one place for a test to move it."""
    return datetime.now(timezone.utc)


def end_of_today(at: datetime | None = None) -> datetime:
    """The next midnight on the PMS's clock, in UTC. Anything due before it is
    due today."""
    tz = zone(settings.pms_timezone)
    today = (at or now()).astimezone(tz).date()
    return datetime.combine(today + timedelta(days=1), time_t(0), tzinfo=tz).astimezone(timezone.utc)


# --- the broker -------------------------------------------------------------------------------


def _publish(tenant_id: str, message_id: str, job_id: str, eta: datetime | None) -> None:
    """Hand one job to the broker. Blocking, and never raises: the row is
    written, so a broker that is down costs a sweep's delay, not the reminder."""
    from app.worker.tasks import send_reminder

    try:
        send_reminder.apply_async(
            args=(tenant_id, message_id), task_id=job_id, eta=eta, retry=False
        )
    except Exception as exc:  # noqa: BLE001 - deliberately swallowed, see docstring
        log.warning("could not queue reminder %s, the sweep will: %s", message_id, exc)


def _revoke(job_id: str) -> None:
    """Tell the workers to drop a job. Best effort: the row has already moved
    on, and a job that runs anyway finds it no longer named and does nothing."""
    from app.worker.celery_app import celery_app

    try:
        celery_app.control.revoke(job_id)
    except Exception as exc:  # noqa: BLE001 - deliberately swallowed, see docstring
        log.warning("could not revoke reminder job %s: %s", job_id, exc)


#: Broker calls scheduled after a commit, held so they are not collected mid-flight.
_pending: set[asyncio.Task] = set()


def _after_commit(session: AsyncSession, call: Callable[..., None], *args: object) -> None:
    """Run a broker call once this transaction commits — never before it.

    A job published before the commit could run against a row that is then
    rolled back; a revoke sent before it could stop the job of a reschedule
    that never happened.
    """

    def _run(_session) -> None:
        task = asyncio.get_running_loop().create_task(asyncio.to_thread(call, *args))
        _pending.add(task)
        task.add_done_callback(_pending.discard)

    event.listen(session.sync_session, "after_commit", _run, once=True)


async def drain() -> None:
    """Wait for broker calls still in flight — for jobs, tests, and shutdown."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


# --- the request's half -----------------------------------------------------------------------


def queue(session: AsyncSession, message: Message) -> None:
    """Give the reminder a new job, due at its send time.

    A fresh id every time, so whatever job the row named before is superseded
    by the row itself, whether or not its revoke ever lands.
    """
    message.status = MessageStatus.QUEUED
    message.job_id = str(uuid.uuid4())
    eta = message.send_after if message.send_after > now() else None
    _after_commit(session, _publish, str(message.tenant_id), str(message.id), message.job_id, eta)


def place(session: AsyncSession, message: Message) -> None:
    """Queued now if it is due today; scheduled if its day is still to come.
    The row must already be flushed."""
    if message.send_after < end_of_today():
        queue(session, message)
    else:
        message.status = MessageStatus.SCHEDULED
        message.job_id = None


def stop(session: AsyncSession, message: Message) -> None:
    """Revoke the job this row names, if it names one. The id stays on the row
    as a record of the last job it had, until `place` or `queue` replaces it."""
    if message.job_id and message.status == MessageStatus.QUEUED:
        _after_commit(session, _revoke, message.job_id)


def cancel(session: AsyncSession, message: Message) -> None:
    stop(session, message)
    message.status = MessageStatus.CANCELLED


# --- the worker's half ------------------------------------------------------------------------


async def _clinics(factory: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    """Every clinic not suspended. `tenants` is the registry, not a
    tenant-owned table, so this read runs with nothing bound."""
    async with factory() as session:
        rows = await session.execute(
            select(Tenant.id).where(Tenant.status != TenantStatus.SUSPENDED)
        )
        return list(rows.scalars())


async def send(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    message_id: uuid.UUID,
    job_id: str,
) -> str:
    """What one reminder job does when its time comes.

    Returns `sent`, `failed`, `queued` (a transient failure: retrying under a
    new job), `superseded` (the row names another job — rescheduled, or
    re-queued by the sweep), `skipped` (no longer queued — cancelled, or
    already sent) or `missing` (not visible: the booking that queued it has not
    committed yet, another job holds it, or it was deleted).

    Two transactions, as the outbox has: the claim commits `sending` so the
    state is visible while the provider is called, and the send records its
    own outcome.
    """
    from app.services.messaging.worker import deliver_one  # imports this module

    async with factory() as session:
        async with session.begin():
            await set_tenant(session, tenant_id)
            row = (
                await session.execute(
                    select(Message).where(Message.id == message_id).with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                return "missing"
            if row.job_id != job_id:
                return "superseded"
            if row.status != MessageStatus.QUEUED:
                return "skipped"
            row.status = MessageStatus.SENDING

    async with factory() as session:
        async with session.begin():
            await set_tenant(session, tenant_id)
            row = await session.get(Message, message_id)
            if row is None:  # pragma: no cover - deleted mid-flight
                return "missing"
            await deliver_one(session, row)
            if row.status == MessageStatus.QUEUED:
                # `mark_retry` put it back with a backoff; a new job waits it out.
                queue(session, row)
            outcome = str(row.status)
    await drain()
    return outcome


async def promote(factory: async_sessionmaker[AsyncSession]) -> int:
    """The midnight run: queue every scheduled reminder due today.

    Running it late or twice costs nothing: anything due before the next
    midnight is queued, and a reminder for a later day is left exactly where
    it is.
    """
    cutoff = end_of_today()
    queued = 0
    for tenant_id in await _clinics(factory):
        async with factory() as session:
            async with session.begin():
                await set_tenant(session, tenant_id)
                due = (
                    (
                        await session.execute(
                            select(Message)
                            .where(
                                Message.status == MessageStatus.SCHEDULED,
                                Message.send_after < cutoff,
                            )
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in due:
                    queue(session, row)
                queued += len(due)
    await drain()
    return queued


async def sweep(factory: async_sessionmaker[AsyncSession]) -> int:
    """Give a new job to every reminder plainly stranded: queued with its job
    gone, or still scheduled after its time because a midnight run was missed.

    If the old job was only late (a backed-up worker) and runs after all, it
    finds the row naming the new one and stands down, so this never sends twice.
    """
    cutoff = now() - LOST_AFTER
    requeued = 0
    for tenant_id in await _clinics(factory):
        async with factory() as session:
            async with session.begin():
                await set_tenant(session, tenant_id)
                lost = (
                    (
                        await session.execute(
                            select(Message)
                            .where(
                                or_(
                                    and_(
                                        Message.status == MessageStatus.QUEUED,
                                        Message.job_id.is_not(None),
                                    ),
                                    Message.status == MessageStatus.SCHEDULED,
                                ),
                                Message.send_after <= cutoff,
                            )
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in lost:
                    queue(session, row)
                requeued += len(lost)
    await drain()
    return requeued
