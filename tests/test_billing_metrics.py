"""Billing metrics, and who is allowed to see them.

Two things are being checked, and the second matters as much as the first:

*   the figures are right — MRR counts what will bill next month rather than
    what billed last month, revenue counts money that actually arrived, and a
    paused or cancelling subscription drops out of MRR immediately;
*   the numbers are gathered without a cross-tenant read. `report` binds each
    clinic in turn, so the console is a privileged *view* and not a privileged
    *query* — and the authorisation tests confirm a clinic cannot reach it.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.db.session import session_factory, tenant_session
from app.models.billing import SubscriptionPayment
from app.models.enums import PaymentGateway, RefundStatus, SubscriptionStatus
from app.services.payment import billing_service, payment_service, subscription_service
from app.services.payment.addon import billing_service as addon_service
from app.services.payment.metrics import billing_metrics_service as metrics_service
from tests.billing_helpers import (
    LAB_MONTHLY,
    STARTER,
    invoices_of,
    make_tenant,
    plan,
    subscription_of,
)
from tests.conftest import CONSOLE, NORTHGATE

pytestmark = pytest.mark.usefixtures("seeded")

JAN_1 = date(2026, 1, 1)
FEB_1 = date(2026, 2, 1)


async def _clinic(label: str, *, code: str = STARTER, on: date = JAN_1):
    tenant_id = await make_tenant(label)
    chosen = await plan(code)
    async with tenant_session(tenant_id) as session:
        await subscription_service.subscribe(
            session, tenant_id, plan=chosen, today=on, trial_days=0
        )
    return tenant_id, chosen


async def _report(**filters) -> metrics_service.BillingMetrics:
    async with session_factory() as session:
        async with session.begin():
            return await metrics_service.report(
                session, metrics_service.MetricsFilter(**filters)
            )


async def test_mrr_is_the_plan_plus_the_addons_that_are_staying():
    tenant_id, starter = await _clinic("met-mrr")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)

    report = await _report(tenant_id=tenant_id)
    assert report.mrr == Decimal(str(starter.amount)) + LAB_MONTHLY
    assert report.arr == report.mrr * 12
    assert report.subscriptions_active == 1

    row = report.tenants[0]
    assert row.tenant_id == tenant_id
    assert row.active_addons == 1
    assert row.mrr == report.mrr


async def test_an_addon_cancelling_at_the_cycle_end_leaves_mrr_immediately():
    """MRR is what will bill next month. A module scheduled to stop will not,
    so it drops out the moment the cancellation is scheduled — even though the
    clinic is still using it and still paid for it."""
    tenant_id, starter = await _clinic("met-cancelling")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
    async with tenant_session(tenant_id) as session:
        await addon_service.cancel(session, tenant_id, "lab", today=date(2026, 1, 20))

    report = await _report(tenant_id=tenant_id)
    assert report.mrr == Decimal(str(starter.amount)), "the lab is on its way out"

    lab = [a for a in report.addons if a.feature_key == "lab"][0]
    assert lab.active_tenants == 1, "still switched on"
    assert lab.cancelling == 1, "…and on notice"
    assert lab.monthly_revenue == Decimal("0.00"), "contributing nothing to next month"


async def test_a_paused_subscription_leaves_mrr_but_stays_counted():
    tenant_id, _ = await _clinic("met-paused")
    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        await subscription_service.pause(session, row)

    report = await _report(tenant_id=tenant_id)
    assert report.mrr == Decimal("0.00")
    assert report.subscriptions_paused == 1
    assert report.subscriptions_active == 0


async def test_revenue_counts_captured_money_and_refunds_come_off_separately():
    tenant_id, _ = await _clinic("met-revenue")
    invoice = (await invoices_of(tenant_id))[0]
    total = Decimal(str(invoice.total))

    async with tenant_session(tenant_id) as session:
        row = await payment_service.get_invoice(session, tenant_id, invoice.id)
        payment = await payment_service.record_capture(
            session,
            invoice=row,
            gateway=PaymentGateway.RAZORPAY,
            gateway_payment_id="pay_metrics_1",
            amount=total,
        )
        payment_id = payment.id

    report = await _report(tenant_id=tenant_id)
    assert report.revenue == total
    assert report.payments_successful == 1
    assert report.payment_success_rate == 1.0
    assert report.invoices_outstanding == 0
    assert report.invoices_paid == 1

    async with tenant_session(tenant_id) as session:
        await payment_service.record_refund(
            session,
            payment=await session.get(SubscriptionPayment, payment_id),
            gateway_refund_id="rfnd_metrics_1",
            amount=Decimal("100.00"),
            status=RefundStatus.PROCESSED,
        )

    report = await _report(tenant_id=tenant_id)
    assert report.revenue == total, "what was collected is still what was collected"
    assert report.refunded == Decimal("100.00")
    assert report.net_revenue == total - Decimal("100.00")
    assert report.refunds_count == 1


async def test_failed_payments_lower_the_success_rate_without_touching_revenue():
    tenant_id, _ = await _clinic("met-failures")
    invoice = (await invoices_of(tenant_id))[0]

    async with tenant_session(tenant_id) as session:
        row = await payment_service.get_invoice(session, tenant_id, invoice.id)
        for index in range(3):
            await payment_service.record_failure(
                session,
                invoice=row,
                gateway=PaymentGateway.RAZORPAY,
                gateway_payment_id=f"pay_metrics_fail_{index}",
                amount=Decimal(str(invoice.total)),
                reason="declined",
            )
        await payment_service.record_capture(
            session,
            invoice=row,
            gateway=PaymentGateway.RAZORPAY,
            gateway_payment_id="pay_metrics_ok",
            amount=Decimal(str(invoice.total)),
        )

    report = await _report(tenant_id=tenant_id)
    assert report.payments_failed == 3
    assert report.payments_successful == 1
    assert report.payment_success_rate == 0.25
    assert report.revenue == Decimal(str(invoice.total))


async def test_outstanding_describes_now_and_ignores_the_date_filter():
    """An unpaid invoice from January is still unpaid in June, whatever window
    the console is looking at."""
    tenant_id, _ = await _clinic("met-outstanding")
    invoice = (await invoices_of(tenant_id))[0]

    far_future = await _report(
        tenant_id=tenant_id, start=date(2030, 1, 1), end=date(2030, 12, 31)
    )
    assert far_future.invoices_outstanding == 1
    assert far_future.outstanding_total == Decimal(str(invoice.total))
    assert far_future.revenue == Decimal("0.00"), "…but nothing was collected in that window"
    assert far_future.invoices_issued == 0, "and nothing was issued in it either"


async def test_new_and_cancelled_subscriptions_are_counted_in_the_window():
    tenant_id, _ = await _clinic("met-movement")

    inside = await _report(tenant_id=tenant_id, start=JAN_1, end=date(2026, 1, 31))
    assert inside.subscriptions_new == 1
    assert inside.subscriptions_cancelled == 0

    async with tenant_session(tenant_id) as session:
        row = await billing_service.require_subscription(session, tenant_id, for_update=True)
        await subscription_service.cancel(session, row, today=date(2026, 1, 20), immediately=True)

    after = await _report(tenant_id=tenant_id, start=JAN_1, end=date(2026, 1, 31))
    assert after.subscriptions_cancelled == 1
    assert after.mrr == Decimal("0.00"), "a cancelled clinic bills nothing next month"
    assert (await subscription_of(tenant_id)).status == SubscriptionStatus.CANCELLED

    outside = await _report(tenant_id=tenant_id, start=FEB_1, end=date(2026, 2, 28))
    assert outside.subscriptions_new == 0
    assert outside.subscriptions_cancelled == 0


async def test_addon_adoption_and_revenue_are_reported_per_module():
    """Requirement: add-on adoption and revenue. Both, and they are different
    questions — one is how many clinics have it, the other is what it earned."""
    first, _ = await _clinic("met-addon-a")
    second, _ = await _clinic("met-addon-b")
    for tenant_id in (first, second):
        async with tenant_session(tenant_id) as session:
            await addon_service.activate(session, tenant_id, "lab", today=JAN_1)
    async with tenant_session(first) as session:
        await addon_service.activate(session, first, "pharmacy", today=JAN_1)

    report = await _report(start=JAN_1, end=date(2026, 1, 31))
    by_key = {a.feature_key: a for a in report.addons}

    assert by_key["lab"].active_tenants >= 2
    assert by_key["lab"].monthly_revenue >= LAB_MONTHLY * 2
    assert by_key["lab"].billed_in_window >= LAB_MONTHLY * 2
    assert by_key["pharmacy"].active_tenants >= 1
    # Ordered by adoption, so the console's table leads with what sells.
    assert report.addons == sorted(
        report.addons, key=lambda a: (-a.active_tenants, a.feature_key)
    )


async def test_filtering_by_feature_narrows_the_rows_not_the_totals():
    """"How is the lab doing" is a question about the lab, not a claim that the
    lab is all the revenue there is."""
    tenant_id, starter = await _clinic("met-filter-feature")
    async with tenant_session(tenant_id) as session:
        await addon_service.activate(session, tenant_id, "lab", today=JAN_1)

    filtered = await _report(tenant_id=tenant_id, feature_key="lab")
    assert [a.feature_key for a in filtered.addons] == ["lab"]
    assert filtered.mrr == Decimal(str(starter.amount)) + LAB_MONTHLY


async def test_filtering_by_plan_and_status_excludes_clinics_entirely():
    starter_clinic, starter = await _clinic("met-filter-starter")
    clinic_clinic, clinic_plan = await _clinic("met-filter-clinic", code="clinic")

    only_starter = await _report(plan_id=starter.id)
    ids = {row.tenant_id for row in only_starter.tenants}
    assert starter_clinic in ids
    assert clinic_clinic not in ids

    async with tenant_session(clinic_clinic) as session:
        row = await billing_service.require_subscription(session, clinic_clinic, for_update=True)
        await subscription_service.pause(session, row)

    paused_only = await _report(subscription_status=SubscriptionStatus.PAUSED)
    assert clinic_clinic in {row.tenant_id for row in paused_only.tenants}
    assert starter_clinic not in {row.tenant_id for row in paused_only.tenants}


async def test_a_clinic_with_no_subscription_is_not_in_the_population():
    """Not a zero in the average — simply not counted. A clinic that has never
    subscribed would otherwise drag every per-clinic figure down."""
    tenant_id = await make_tenant("met-unsubscribed")
    report = await _report(tenant_id=tenant_id)
    assert report.tenants == []
    assert report.mrr == Decimal("0.00")


# --- who may look ------------------------------------------------------------


async def test_the_console_can_read_the_metrics(console):
    response = await console.get("/v1/platform/billing/metrics")
    assert response.status_code == 200, response.text
    body = response.json()
    assert {"mrr", "arr", "revenue", "addons", "tenants"} <= set(body)
    assert isinstance(body["payment_success_rate"], float)


async def test_a_clinic_cannot_read_the_platform_metrics(northgate):
    """`/platform/*` is refused on a clinic address, before any query runs."""
    response = await northgate.get("/v1/platform/billing/metrics")
    assert response.status_code == 401
    assert NORTHGATE != CONSOLE


async def test_the_metrics_accept_the_filters_the_console_offers(console):
    today = date.today()
    response = await console.get(
        "/v1/platform/billing/metrics",
        params={
            "start": (today - timedelta(days=30)).isoformat(),
            "end": today.isoformat(),
            "feature_key": "lab",
            "payment_status": "captured",
            "subscription_status": "active",
        },
    )
    assert response.status_code == 200, response.text
    assert [a["feature_key"] for a in response.json()["addons"]] == ["lab"]


async def test_a_malformed_filter_is_a_client_error_not_a_crash(console):
    response = await console.get(
        "/v1/platform/billing/metrics", params={"subscription_status": "banana"}
    )
    assert response.status_code == 422


async def test_a_bundled_module_counts_for_adoption_but_not_for_revenue():
    """Adoption and revenue answer different questions, and the console's two
    columns say so.

    The Clinic plan bundles the lab. A practice on it *has* the lab — so "how
    many practices have the lab" must count it — and pays nothing extra for it,
    so it must contribute nothing to MRR. Counting it in both would
    double-count the base plan that already includes it; counting it in neither
    would report a module in use by nobody.
    """
    tenant_id, clinic_plan = await _clinic("met-bundled", code="clinic")

    report = await _report(tenant_id=tenant_id)
    lab = [a for a in report.addons if a.feature_key == "lab"][0]

    assert lab.active_tenants == 1, "the practice has it"
    assert lab.monthly_revenue == Decimal("0.00"), "…and is not billed for it"
    assert report.mrr == Decimal(str(clinic_plan.amount)), "MRR is the plan, once"

    row = report.tenants[0]
    assert row.active_addons == 0, "a bundled module is not an add-on this clinic buys"
