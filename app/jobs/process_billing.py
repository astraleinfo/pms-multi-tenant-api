"""ProcessBillingJob — the daily sweep that chases what is owed.

Three things, in order, per clinic:

1.  add-on cancellations whose day has arrived are applied, so a module that
    was scheduled to stop actually stops even if the renewal job has not run;
2.  an invoice past its due date plus the grace period moves the subscription
    to `past_due`;
3.  a clinic that has paid everything moves back out of `past_due`.

What it deliberately does **not** do is suspend anybody. Cutting a clinic off
from its own patient records is a decision a person makes, not a cron job:
`past_due` is the flag that tells somebody to look.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.jobs._base import JobResult, for_each_tenant
from app.models.enums import SubscriptionStatus
from app.services.payment import billing_service, payment_service, subscription_service

log = logging.getLogger("pms.billing.jobs")

JOB = "ProcessBillingJob"


async def _sweep(session: AsyncSession, tenant_id: uuid.UUID, today: date) -> int:
    subscription = await billing_service.active_subscription(session, tenant_id, for_update=True)
    if subscription is None:
        return 0

    actions = 0
    ended = await subscription_service.apply_scheduled_cancellations(
        session, subscription, on=today
    )
    actions += len(ended)

    overdue = await payment_service.overdue_invoices(
        session, tenant_id, on=today, grace_days=settings.billing_grace_days
    )
    if overdue and subscription.status == SubscriptionStatus.ACTIVE:
        await subscription_service.mark_past_due(session, subscription)
        log.info(
            "tenant %s is past due on %s invoice(s): %s",
            tenant_id, len(overdue), ", ".join(i.number for i in overdue),
        )
        actions += 1
    elif not overdue and subscription.status == SubscriptionStatus.PAST_DUE:
        await subscription_service.clear_past_due(session, subscription)
        actions += 1

    return actions


async def run(tenant_id: uuid.UUID | None = None) -> JobResult:
    return await for_each_tenant(JOB, _sweep, tenant_id=tenant_id)
