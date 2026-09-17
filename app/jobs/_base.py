"""What every billing job shares: which clinics to visit, and how.

One clinic at a time, each in its own transaction, each with `app.tenant_id`
bound to that clinic. Three consequences, all of them wanted:

*   row-level security applies to a cron job exactly as it does to a request,
    so a bug in a job cannot read across clinics any more than a bug in a
    handler can;
*   one clinic's failure rolls back one clinic's work. The other two hundred
    still get their invoices, which is the difference between "the billing run
    had a problem" and "nobody was billed this month";
*   the work is resumable. Every job is idempotent, so the fix for a partial
    run is to run it again.

"Today" is the clinic's own date, in the clinic's own timezone. A billing cycle
that turns over at midnight turns over at midnight *there* — a clinic in
Kolkata and one in London do not renew at the same instant, and pretending they
do would bill one of them a day early.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import session_factory, set_tenant
from app.models.enums import TenantStatus
from app.models.tenancy import Tenant
from app.schemas.common import zone

log = logging.getLogger("pms.billing.jobs")


@dataclass
class JobResult:
    """What a run did. Returned rather than logged alone, so a test — or an
    operator running a job by hand — can see what happened."""

    job: str
    tenants: int = 0
    processed: int = 0
    skipped: int = 0
    failures: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def __str__(self) -> str:  # pragma: no cover - log formatting
        return (
            f"{self.job}: {self.processed} done, {self.skipped} skipped, "
            f"{len(self.failures)} failed across {self.tenants} clinics"
        )


async def billable_tenants(tenant_id: uuid.UUID | None = None) -> list[tuple[uuid.UUID, str]]:
    """The clinics a billing run visits, with their timezones.

    `tenants` is the platform registry rather than a tenant-owned table, so
    this is the one query in the package that legitimately runs with nothing
    bound — the same exemption `messaging.worker._active_tenant_ids` takes.

    A suspended clinic is still billed. Suspension is a platform action about
    access, and a clinic that owes money does not stop owing it because
    somebody switched it off; the invoice is still raised and still chased.
    """
    async with session_factory() as session:
        stmt = select(Tenant.id, Tenant.timezone).order_by(Tenant.id)
        if tenant_id is not None:
            stmt = stmt.where(Tenant.id == tenant_id)
        rows = (await session.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]


async def active_tenants(tenant_id: uuid.UUID | None = None) -> list[tuple[uuid.UUID, str]]:
    """Clinics that are not suspended — for the jobs that talk to people."""
    async with session_factory() as session:
        stmt = (
            select(Tenant.id, Tenant.timezone)
            .where(Tenant.status != TenantStatus.SUSPENDED)
            .order_by(Tenant.id)
        )
        if tenant_id is not None:
            stmt = stmt.where(Tenant.id == tenant_id)
        rows = (await session.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]


def local_today(timezone_name: str) -> date:
    """The clinic's own date. Midnight is midnight where the clinic is."""
    return datetime.now(zone(timezone_name)).date()


@asynccontextmanager
async def tenant_work(tenant_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """One clinic, one transaction, that clinic bound."""
    async with session_factory() as session:
        async with session.begin():
            await set_tenant(session, tenant_id)
            yield session


async def for_each_tenant(
    job: str,
    work: Callable[[AsyncSession, uuid.UUID, date], Awaitable[int]],
    *,
    tenant_id: uuid.UUID | None = None,
    only_active: bool = False,
) -> JobResult:
    """Run `work` once per clinic, isolating failures.

    `work` returns how many things it did; zero counts as a skip, which is what
    makes "ran and found nothing to do" distinguishable from "ran and did
    something" in the result and in the log.
    """
    result = JobResult(job=job)
    tenants = await (active_tenants(tenant_id) if only_active else billable_tenants(tenant_id))

    for clinic_id, timezone_name in tenants:
        result.tenants += 1
        today = local_today(timezone_name)
        try:
            async with tenant_work(clinic_id) as session:
                done = await work(session, clinic_id, today)
        except Exception as exc:  # noqa: BLE001 - one clinic must not stop the run
            result.failures.append(f"{clinic_id}: {type(exc).__name__}: {exc}")
            log.exception("%s failed for tenant %s", job, clinic_id)
            continue
        if done:
            result.processed += done
        else:
            result.skipped += 1

    log.info("%s", result)
    return result
