"""The background jobs: idempotent, retryable, and isolated per clinic.

Every job here is run twice in a row and the database is checked to be the same
afterwards. That is not a stylistic preference — a cron job that is retried
after a timeout, or run by two workers at once, must not produce a second
invoice or a second charge, and the only way to know is to do it.

The isolation test is the other half: one clinic failing must not stop the
others being billed. A month of missing invoices because one clinic had a bad
row would be a far worse failure than the bad row.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.session import session_factory, tenant_session
from app.jobs import (
    JOBS,
    generate_monthly_invoices,
    process_billing,
    process_razorpay_webhook,
    process_subscription_renewals,
    send_invoice_email,
)
from app.models.billing import SubscriptionInvoice, WebhookEvent
from app.models.enums import (
    PaymentGateway,
    SubscriptionStatus,
    TenantFeatureStatus,
    WebhookStatus,
)
from app.services.payment import billing_service, payment_service, subscription_service
from app.services.payment.addon import billing_service as addon_service
from tests.billing_helpers import (
    STARTER,
    entitlement,
    invoices_of,
    make_tenant,
    plan,
    subscription_of,
)

pytestmark = pytest.mark.usefixtures("seeded")

JAN_1 = date(2026, 1, 1)


async def _clinic(label: str, *, on: date = JAN_1):
    """A subscribed clinic whose cycle started today, in the clinic's own zone.

    The jobs read "today" from the clinic's timezone, so a test that wants a
    job to *do* something has to put the clinic's calendar where the real clock
    is — hence `on=today` for the job tests rather than a fixed January.
    """
    tenant_id = await make_tenant(label)
    starter = await plan(STARTER)
    async with tenant_session(tenant_id) as session:
        await subscription_service.subscribe(
            session, tenant_id, plan=starter, today=on, trial_days=0
        )
    return tenant_id


def _today() -> date:
    """What the jobs will call today for a Europe/London clinic."""
    from app.jobs._base import local_today

    return local_today("Europe/London")


async def test_every_named_job_exists_and_is_callable():
    """The six the requirements name, by the names they were given."""
    assert set(JOBS) == {
        "GenerateMonthlyInvoicesJob",
        "ProcessBillingJob",
        "ProcessSubscriptionRenewalsJob",
        "SendInvoiceEmailJob",
        "ProcessRazorpayWebhookJob",
        "SyncRazorpaySubscriptionJob",
    }
    for run in JOBS.values():
        assert callable(run)


async def test_the_renewal_job_is_idempotent():
    """Run it four times on the same day; one cycle turns over, once."""
    started = _today() - timedelta(days=45)
    tenant_id = await _clinic("job-renew", on=started)

    for _ in range(4):
        result = await process_subscription_renewals.run(tenant_id=tenant_id)
        assert result.ok, result.failures

    invoices = await invoices_of(tenant_id)
    periods = [(i.period_start, i.period_end) for i in invoices]
    assert len(periods) == len(set(periods)), "no period is invoiced twice"
    assert len({i.number for i in invoices}) == len(invoices), "no number is reused"


async def test_the_invoice_generator_backfills_and_then_does_nothing():
    """The backstop for a cycle whose invoice never got raised.

    The invoice is voided to simulate the gap, then the job fills it — and the
    second run finds nothing to do, which is the property that lets it run on a
    timer.
    """
    from app.services.payment.invoice import generation_service

    tenant_id = await _clinic("job-backfill", on=_today())
    first = (await invoices_of(tenant_id))[0]

    async with tenant_session(tenant_id) as session:
        await generation_service.void_invoice(session, tenant_id, first.id)

    filled = await generate_monthly_invoices.run(tenant_id=tenant_id)
    assert filled.processed == 1, "the gap is filled"

    again = await generate_monthly_invoices.run(tenant_id=tenant_id)
    assert again.processed == 0, "and not filled twice"

    live = [i for i in await invoices_of(tenant_id) if str(i.status) != "void"]
    assert len(live) == 1


async def test_the_billing_sweep_applies_scheduled_cancellations():
    """A module scheduled to stop actually stops, even if the renewal job has
    not run."""
    today = _today()
    tenant_id = await _clinic("job-sweep-cancel", on=today - timedelta(days=40))

    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=today - timedelta(days=40))
    async with tenant_session(tenant_id) as session:
        result = await addon_service.cancel(
            session, tenant_id, "lab", today=today - timedelta(days=39)
        )
        assert result.entitlement.cancel_at <= today, "the cancellation is already due"

    swept = await process_billing.run(tenant_id=tenant_id)
    assert swept.ok, swept.failures
    assert (await entitlement(tenant_id, "lab")).status == TenantFeatureStatus.INACTIVE

    # Running it again changes nothing.
    again = await process_billing.run(tenant_id=tenant_id)
    assert again.processed == 0


async def test_the_billing_sweep_marks_an_overdue_clinic_past_due_without_suspending_it():
    """`past_due` is a flag for a person to act on, not an automatic cut-off.

    Suspending a clinic takes away its access to its own patient records, and
    that is never a decision a cron job gets to make.
    """
    today = _today()
    overdue_by = settings.billing_payment_terms_days + settings.billing_grace_days + 5
    tenant_id = await _clinic("job-past-due", on=today - timedelta(days=overdue_by))

    result = await process_billing.run(tenant_id=tenant_id)
    assert result.ok, result.failures

    subscription = await subscription_of(tenant_id)
    assert subscription.status == SubscriptionStatus.PAST_DUE

    # Entitlements are untouched — the clinic keeps working.
    assert (await entitlement(tenant_id, "patient")).status == TenantFeatureStatus.ACTIVE

    # Paying clears it.
    invoice = (await invoices_of(tenant_id))[0]
    async with tenant_session(tenant_id) as session:
        row = await payment_service.get_invoice(session, tenant_id, invoice.id)
        await payment_service.record_capture(
            session,
            invoice=row,
            gateway=PaymentGateway.RAZORPAY,
            gateway_payment_id="pay_job_clears",
            amount=Decimal(str(invoice.total)),
        )

    await process_billing.run(tenant_id=tenant_id)
    assert (await subscription_of(tenant_id)).status == SubscriptionStatus.ACTIVE


async def test_an_invoice_with_nobody_to_send_it_to_is_left_unsent():
    """A clinic with no billing address and no active administrator.

    Not an error and not a silent success: the invoice keeps `emailed_at` null
    so the next sweep tries again once somebody has an address, and the job
    logs which clinic it could not reach.
    """
    tenant_id = await _clinic("job-email-nobody", on=_today())
    result = await send_invoice_email.run(tenant_id=tenant_id)
    assert result.ok
    assert result.processed == 0

    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                select(SubscriptionInvoice).where(SubscriptionInvoice.tenant_id == tenant_id)
            )
        ).scalars().first()
    assert row.emailed_at is None, "it will be tried again"


async def test_the_invoice_email_job_sends_once_and_can_be_forced_to_resend():
    """Idempotent by `emailed_at`, so a retry does not spam a practice manager
    — and a reminder is therefore a deliberate act."""
    tenant_id = await _clinic("job-email", on=_today())
    async with tenant_session(tenant_id) as session:
        account = await billing_service.get_account(session, tenant_id)
        account.billing_email = "accounts@example.test"

    first = await send_invoice_email.run(tenant_id=tenant_id)
    assert first.processed == 1, first.failures

    second = await send_invoice_email.run(tenant_id=tenant_id)
    assert second.processed == 0, "a second sweep sends nothing"

    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                select(SubscriptionInvoice).where(SubscriptionInvoice.tenant_id == tenant_id)
            )
        ).scalars().first()
        assert row.emailed_at is not None

    forced = await send_invoice_email.run(tenant_id=tenant_id, force=True)
    assert forced.processed == 1, "a reminder is possible, but only on purpose"


async def test_the_invoice_email_goes_to_the_billing_address_when_one_is_set():
    tenant_id = await _clinic("job-email-to", on=_today())
    async with tenant_session(tenant_id) as session:
        account = await billing_service.get_account(session, tenant_id)
        account.billing_email = "accounts@example.test"

    async with tenant_session(tenant_id) as session:
        to = await send_invoice_email.recipients(session, tenant_id)
    assert to == ["accounts@example.test"]


async def test_the_webhook_job_drains_the_queue_and_leaves_nothing_behind():
    """Rejected and unsupported deliveries are skipped, not retried forever."""
    async with session_factory() as session:
        async with session.begin():
            session.add(
                WebhookEvent(
                    gateway=PaymentGateway.RAZORPAY,
                    event_id=f"evt_job_{uuid.uuid4().hex[:8]}",
                    event_type="payment.captured",
                    entity_id="pay_nowhere",
                    signature_verified=True,
                    status=WebhookStatus.RECEIVED,
                    payload={
                        "event": "payment.captured",
                        "payload": {"payment": {"entity": {"id": "pay_nowhere", "notes": {}}}},
                    },
                )
            )

    result = await process_razorpay_webhook.run()
    assert result.ok, result.failures
    # It named nothing this deployment owns, so it is ignored rather than
    # failed — and never looked at again.
    assert result.skipped >= 1

    async with session_factory() as session:
        pending = (
            await session.execute(
                select(WebhookEvent).where(WebhookEvent.status == WebhookStatus.RECEIVED)
            )
        ).scalars().all()
    assert pending == [], "the queue is drained"


async def test_a_job_that_fails_for_one_clinic_still_serves_the_others(monkeypatch):
    """The isolation property. One bad clinic is one bad clinic.

    The failure is injected rather than contrived out of bad data, because the
    thing being tested is the *loop* — that an exception from one clinic's work
    is caught, recorded and stepped over — not any particular way of causing
    one.
    """
    good = await _clinic("job-isolation-good", on=_today() - timedelta(days=40))
    broken = await _clinic("job-isolation-broken", on=_today() - timedelta(days=40))

    real_renew = subscription_service.renew

    async def explode(session, subscription, *, today):
        if subscription.tenant_id == broken:
            raise RuntimeError("something went wrong for this clinic")
        return await real_renew(session, subscription, today=today)

    monkeypatch.setattr(
        process_subscription_renewals.subscription_service, "renew", explode
    )

    result = await process_subscription_renewals.run()
    assert result.failures, "the broken clinic is reported"
    assert any(str(broken) in failure for failure in result.failures)
    assert result.processed >= 1, "…and the others were still billed"

    assert len(await invoices_of(good)) >= 1


async def test_a_job_run_reports_what_it_did():
    """The result is returned rather than only logged, so an operator running a
    job by hand can see the outcome."""
    tenant_id = await _clinic("job-result", on=_today())
    result = await process_billing.run(tenant_id=tenant_id)
    assert result.job == "ProcessBillingJob"
    assert result.tenants == 1
    assert result.ok
    assert "ProcessBillingJob" in str(result)
