"""GenerateMonthlyInvoicesJob — raise the invoice for any cycle that lacks one.

The renewal job normally raises an invoice as it turns the calendar over. This
one is the backstop: it walks every open cycle whose invoice is missing — one
skipped by a gateway outage, one whose transaction died halfway, one belonging
to a clinic created while billing was switched off — and raises it.

It cannot double-bill. `uq_subscription_invoice_per_cycle` allows exactly one
non-void invoice per cycle, and `generate_for_cycle` looks before it writes, so
the normal case is a read that finds the invoice already there.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs._base import JobResult, for_each_tenant
from app.models.billing import BillingCycle
from app.models.enums import BillingCycleStatus, SubscriptionStatus
from app.services.payment import billing_service
from app.services.payment.invoice import generation_service

log = logging.getLogger("pms.billing.jobs")

JOB = "GenerateMonthlyInvoicesJob"


async def _generate(session: AsyncSession, tenant_id: uuid.UUID, today: date) -> int:
    subscription = await billing_service.active_subscription(session, tenant_id, for_update=True)
    if subscription is None:
        return 0
    if subscription.status in (SubscriptionStatus.PAUSED, SubscriptionStatus.TRIALING):
        # A paused subscription bills nothing, and a trial's invoice is raised
        # when the trial ends rather than while it is running.
        return 0

    cycles = (
        await session.execute(
            select(BillingCycle)
            .where(
                BillingCycle.subscription_id == subscription.id,
                BillingCycle.status != BillingCycleStatus.SUPERSEDED,
                BillingCycle.period_start <= today,
            )
            .order_by(BillingCycle.sequence)
        )
    ).scalars()

    raised = 0
    for cycle in cycles:
        _, created = await generation_service.generate_for_cycle(
            session, subscription=subscription, cycle=cycle, today=today
        )
        if created:
            raised += 1
            log.info("backfilled invoice for cycle %s of tenant %s", cycle.sequence, tenant_id)
    return raised


async def run(tenant_id: uuid.UUID | None = None) -> JobResult:
    return await for_each_tenant(JOB, _generate, tenant_id=tenant_id)
