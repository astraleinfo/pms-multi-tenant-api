"""Spreadsheet imports, run in the background.

The upload is answered as soon as the file is stored; the Celery worker checks
it, writes it or says why not, and deletes the file whichever way it went — a
clinic's staff list or price list is not kept a moment longer than the import
needs it. The job row is what is left: its status, and a report.

One runner, several kinds. A kind is its columns (what the sheet must carry,
checked at the upload so an unreadable file is refused at once) and its
`process` (the rows checked and written in one transaction, or `Rejected` with
the rows that need fixing). Inventory and staff are the kinds today; see
`KINDS`.

A worker that dies mid-import leaves the row `running` with the file still in
place, and the redelivered job starts it again — the import is one
transaction, so a half-written one was never committed. One still `queued`
well after its hand-off was lost on the way to the broker; it shows as
`retryable`, and the person hands it over again with `retry`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.errors import Conflict, ValidationFailed
from app.db.session import set_tenant
from app.models.enums import ImportJobStatus
from app.models.imports import ImportJob
from app.models.tenancy import Tenant
from app.services import storage
from app.services.imports import MAX_FILE_BYTES, Column, read_rows

log = logging.getLogger("pms.imports")


@dataclass(frozen=True)
class Actor:
    """Who asked for the import — what the audit trail records it against."""

    id: uuid.UUID | None
    label: str
    kind: str


class Rejected(Exception):
    """The sheet was read, and some rows did not pass. Nothing was written."""

    def __init__(self, report: dict[str, Any]):
        super().__init__("rows need fixing")
        self.report = report


Process = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Kind:
    columns: tuple[Column, ...]
    #: `(session, tenant, rows, *, filename, actor) -> report`, raising `Rejected`
    process: Process
    #: for the log, when something unexpected goes wrong
    noun: str


def kinds() -> dict[str, Kind]:
    """Imported here, not at the top: each kind's module imports this one."""
    from app.services import inventory_io, staff_io

    return {
        inventory_io.KIND: Kind(inventory_io.COLUMNS, inventory_io.process, "inventory import"),
        staff_io.KIND: Kind(staff_io.COLUMNS, staff_io.process, "staff import"),
    }


def _key(tenant_id: uuid.UUID, job_id: uuid.UUID) -> str:
    """Where an upload waits in the bucket. Built from ids the server minted."""
    prefix = settings.s3_prefix.strip("/")
    path = f"tenants/{tenant_id}/imports/{job_id}"
    return f"{prefix}/{path}" if prefix else path


# --- the request's half -----------------------------------------------------------------------


async def queue(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    kind: str,
    *,
    filename: str,
    content: bytes,
    actor: Actor,
) -> ImportJob:
    """Store an upload for the worker.

    Is it the sheet at all? That is read now, in a thread — empty, too big, not
    a spreadsheet, a different sheet's columns, no rows, too many — so the
    person is answered at once rather than by a job that fails a moment later.
    The worker reads it again with the same function, so the two cannot
    disagree.
    """
    spec = kinds()[kind]
    if not content:
        raise ValidationFailed("That file is empty.")
    if len(content) > MAX_FILE_BYTES:
        raise ValidationFailed(f"Files are limited to {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    if not filename.lower().endswith((".xlsx", ".csv")):
        raise ValidationFailed("Upload the .xlsx sample sheet, or a .csv with the same columns.")
    await run_in_threadpool(read_rows, content, filename, spec.columns, kind=kind)

    job = ImportJob(
        tenant_id=tenant_id,
        kind=kind,
        status=ImportJobStatus.QUEUED,
        filename=filename[:255],
        size=len(content),
        requested_by_id=actor.id,
        requested_by_label=actor.label[:160],
        actor_kind=actor.kind,
    )
    session.add(job)
    await session.flush()
    if storage.uses_s3():
        job.storage_key = _key(tenant_id, job.id)
        await storage.put(job.storage_key, content, "application/octet-stream")
    else:
        job.data = content
    await session.flush()
    return job


async def latest(session: AsyncSession, tenant_id: uuid.UUID, kind: str) -> ImportJob | None:
    return (
        await session.execute(
            select(ImportJob)
            .where(ImportJob.tenant_id == tenant_id, ImportJob.kind == kind)
            .order_by(ImportJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_job(
    session: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID, kind: str
) -> ImportJob | None:
    """One job of one kind — the staff routes never answer with a stock import."""
    return (
        await session.execute(
            select(ImportJob).where(
                ImportJob.tenant_id == tenant_id, ImportJob.id == job_id, ImportJob.kind == kind
            )
        )
    ).scalar_one_or_none()


#: How long a job may sit `queued` after its hand-off before it counts as lost.
#: Longer than the enqueue's countdown, so it never races a hand-off that is
#: still on its way.
STUCK_AFTER = timedelta(seconds=30)


def retryable(job: ImportJob) -> bool:
    """Queued long enough that its hand-off to the broker must have been lost.

    A failed import is not retried: its file is gone, and what it needs is a
    corrected sheet, not another run of the same one.
    """
    if job.status != ImportJobStatus.QUEUED:
        return False
    cutoff = datetime.now(timezone.utc) - STUCK_AFTER
    # `created_at` first: a job this new is never stuck, and the check then
    # never reaches `updated_at`, which a fresh upload may not have loaded.
    return job.created_at <= cutoff and job.updated_at <= cutoff


async def retry(session: AsyncSession, job: ImportJob) -> ImportJob:
    """Mark a stuck job as handed over again. The caller enqueues it once this
    transaction has committed."""
    if not retryable(job):
        raise Conflict("This import is not stuck — it is running or already finished.")
    # What restarts the stuck clock; set here rather than left to `onupdate`
    # so the response can read it without another round trip.
    job.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return job


# --- the worker's half ------------------------------------------------------------------------


def _failure(report: dict[str, Any]) -> str:
    dupes = report.get("duplicates", 0)
    others = report["invalid"] - dupes
    parts = [f"{dupes} duplicate(s)" if dupes else "", f"{others} other row(s) to fix" if others else ""]
    return f"{' and '.join(p for p in parts if p)} — nothing was imported."


async def run(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, job_id: uuid.UUID) -> str:
    """Check and write one uploaded sheet, then delete the upload.

    Returns `done`, `failed`, `skipped` (already finished — a redelivered job)
    or `missing` (not visible yet: the upload's transaction has not committed).

    Three transactions. The first claims the job and reads the file. The
    second is the import — every row or none. The third records the outcome
    and clears the file; it runs whatever the second did, so a failed import
    never leaves its sheet behind.
    """
    async with factory() as session:
        async with session.begin():
            await set_tenant(session, tenant_id)
            job = (
                await session.execute(
                    select(ImportJob).where(ImportJob.id == job_id).with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if job is None:
                return "missing"
            if job.status in (ImportJobStatus.DONE, ImportJobStatus.FAILED):
                return "skipped"
            job.status = ImportJobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc)
            job.attempts += 1
            key, filename, kind = job.storage_key, job.filename, job.kind
            actor = Actor(job.requested_by_id, job.requested_by_label, job.actor_kind)
            content = (
                await session.execute(select(ImportJob.data).where(ImportJob.id == job_id))
            ).scalar_one()

    report: dict[str, Any] | None = None
    error = ""
    spec = kinds().get(kind)
    try:
        if spec is None:
            raise ValidationFailed("This kind of import is not supported any more.")
        if key:
            content = await storage.get(key)
        async with factory() as session:
            async with session.begin():
                await set_tenant(session, tenant_id)
                tenant = await session.get(Tenant, tenant_id)
                rows = read_rows(content or b"", filename, spec.columns, kind=kind)
                report = await spec.process(session, tenant, rows, filename=filename, actor=actor)
    except Rejected as rejected:
        report = rejected.report
        error = _failure(report)
    except ValidationFailed as exc:
        error = exc.message  # the sheet could not be read at all — one sentence says why
    except Exception:  # noqa: BLE001 - any other failure is the same answer to the person
        log.exception("%s %s failed", spec.noun if spec else "import", job_id)
        report = None
        error = "The import could not be completed. Nothing was imported — try again."

    outcome = ImportJobStatus.FAILED if error else ImportJobStatus.DONE
    async with factory() as session:
        async with session.begin():
            await set_tenant(session, tenant_id)
            job = await session.get(ImportJob, job_id)
            job.status = outcome
            job.error = error
            job.report = report
            job.finished_at = datetime.now(timezone.utc)
            job.data = None
            job.storage_key = None
    if key:
        try:
            await storage.delete(key)
        except Exception as exc:  # noqa: BLE001 - the row no longer points at it; log and move on
            log.error("could not delete import upload %s: %s", key, exc)
    return outcome.value
