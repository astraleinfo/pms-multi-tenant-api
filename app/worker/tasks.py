"""The jobs.

Celery tasks are synchronous and the application is async, so each task runs
its coroutine with `asyncio.run` — a fresh event loop per job. asyncpg
connections belong to the loop that opened them, so a task cannot borrow the
API's pooled engine; it opens its own with `NullPool` and disposes of it when
the job ends.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db import guard  # noqa: F401 - the ORM tenant guard applies to jobs too
from app.services import import_jobs, rent, support
from app.services.messaging import reminders
from app.worker.celery_app import celery_app

log = logging.getLogger("pms.worker")

T = TypeVar("T")


def _run(job: Callable[[async_sessionmaker[AsyncSession]], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
            return await job(factory)
        finally:
            await engine.dispose()

    return asyncio.run(main())


@celery_app.task(
    name="pms.support.deliver",
    bind=True,
    # Only for `missing`: the request that queued this may not have committed
    # yet. Mail failures are not Celery retries — they are written to the row
    # and the sweep brings them back when their backoff expires, so a long
    # backoff never sits in the broker as an ETA job.
    max_retries=3,
    default_retry_delay=10,
)
def deliver_support_request(self, tenant_id: str, request_id: str) -> str:
    outcome = _run(
        lambda factory: support.deliver(factory, uuid.UUID(tenant_id), uuid.UUID(request_id))
    )
    if outcome == "missing" and self.request.retries < self.max_retries:
        raise self.retry()
    return outcome


@celery_app.task(name="pms.support.sweep")
def sweep() -> int:
    """Re-queue every support request still waiting to be emailed."""
    pending = _run(lambda factory: support.due(factory))
    for tenant_id, request_id in pending:
        deliver_support_request.delay(str(tenant_id), str(request_id))
    if pending:
        log.info("sweep re-queued %s support request(s)", len(pending))
    return len(pending)


def _run_import(task, tenant_id: str, job_id: str) -> str:
    outcome = _run(lambda factory: import_jobs.run(factory, uuid.UUID(tenant_id), uuid.UUID(job_id)))
    if outcome == "missing" and task.request.retries < task.max_retries:
        raise task.retry()
    return outcome


@celery_app.task(
    name="pms.imports.run",
    bind=True,
    # Only for `missing`: the upload that queued this may not have committed
    # yet. A sheet that fails its checks is not a retry — it is a finished
    # job with a report, and the person fixes the sheet.
    max_retries=3,
    default_retry_delay=5,
)
def run_import(self, tenant_id: str, job_id: str) -> str:
    """Any kind of spreadsheet import — the job row says which."""
    return _run_import(self, tenant_id, job_id)


@celery_app.task(name="pms.inventory.import", bind=True, max_retries=3, default_retry_delay=5)
def run_inventory_import(self, tenant_id: str, job_id: str) -> str:
    """The name inventory imports were queued under before there were other
    kinds. Kept so a message already in the broker still runs."""
    return _run_import(self, tenant_id, job_id)


@celery_app.task(name="pms.imports.sweep")
def sweep_imports() -> int:
    """Re-queue every import still waiting — its hand-off to the broker was lost."""
    pending = _run(lambda factory: import_jobs.due(factory))
    for tenant_id, job_id in pending:
        run_import.delay(str(tenant_id), str(job_id))
    if pending:
        log.info("sweep re-queued %s import(s)", len(pending))
    return len(pending)


@celery_app.task(name="pms.rent.cycle")
def rent_cycle() -> dict:
    """Raise this month's bills, send due reminders, hold or release clinics.

    Scheduled only when `RENT_BILLING_ENABLED` is on; idempotent, so the
    schedule can be frequent and a missed run costs nothing but a delay.
    """
    summary = _run(lambda factory: rent.run_cycle(factory))
    if summary["raised"] or summary["held"] or summary["lifted"] or summary["failed"]:
        log.info("rent cycle: %s", summary)
    return summary


@celery_app.task(
    name="pms.reminders.send",
    bind=True,
    # Only for `missing`: the booking that queued this may not have committed
    # yet, or another delivery of this job holds the row. A failed send is not
    # a Celery retry — the row gets a backoff and a new job.
    max_retries=3,
    default_retry_delay=10,
)
def send_reminder(self, tenant_id: str, message_id: str) -> str:
    """Send one appointment reminder — if the row still names this job."""
    outcome = _run(
        lambda factory: reminders.send(
            factory, uuid.UUID(tenant_id), uuid.UUID(message_id), self.request.id
        )
    )
    if outcome == "missing" and self.request.retries < self.max_retries:
        raise self.retry()
    return outcome


@celery_app.task(name="pms.reminders.promote")
def promote_reminders() -> int:
    """The midnight run: queue each clinic's reminders for its new day."""
    queued = _run(lambda factory: reminders.promote(factory))
    if queued:
        log.info("queued %s reminder(s) for today", queued)
    return queued


@celery_app.task(name="pms.reminders.sweep")
def sweep_reminders() -> int:
    """Give a new job to every queued reminder whose job was lost."""
    requeued = _run(lambda factory: reminders.sweep(factory))
    if requeued:
        log.info("sweep re-queued %s reminder(s)", requeued)
    return requeued


def enqueue_support_request(tenant_id: uuid.UUID, request_id: uuid.UUID) -> bool:
    """Hand a new request to the worker. Never raises.

    The row is already written, so a broker that is down costs latency, not
    the request: the sweep finds it. The short countdown gives the creating
    transaction time to commit before the worker looks for the row.
    """
    try:
        deliver_support_request.apply_async(
            args=(str(tenant_id), str(request_id)), countdown=2, retry=False
        )
        return True
    except Exception as exc:  # noqa: BLE001 - deliberately swallowed, see docstring
        log.warning("could not queue support request %s, the sweep will: %s", request_id, exc)
        return False


def enqueue_import(tenant_id: uuid.UUID, job_id: uuid.UUID) -> bool:
    """Hand a stored upload to the worker. Never raises — the row is written,
    so a broker that is down costs a sweep's delay, not the import."""
    try:
        run_import.apply_async(args=(str(tenant_id), str(job_id)), countdown=1, retry=False)
        return True
    except Exception as exc:  # noqa: BLE001 - deliberately swallowed, see docstring
        log.warning("could not queue import %s, the sweep will: %s", job_id, exc)
        return False
