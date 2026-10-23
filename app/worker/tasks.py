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
from app.models.enums import AuthTokenPurpose
from app.services import import_jobs, invitations, rent, support
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
    # yet. A mail failure is not a Celery retry — the row is marked failed and
    # the desk sends it again from the console.
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


#: How a link email's purpose reads on the wire and in the job monitor. The
#: stored value stays `reset`; a job queued before this said so, and still runs.
PURPOSE_ON_WIRE = {AuthTokenPurpose.INVITE: "invite", AuthTokenPurpose.RESET: "reset_password"}
_PURPOSE_FROM_WIRE = {v: k for k, v in PURPOSE_ON_WIRE.items()} | {p.value: p for p in AuthTokenPurpose}


@celery_app.task(
    name="pms.invitations.deliver",
    bind=True,
    # Only for `missing`: the request that queued this may not have committed
    # yet. A mail failure is not a Celery retry — the account is marked failed
    # and Resend sends it again.
    max_retries=3,
    default_retry_delay=5,
)
def deliver_link_email(
    self,
    tenant_id: str,
    user_id: str,
    purpose: str,
    issued_by_label: str = "",
    ip: str = "",
    user_agent: str = "",
) -> str:
    """An invitation or reset email. Who to write to, never the link: the worker
    mints that itself, so the plaintext is not in the broker or the monitor."""
    outcome = _run(
        lambda factory: invitations.deliver(
            factory, uuid.UUID(tenant_id), uuid.UUID(user_id), _PURPOSE_FROM_WIRE[purpose],
            issued_by_label=issued_by_label, ip=ip, user_agent=user_agent,
        )
    )
    if outcome == "missing" and self.request.retries < self.max_retries:
        raise self.retry()
    return outcome


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


def enqueue_support_request(tenant_id: uuid.UUID, request_id: uuid.UUID) -> bool:
    """Hand a request to the worker. Never raises.

    The row is already written, so a broker that is down costs the email, not
    the request: it stays `queued`, turns retryable, and the desk sends it from
    the console. The short countdown gives the creating transaction time to
    commit before the worker looks for the row.
    """
    try:
        deliver_support_request.apply_async(
            kwargs={"tenant_id": str(tenant_id), "request_id": str(request_id)}, countdown=2, retry=False
        )
        return True
    except Exception as exc:  # noqa: BLE001 - deliberately swallowed, see docstring
        log.warning("could not queue support request %s, it waits for a retry: %s", request_id, exc)
        return False


def enqueue_link_email(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    purpose: AuthTokenPurpose,
    *,
    issued_by_label: str = "",
    ip: str = "",
    user_agent: str = "",
) -> bool:
    """Hand an invitation or reset email to the worker. Never raises.

    The account is already marked `queued`, so a broker that is down costs the
    email, not the account: it turns retryable and Resend sends it. The short
    countdown gives the queuing transaction time to commit.
    """
    try:
        who = {"tenant_id": str(tenant_id), "user_id": str(user_id), "purpose": PURPOSE_ON_WIRE[purpose]}
        deliver_link_email.apply_async(
            kwargs={**who, "issued_by_label": issued_by_label, "ip": ip, "user_agent": user_agent},
            # What the job monitor shows. The requester's address and browser
            # go to the worker, which writes them on the link's row; they are
            # not put on display beside every job.
            kwargsrepr=repr({**who, "requested_by": issued_by_label or "the account holder"}),
            countdown=1,
            retry=False,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - deliberately swallowed, see docstring
        log.warning(
            "could not queue %s email for %s, it waits for a resend: %s",
            PURPOSE_ON_WIRE[purpose], user_id, exc,
        )
        return False


def enqueue_link_emails(
    tenant_id: uuid.UUID, user_ids: list[uuid.UUID], *, issued_by_label: str = ""
) -> int:
    """Invitations for a batch of new accounts — a staff import. Returns how
    many reached the broker; the rest wait for a resend, like any other."""
    return sum(
        enqueue_link_email(tenant_id, user_id, AuthTokenPurpose.INVITE, issued_by_label=issued_by_label)
        for user_id in user_ids
    )


def enqueue_import(tenant_id: uuid.UUID, job_id: uuid.UUID) -> bool:
    """Hand a stored upload to the worker. Never raises — the row is written,
    so a broker that is down leaves the job `queued` until the person retries
    it, and loses nothing."""
    try:
        run_import.apply_async(
            kwargs={"tenant_id": str(tenant_id), "job_id": str(job_id)}, countdown=1, retry=False
        )
        return True
    except Exception as exc:  # noqa: BLE001 - deliberately swallowed, see docstring
        log.warning("could not queue import %s, it waits for a retry: %s", job_id, exc)
        return False
