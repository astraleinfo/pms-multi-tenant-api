"""SyncRazorpaySubscriptionJob — reconcile with what the gateway believes.

Two jobs in one, and both are repair rather than routine:

*   a subscription that has no gateway id — created while the gateway was
    unreachable — is registered now;
*   every other live subscription has its `gateway_status` refreshed.

What it never does is change our own `status`. The gateway's word is recorded
beside ours and the two are allowed to disagree visibly: a webhook or a sync
saying "halted" is information for whoever reads the console, not permission to
stop serving a clinic whose invoices this system knows are paid. Reconciliation
that silently obeys the other system is how a paying customer gets cut off.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs._base import JobResult, for_each_tenant
from app.models.billing import Subscription
from app.services.payment import billing_service, subscription_service

log = logging.getLogger("pms.billing.jobs")

JOB = "SyncRazorpaySubscriptionJob"


async def _sync(session: AsyncSession, tenant_id: uuid.UUID, today: date) -> int:
    subscription = (
        await session.execute(
            select(Subscription).where(
                Subscription.tenant_id == tenant_id,
                Subscription.status.not_in(billing_service.DEAD_STATUSES),
            )
        )
    ).scalars().first()
    if subscription is None:
        return 0

    if not subscription.gateway_subscription_id:
        plan = await billing_service.get_plan(session, subscription.plan_id)
        await subscription_service.register_with_gateway(session, subscription, plan)
        return 1 if subscription.gateway_subscription_id else 0

    before = subscription.gateway_status
    await subscription_service.sync_from_gateway(session, subscription)
    return 1 if subscription.gateway_status != before else 0


async def run(tenant_id: uuid.UUID | None = None) -> JobResult:
    return await for_each_tenant(JOB, _sync, tenant_id=tenant_id, only_active=True)
