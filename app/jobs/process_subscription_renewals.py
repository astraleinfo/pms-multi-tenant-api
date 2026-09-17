"""ProcessSubscriptionRenewalsJob — turn the billing calendar over.

Closes the cycle that has ended, applies the add-on and subscription
cancellations scheduled for its end, opens the next cycle and invoices it.
Everything it does is in `subscription_service.renew`, which is idempotent
throughout; this module is the per-clinic scheduling around it.

Run it daily. Running it hourly is harmless — a clinic whose cycle has not
ended yet is a no-op, and one whose cycle ended three months ago is caught up
one real month at a time rather than in a single long period.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs._base import JobResult, for_each_tenant
from app.services.payment import billing_service, subscription_service

log = logging.getLogger("pms.billing.jobs")

JOB = "ProcessSubscriptionRenewalsJob"


async def _renew(session: AsyncSession, tenant_id: uuid.UUID, today: date) -> int:
    # The row lock is what makes two overlapping runs safe: the second waits,
    # then finds the cycle already opened and the invoice already raised.
    subscription = await billing_service.active_subscription(session, tenant_id, for_update=True)
    if subscription is None:
        return 0

    before = subscription.status
    result = await subscription_service.renew(session, subscription, today=today)
    if result.invoice is None and before == subscription.status:
        return 0
    return 1


async def run(tenant_id: uuid.UUID | None = None) -> JobResult:
    return await for_each_tenant(JOB, _renew, tenant_id=tenant_id)
