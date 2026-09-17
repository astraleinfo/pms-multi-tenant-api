"""Settings → Billing, the console's billing metrics, and the webhook endpoint.

Three routers with three different front doors, and the difference is the whole
authorisation story:

*   `/v1/settings/billing/*` — a clinic, on its own subdomain, behind
    `require_permission(SUBSCRIPTION_*)`. Those two permissions belong to the
    tenant administrator alone: every other role can take a *patient's* money,
    and none of them can change what the practice itself pays for.
*   `/v1/platform/billing/*` — platform staff, on the admin host only, behind
    `current_platform_user`. Metrics, refunds and the webhook log.
*   `/v1/webhooks/{gateway}` — unauthenticated, because a gateway cannot sign
    in. The signature *is* the authentication, it is checked against the raw
    body, and a delivery that fails it is stored, audited and never processed.

Two rules this file keeps that are easy to lose:

*   **no amount is ever read from a request body.** Every figure comes from the
    plan catalogue, the entitlements and the cycle calendar. The payment
    schemas have no amount field at all, which is why.
*   **no credential is ever returned** beyond the gateway's public key id, in
    `CheckoutOut`, which is the value a checkout widget is supposed to have.
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    TenantContext,
    client_ip,
    current_platform_user,
    db_session,
    parse_uuid,
    require_permission,
    user_agent,
)
from app.core.errors import NotFound
from app.core.permissions import Permission
from app.db.session import set_tenant
from app.models.billing import (
    SubscriptionInvoice,
    SubscriptionPayment,
    SubscriptionRefund,
    WebhookEvent,
)
from app.models.enums import (
    SubscriptionInvoiceStatus,
    SubscriptionPaymentStatus,
    SubscriptionStatus,
)
from app.models.identity import PlatformUser
from app.models.tenancy import Tenant
from app.schemas.billing import (
    AddonActionOut,
    AddonOut,
    BillingAccountOut,
    BillingAccountPatch,
    BillingMetricsOut,
    BillingSummaryOut,
    CancelIn,
    CheckoutOut,
    InvoiceOut,
    InvoicePageOut,
    PaymentOut,
    PaymentVerifyIn,
    PlanChangeIn,
    PlanOut,
    RefundIn,
    RefundOut,
    SubscribeIn,
    SubscriptionOut,
    WebhookAckOut,
    WebhookEventOut,
)
from app.schemas.common import iso_date, money
from app.services import audit, paging
from app.services.payment import billing_service, payment_service, subscription_service
from app.services.payment.addon import billing_service as addon_service
from app.services.payment.invoice import generation_service
from app.services.payment.metrics import billing_metrics_service as metrics_service
from app.services.payment.webhook import handler as webhook_handler

log = logging.getLogger("pms.billing")

router = APIRouter(prefix="/settings/billing", tags=["billing-subscription"])
platform_router = APIRouter(prefix="/platform/billing", tags=["platform-billing"])
webhooks_router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _read(ctx: TenantContext = Depends(require_permission(Permission.SUBSCRIPTION_READ))):
    return ctx


def _manage(ctx: TenantContext = Depends(require_permission(Permission.SUBSCRIPTION_MANAGE))):
    return ctx


def _invoice_out(invoice: SubscriptionInvoice, tz: str | None = None) -> InvoiceOut:
    return InvoiceOut.of(
        invoice,
        paid=billing_service.paid_amount(invoice),
        balance=billing_service.balance_of(invoice),
        tz=tz,
    )


async def _summary_out(ctx: TenantContext) -> BillingSummaryOut:
    summary = await billing_service.summary(ctx.session, ctx.tenant_id, today=ctx.today)
    subscription = None
    if summary.subscription is not None and summary.plan is not None:
        subscription = SubscriptionOut.of(summary.subscription, summary.plan, summary.cycle)
    return BillingSummaryOut(
        subscription=subscription,
        account=BillingAccountOut.of(summary.account),
        addons=[AddonOut.of(a) for a in summary.addons],
        recurring_subtotal=money(summary.recurring_subtotal),
        next_billing_on=iso_date(summary.next_billing_on),
        outstanding_total=money(summary.outstanding_total),
        outstanding_count=summary.outstanding_count,
        currency=str(summary.plan.currency) if summary.plan else str(summary.account.currency),
    )


# --- Settings → Billing ------------------------------------------------------


@router.get("", response_model=BillingSummaryOut)
async def billing_summary(ctx: TenantContext = Depends(_read)) -> BillingSummaryOut:
    """The whole billing screen: plan, add-ons, prices, next billing date,
    scheduled cancellations and what is outstanding.

    Read-only. Opening this page does not open a billing cycle — the renewal
    job does that, on a schedule, where a failure is retried rather than shown
    to whoever happened to load a tab.
    """
    return await _summary_out(ctx)


@router.get("/plans", response_model=list[PlanOut])
async def available_plans(ctx: TenantContext = Depends(_read)) -> list[PlanOut]:
    return [PlanOut.of(p) for p in await billing_service.list_plans(ctx.session)]


@router.post("/subscribe", response_model=BillingSummaryOut, status_code=status.HTTP_201_CREATED)
async def subscribe(
    payload: SubscribeIn, request: Request, ctx: TenantContext = Depends(_manage)
) -> BillingSummaryOut:
    """Take the mandatory base plan (requirement 1).

    The plan is named by code; its price is read from the catalogue. There is
    no amount in the request body and there will not be one.
    """
    plan = await billing_service.plan_by_code(ctx.session, payload.plan_code)
    result = await subscription_service.subscribe(
        ctx.session, ctx.tenant_id, plan=plan, today=ctx.today
    )
    await audit.record(
        ctx.session, action="billing.subscribe", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="subscription", entity_id=str(result.subscription.id),
        ip=client_ip(request), user_agent=user_agent(request),
        meta={"plan": plan.code, "invoice": result.invoice.number if result.invoice else None},
    )
    return await _summary_out(ctx)


@router.post("/plan", response_model=BillingSummaryOut)
async def change_plan(
    payload: PlanChangeIn, request: Request, ctx: TenantContext = Depends(_manage)
) -> BillingSummaryOut:
    """Move to a different base plan.

    The cycle in flight ends today and the unused part of it becomes a credit
    on the new cycle's invoice — the one place in this product where money
    already taken comes back, and it comes back as a credit rather than a
    refund.
    """
    subscription = await billing_service.require_subscription(
        ctx.session, ctx.tenant_id, for_update=True
    )
    plan = await billing_service.plan_by_code(ctx.session, payload.plan_code)
    result = await subscription_service.change_plan(
        ctx.session, subscription, plan=plan, today=ctx.today
    )
    await audit.record(
        ctx.session, action="billing.plan_change", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="subscription", entity_id=str(subscription.id),
        ip=client_ip(request), user_agent=user_agent(request),
        meta={"plan": plan.code, "invoice": result.invoice.number if result.invoice else None},
    )
    return await _summary_out(ctx)


@router.post("/pause", response_model=BillingSummaryOut)
async def pause(request: Request, ctx: TenantContext = Depends(_manage)) -> BillingSummaryOut:
    subscription = await billing_service.require_subscription(
        ctx.session, ctx.tenant_id, for_update=True
    )
    await subscription_service.pause(ctx.session, subscription)
    await audit.record(
        ctx.session, action="billing.pause", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="subscription",
        entity_id=str(subscription.id), ip=client_ip(request), user_agent=user_agent(request),
    )
    return await _summary_out(ctx)


@router.post("/resume", response_model=BillingSummaryOut)
async def resume(request: Request, ctx: TenantContext = Depends(_manage)) -> BillingSummaryOut:
    subscription = await billing_service.require_subscription(
        ctx.session, ctx.tenant_id, for_update=True
    )
    await subscription_service.resume(ctx.session, subscription, today=ctx.today)
    await audit.record(
        ctx.session, action="billing.resume", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="subscription",
        entity_id=str(subscription.id), ip=client_ip(request), user_agent=user_agent(request),
    )
    return await _summary_out(ctx)


@router.post("/cancel", response_model=BillingSummaryOut)
async def cancel(
    payload: CancelIn, request: Request, ctx: TenantContext = Depends(_manage)
) -> BillingSummaryOut:
    """Cancel at the end of the period already paid for.

    The clinic keeps the product until then. There is no field on `CancelIn` to
    make it sooner, because ending a paid period early is a platform decision
    and never a self-service one.
    """
    subscription = await billing_service.require_subscription(
        ctx.session, ctx.tenant_id, for_update=True
    )
    await subscription_service.cancel(ctx.session, subscription, today=ctx.today)
    await audit.record(
        ctx.session, action="billing.cancel", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="subscription",
        entity_id=str(subscription.id), ip=client_ip(request), user_agent=user_agent(request),
        meta={"reason": payload.reason} if payload.reason else None,
    )
    return await _summary_out(ctx)


@router.post("/resume-cancellation", response_model=BillingSummaryOut)
async def resume_cancellation(
    request: Request, ctx: TenantContext = Depends(_manage)
) -> BillingSummaryOut:
    """Change your mind before the cancellation takes effect. Nothing is charged."""
    subscription = await billing_service.require_subscription(
        ctx.session, ctx.tenant_id, for_update=True
    )
    await subscription_service.resume_cancellation(ctx.session, subscription)
    await audit.record(
        ctx.session, action="billing.cancel_reverted", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="subscription", entity_id=str(subscription.id),
        ip=client_ip(request), user_agent=user_agent(request),
    )
    return await _summary_out(ctx)


@router.get("/account", response_model=BillingAccountOut)
async def billing_account(ctx: TenantContext = Depends(_read)) -> BillingAccountOut:
    return BillingAccountOut.of(await billing_service.get_account(ctx.session, ctx.tenant_id))


@router.patch("/account", response_model=BillingAccountOut)
async def update_billing_account(
    payload: BillingAccountPatch, ctx: TenantContext = Depends(_manage)
) -> BillingAccountOut:
    """Who to bill, where, and under which tax identity.

    `place_of_supply` and `country` feed the tax lookup, so changing them
    changes what future invoices are taxed at — never what past ones say.
    """
    account = await billing_service.get_account(ctx.session, ctx.tenant_id)
    for key, value in payload.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(account, key, value.strip().upper() if key == "country" else value)
    await ctx.session.flush()
    return BillingAccountOut.of(account)


# --- add-ons -----------------------------------------------------------------


@router.post("/addons/{feature_key}", response_model=AddonActionOut)
async def activate_addon(
    feature_key: str, request: Request, ctx: TenantContext = Depends(_manage)
) -> AddonActionOut:
    """Switch a module on.

    On the first day of the cycle this is the full monthly charge; mid-cycle it
    is prorated over the days the cycle actually has and invoiced immediately.
    Switching one back on before a scheduled cancellation takes effect charges
    nothing at all — the response says which of those happened.
    """
    result = await addon_service.activate(
        ctx.session, ctx.tenant_id, feature_key, today=ctx.today
    )
    await audit.record(
        ctx.session, action="billing.addon_activate", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="tenant_feature", entity_id=feature_key,
        ip=client_ip(request), user_agent=user_agent(request),
        meta={
            "charged": result.charged,
            "reinstated": result.reinstated,
            "amount": float(result.proration.amount) if result.proration else 0.0,
            "invoice": result.invoice.number if result.invoice else None,
        },
    )
    return _addon_action(feature_key, result, ctx.tz)


@router.delete("/addons/{feature_key}", response_model=AddonActionOut)
async def cancel_addon(
    feature_key: str, request: Request, ctx: TenantContext = Depends(_manage)
) -> AddonActionOut:
    """Stop a module at the end of the cycle.

    It keeps working until then, nothing is refunded, and the next invoice does
    not mention it.
    """
    result = await addon_service.cancel(ctx.session, ctx.tenant_id, feature_key, today=ctx.today)
    await audit.record(
        ctx.session, action="billing.addon_cancel", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="tenant_feature", entity_id=feature_key,
        ip=client_ip(request), user_agent=user_agent(request),
        meta={"cancels_on": iso_date(result.entitlement.cancel_at)},
    )
    return _addon_action(feature_key, result, ctx.tz)


def _addon_action(feature_key: str, result, tz: str | None) -> AddonActionOut:
    from app.models.enums import TenantFeatureStatus

    return AddonActionOut(
        feature_key=feature_key,
        active=result.entitlement.status == TenantFeatureStatus.ACTIVE,
        charged=result.charged,
        reinstated=result.reinstated,
        invoice=_invoice_out(result.invoice, tz) if result.invoice else None,
        cancels_on=iso_date(result.entitlement.cancel_at),
        prorated_days=result.proration.charged_days if result.proration else None,
        cycle_days=result.proration.cycle_days if result.proration else None,
        amount=money(result.proration.amount) if result.proration else None,
    )


# --- invoices and payment ----------------------------------------------------


@router.get("/invoices", response_model=InvoicePageOut)
async def list_invoices(
    status_filter: SubscriptionInvoiceStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=0, ge=0, le=200, description="0 asks for the whole collection."),
    offset: int = Query(default=0, ge=0),
    ctx: TenantContext = Depends(_read),
) -> InvoicePageOut:
    """Billing history, newest first."""
    stmt = select(SubscriptionInvoice).where(SubscriptionInvoice.tenant_id == ctx.tenant_id)
    if status_filter is not None:
        stmt = stmt.where(SubscriptionInvoice.status == status_filter)
    stmt = stmt.order_by(SubscriptionInvoice.issued_on.desc(), SubscriptionInvoice.number.desc())
    rows, total = await paging.page(ctx.session, stmt, limit=limit, offset=offset)
    return InvoicePageOut(
        items=[_invoice_out(i, ctx.tz) for i in rows], total=total, limit=limit, offset=offset
    )


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(invoice_id: str, ctx: TenantContext = Depends(_read)) -> InvoiceOut:
    invoice = await payment_service.get_invoice(
        ctx.session, ctx.tenant_id, parse_uuid(invoice_id, what="invoice")
    )
    return _invoice_out(invoice, ctx.tz)


@router.get("/invoices/{invoice_id}/download")
async def download_invoice(invoice_id: str, ctx: TenantContext = Depends(_read)) -> Response:
    """The invoice as a PDF, built from the stored line items.

    Rendered from what was written at issue time, never recomputed — a price
    change next month must not be able to alter a document somebody has already
    filed.
    """
    from app.services.pdf import build_subscription_invoice_pdf

    invoice = await payment_service.get_invoice(
        ctx.session, ctx.tenant_id, parse_uuid(invoice_id, what="invoice")
    )
    account = await billing_service.get_account(ctx.session, ctx.tenant_id)
    pdf = build_subscription_invoice_pdf(
        invoice,
        tenant=ctx.tenant,
        account=account,
        paid=billing_service.paid_amount(invoice),
        balance=billing_service.balance_of(invoice),
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{invoice.number}.pdf"'},
    )


@router.post("/invoices/{invoice_id}/checkout", response_model=CheckoutOut)
async def checkout(invoice_id: str, ctx: TenantContext = Depends(_manage)) -> CheckoutOut:
    """Open a payment for an invoice — the "Pay Now" button.

    The amount is the outstanding balance as this server computes it, and the
    response carries the gateway's *public* key id and an order id. Nothing
    secret crosses this boundary.
    """
    context = await payment_service.create_order(
        ctx.session, ctx.tenant_id, parse_uuid(invoice_id, what="invoice")
    )
    return CheckoutOut.of(context)


@router.post("/invoices/{invoice_id}/verify", response_model=InvoiceOut)
async def verify_payment(
    invoice_id: str,
    payload: PaymentVerifyIn,
    request: Request,
    ctx: TenantContext = Depends(_manage),
) -> InvoiceOut:
    """What Checkout hands back, checked before it counts.

    Signature, then a server-side fetch of the payment, then the amount against
    the invoice. The browser's claim of success is not one of the three checks
    — it is only what prompts them.

    A webhook will say the same thing, and both paths end at the same
    idempotent write, so whichever arrives first settles the invoice and the
    second changes nothing.
    """
    invoice = await payment_service.verify_and_capture(
        ctx.session,
        ctx.tenant_id,
        order_id=payload.order_id,
        payment_id=payload.payment_id,
        signature=payload.signature,
    )
    await audit.record(
        ctx.session, action="billing.payment_verified", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="subscription_invoice", entity_id=str(invoice.id),
        ip=client_ip(request), user_agent=user_agent(request),
        meta={"number": invoice.number, "payment_id": payload.payment_id},
    )
    return _invoice_out(invoice, ctx.tz)


@router.get("/payments", response_model=list[PaymentOut])
async def list_payments(ctx: TenantContext = Depends(_read)) -> list[PaymentOut]:
    rows = (
        await ctx.session.execute(
            select(SubscriptionPayment)
            .where(SubscriptionPayment.tenant_id == ctx.tenant_id)
            .order_by(SubscriptionPayment.created_at.desc())
        )
    ).scalars()
    return [PaymentOut.of(p, ctx.tz) for p in rows]


# --- the console -------------------------------------------------------------


@platform_router.get("/metrics", response_model=BillingMetricsOut)
async def billing_metrics(
    start: date | None = Query(default=None, description="Inclusive. Bounds the movement figures."),
    end: date | None = Query(default=None, description="Inclusive."),
    tenant_id: str | None = Query(default=None),
    plan_id: str | None = Query(default=None),
    feature_key: str | None = Query(default=None, max_length=40),
    payment_status: SubscriptionPaymentStatus | None = Query(default=None),
    subscription_status: SubscriptionStatus | None = Query(default=None),
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session),
) -> BillingMetricsOut:
    """MRR, revenue, subscriptions, payments, outstanding invoices, refunds and
    add-on adoption — filtered.

    Gathered one clinic at a time with that clinic bound, because there is no
    row-level-security bypass in this application and the console does not get
    one either.
    """
    filters = metrics_service.MetricsFilter(
        start=start,
        end=end,
        tenant_id=parse_uuid(tenant_id, what="clinic") if tenant_id else None,
        plan_id=parse_uuid(plan_id, what="plan") if plan_id else None,
        feature_key=feature_key,
        payment_status=payment_status,
        subscription_status=subscription_status,
    )
    return BillingMetricsOut.of(await metrics_service.report(session, filters))


@platform_router.get("/plans", response_model=list[PlanOut])
async def console_plans(
    include_inactive: bool = Query(default=True),
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session),
) -> list[PlanOut]:
    rows = await billing_service.list_plans(session, include_inactive=include_inactive)
    return [PlanOut.of(p) for p in rows]


@platform_router.get("/tenants/{tenant_id}/invoices", response_model=list[InvoiceOut])
async def console_tenant_invoices(
    tenant_id: str,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session),
) -> list[InvoiceOut]:
    """One clinic's invoices, read with that clinic bound."""
    clinic_id = parse_uuid(tenant_id, what="clinic")
    if await session.get(Tenant, clinic_id) is None:
        raise NotFound("No such clinic.")
    await set_tenant(session, clinic_id)
    rows = (
        await session.execute(
            select(SubscriptionInvoice)
            .where(SubscriptionInvoice.tenant_id == clinic_id)
            .order_by(SubscriptionInvoice.issued_on.desc())
        )
    ).scalars()
    out = [_invoice_out(i) for i in rows]
    await set_tenant(session, None)
    return out


@platform_router.post("/tenants/{tenant_id}/refunds", response_model=RefundOut)
async def console_refund(
    tenant_id: str,
    payload: RefundIn,
    request: Request,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session),
) -> RefundOut:
    """Refund a captured payment. A deliberate act, and the only way money goes
    back — nothing in this product refunds automatically."""
    clinic_id = parse_uuid(tenant_id, what="clinic")
    tenant = await session.get(Tenant, clinic_id)
    if tenant is None:
        raise NotFound("No such clinic.")
    await set_tenant(session, clinic_id)
    refund = await payment_service.refund(
        session,
        clinic_id,
        parse_uuid(payload.payment_id, what="payment"),
        amount=None if payload.amount is None else round(payload.amount, 2),
        reason=payload.reason,
    )
    out = RefundOut.of(refund, tenant.timezone)
    await audit.record(
        session, action="billing.refund", tenant_id=clinic_id, actor_kind="platform",
        actor_id=staff.id, actor_label=f"{staff.name} <{staff.email}>", entity="subscription_refund",
        entity_id=str(refund.id), ip=client_ip(request), user_agent=user_agent(request),
        meta={"amount": out.amount, "reason": payload.reason},
    )
    await session.flush()
    await set_tenant(session, None)
    return out


@platform_router.post("/tenants/{tenant_id}/invoices/{invoice_id}/void", response_model=InvoiceOut)
async def console_void_invoice(
    tenant_id: str,
    invoice_id: str,
    request: Request,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session),
) -> InvoiceOut:
    """Unmake an invoice that should not have been raised.

    The only way to change one: the figures are frozen by a database trigger,
    so a mistake is corrected by voiding and re-raising rather than by editing.
    Voiding releases the one-invoice-per-cycle index, which is what lets the
    cycle be invoiced again.
    """
    clinic_id = parse_uuid(tenant_id, what="clinic")
    if await session.get(Tenant, clinic_id) is None:
        raise NotFound("No such clinic.")
    await set_tenant(session, clinic_id)
    invoice = await generation_service.void_invoice(
        session, clinic_id, parse_uuid(invoice_id, what="invoice")
    )
    out = _invoice_out(invoice)
    await audit.record(
        session, action="billing.invoice_void", tenant_id=clinic_id, actor_kind="platform",
        actor_id=staff.id, actor_label=f"{staff.name} <{staff.email}>",
        entity="subscription_invoice", entity_id=str(invoice.id),
        ip=client_ip(request), user_agent=user_agent(request), meta={"number": invoice.number},
    )
    await session.flush()
    await set_tenant(session, None)
    return out


@platform_router.get("/refunds", response_model=list[RefundOut])
async def console_refunds(
    tenant_id: str = Query(description="Refunds are read one clinic at a time."),
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session),
) -> list[RefundOut]:
    clinic_id = parse_uuid(tenant_id, what="clinic")
    tenant = await session.get(Tenant, clinic_id)
    if tenant is None:
        raise NotFound("No such clinic.")
    await set_tenant(session, clinic_id)
    rows = (
        await session.execute(
            select(SubscriptionRefund)
            .where(SubscriptionRefund.tenant_id == clinic_id)
            .order_by(SubscriptionRefund.created_at.desc())
        )
    ).scalars()
    out = [RefundOut.of(r, tenant.timezone) for r in rows]
    await set_tenant(session, None)
    return out


@platform_router.get("/webhooks", response_model=list[WebhookEventOut])
async def console_webhooks(
    status_filter: str | None = Query(default=None, alias="status", max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session),
) -> list[WebhookEventOut]:
    """The delivery log — including the ones that were refused.

    A rejected signature is the entry most worth having, so it is here rather
    than only in the application log.
    """
    stmt = select(WebhookEvent).order_by(WebhookEvent.received_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(WebhookEvent.status == status_filter)
    rows = (await session.execute(stmt)).scalars()
    return [WebhookEventOut.of(e) for e in rows]


# --- the gateway's front door ------------------------------------------------


@webhooks_router.post("/{gateway_name}", response_model=WebhookAckOut)
async def receive_webhook(
    gateway_name: str,
    request: Request,
    session: AsyncSession = Depends(db_session),
) -> WebhookAckOut:
    """Receive one gateway delivery. Unauthenticated by necessity, verified by
    signature.

    Deliberately does almost nothing: verify, store, answer. Razorpay retries
    for twenty-four hours on anything that is not a 2xx, so real work inside
    this request would turn one slow query into a retry storm. The billing
    worker processes the stored row.

    The answer is the same — `200 received` — whether the delivery was new, a
    redelivery, or refused for a bad signature. Saying which would let an
    unauthenticated caller probe for valid event ids and learn whether a forged
    signature was getting closer.
    """
    # A failure to *store* is deliberately allowed to surface as a 500: that is
    # how Razorpay is told to send it again, and losing a delivery quietly
    # would be worse than a noisy log line.
    await webhook_handler.receive(
        session,
        body=await request.body(),
        headers=dict(request.headers),
        gateway_name=gateway_name.strip().lower(),
    )
    return WebhookAckOut()


__all__ = ["platform_router", "router", "webhooks_router"]
