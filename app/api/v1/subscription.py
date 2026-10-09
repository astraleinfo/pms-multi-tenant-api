"""The platform's bill to a clinic — read by the clinic, settled by the console.

The clinic side is the practice admin's: what is owed, when, and the bill as a
PDF. It is one of the few routes still open while the clinic is on hold (see
`_open_while_on_hold` in `deps.py`), because it is how the hold ends.

The console side reads every clinic's bills one clinic at a time with that
clinic bound — the same discipline as the rest of the console — and records
payments. Settling and voiding move money, so they are a super admin's; the
support desk can read the bills to answer a clinic's question about one.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    TenantContext,
    client_ip,
    current_platform_user,
    current_super_admin,
    db_session,
    parse_uuid,
    require_permission,
    user_agent,
)
from app.core.config import settings
from app.core.errors import NotFound
from app.core.permissions import Permission
from app.db.session import session_factory, set_tenant
from app.models.identity import PlatformUser
from app.models.referrals import Referral
from app.models.tenancy import Tenant
from app.schemas.common import local_iso
from app.schemas.tenancy import (
    BillingRunOut,
    ConsoleReferralsOut,
    PlatformBillingOut,
    PlatformBillingRowOut,
    ReferralOut,
    ReferralProgramOut,
    ReferredByOut,
    SettleIn,
    SubscriptionInvoiceOut,
    SubscriptionOut,
    SubscriptionPlanOut,
    SubscriptionSummaryOut,
    TenantOut,
)
from app.services import audit
from app.services import platform as platform_service
from app.services import referrals as referral_service
from app.services import rent as rent_service
from app.services import tenants as tenant_service
from app.services.documents import build_subscription_invoice_pdf
from app.services.platform import Timings

clinic_router = APIRouter(prefix="/tenant/subscription", tags=["subscription"])
console_router = APIRouter(prefix="/platform", tags=["platform"])


async def _subscription(
    session: AsyncSession, tenant: Tenant, timings: Timings | None = None
) -> SubscriptionOut:
    bills = await rent_service.list_bills(session, tenant.id)
    today = rent_service.clinic_today(tenant)
    if timings is None:
        timings = await platform_service.get_timings(session)
    return SubscriptionOut(
        summary=SubscriptionSummaryOut(**rent_service.summary_for(tenant, bills, today, timings)),
        invoices=[SubscriptionInvoiceOut.of(b, rent_service.hold_date(b, timings)) for b in bills],
        plan=SubscriptionPlanOut(
            **await rent_service.plan_for(session, tenant, timings.subscription_fee_monthly)
        ),
    )


def _pdf(bill, tenant: Tenant, timings: Timings, logo: bytes | None) -> Response:
    content = build_subscription_invoice_pdf(
        bill, clinic=tenant.name, hold_on=rent_service.hold_date(bill, timings), clinic_logo=logo,
    )
    safe = "".join(ch for ch in bill.number if ch.isalnum() or ch in "-_") or "bill"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{safe}.pdf"',
            "Cache-Control": "private, no-store",
        },
    )


# --- the clinic's own bills ---------------------------------------------------------


@clinic_router.get("", response_model=SubscriptionOut)
async def my_subscription(
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_SUBSCRIPTION_MANAGE)),
) -> SubscriptionOut:
    """What this clinic owes the platform, and every bill it has been sent."""
    return await _subscription(ctx.session, ctx.tenant)


@clinic_router.get("/invoices/{bill_id}/pdf")
async def my_bill_pdf(
    bill_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_SUBSCRIPTION_MANAGE)),
) -> Response:
    bill = await rent_service.get_bill(ctx.session, ctx.tenant_id, parse_uuid(bill_id, what="bill"))
    timings = await platform_service.get_timings(ctx.session)
    return _pdf(bill, ctx.tenant, timings, await tenant_service.logo_for(ctx.session, ctx.tenant_id))


# --- the console ------------------------------------------------------------------------


async def _clinic(session: AsyncSession, tenant_id: str) -> Tenant:
    clinic = await session.get(Tenant, parse_uuid(tenant_id, what="clinic"))
    if clinic is None:
        raise NotFound("No such clinic.")
    await set_tenant(session, clinic.id)
    return clinic


@console_router.get("/billing", response_model=PlatformBillingOut)
async def platform_billing(
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> PlatformBillingOut:
    """Every clinic's bills, gathered one clinic at a time with it bound, and
    the flat monthly clinic fee they are raised at."""
    timings = await platform_service.get_timings(session)
    clinics = list((await session.execute(select(Tenant).order_by(Tenant.name))).scalars())
    rows = []
    for clinic in clinics:
        await set_tenant(session, clinic.id)
        sub = await _subscription(session, clinic, timings)
        rows.append(
            PlatformBillingRowOut(tenant=TenantOut.of(clinic), summary=sub.summary, invoices=sub.invoices)
        )
    await set_tenant(session, None)
    return PlatformBillingOut(
        enabled=settings.rent_billing_enabled,
        currency=settings.rent_currency,
        fee_monthly=float(timings.subscription_fee_monthly),
        due_days=timings.rent_due_days,
        grace_days=timings.rent_grace_days,
        reminder_days_before_due=timings.rent_reminder_days_before_due,
        clinics=rows,
    )


@console_router.post("/billing/run", response_model=BillingRunOut)
async def run_billing(
    request: Request,
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> BillingRunOut:
    """Run the cycle now: raise this month's bills, send what reminders are
    due, and hold or release clinics. The same run the beat makes, so it is
    safe to press twice — the second press finds nothing left to do.

    It works whether or not `RENT_BILLING_ENABLED` is on; that switch only
    decides whether the beat runs it by itself.
    """
    summary = await rent_service.run_cycle(session_factory)
    await audit.record(
        session, action="platform.billing_run", actor_kind="platform", actor_id=staff.id,
        actor_label=f"{staff.name} <{staff.email}>", entity="billing", ip=client_ip(request),
        user_agent=user_agent(request), meta=summary,
    )
    return BillingRunOut(**summary)


@console_router.get("/tenants/{tenant_id}/subscription", response_model=SubscriptionOut)
async def tenant_subscription(
    tenant_id: str,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> SubscriptionOut:
    clinic = await _clinic(session, tenant_id)
    return await _subscription(session, clinic)


@console_router.get("/tenants/{tenant_id}/subscription/invoices/{bill_id}/pdf")
async def tenant_bill_pdf(
    tenant_id: str,
    bill_id: str,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> Response:
    clinic = await _clinic(session, tenant_id)
    bill = await rent_service.get_bill(session, clinic.id, parse_uuid(bill_id, what="bill"))
    timings = await platform_service.get_timings(session)
    # `_clinic` has bound this session to the clinic, so its settings are visible
    return _pdf(bill, clinic, timings, await tenant_service.logo_for(session, clinic.id))


@console_router.get("/tenants/{tenant_id}/referrals", response_model=ConsoleReferralsOut)
async def tenant_referrals(
    tenant_id: str,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> ConsoleReferralsOut:
    clinic = await _clinic(session, tenant_id)

    referred_by = None
    incoming = (
        await session.execute(select(Referral).where(Referral.referred_tenant_id == clinic.id))
    ).scalar_one_or_none()
    if incoming is not None:
        referrer = await session.get(Tenant, incoming.referrer_tenant_id)
        referred_by = ReferredByOut(
            referrer_tenant_name=referrer.name if referrer else "—",
            code_used=incoming.code_used, status=incoming.status.value,
            created_at=local_iso(incoming.created_at) or "",
        )

    settings_row = await platform_service.get_platform_settings(session)
    balance = await referral_service.balance_for(session, clinic.id)
    rows = await referral_service.list_for(session, clinic.id)
    referred = []
    for row in rows:
        target = await session.get(Tenant, row.referred_tenant_id)
        referred.append(ReferralOut.of(row, target.name if target else "—"))

    return ConsoleReferralsOut(
        referred_by=referred_by,
        program=ReferralProgramOut(
            code=clinic.referral_code,
            formatted_code=referral_service.format_code(clinic.referral_code)
            if clinic.referral_code else None,
            credit_amount=float(settings_row.referral_credit_amount),
            currency=settings.rent_currency,
            balance=float(balance),
            referrals=referred,
        ),
    )


@console_router.post(
    "/tenants/{tenant_id}/subscription/invoices/{bill_id}/settle", response_model=SubscriptionOut
)
async def settle_bill(
    tenant_id: str,
    bill_id: str,
    payload: SettleIn,
    request: Request,
    background: BackgroundTasks,
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> SubscriptionOut:
    """Record a bill as paid. If it was what held the clinic, the hold lifts now."""
    clinic = await _clinic(session, tenant_id)
    bill, change, receipts = await rent_service.settle(
        session, clinic, parse_uuid(bill_id, what="bill"),
        reference=payload.reference, by=f"{staff.name} <{staff.email}>",
    )
    await audit.record_for_tenant(
        session, clinic.id, action="platform.bill_settled", actor_kind="platform",
        actor_id=staff.id, actor_label=f"{staff.name} <{staff.email}>",
        entity="subscription_invoice", entity_id=str(bill.id), ip=client_ip(request),
        user_agent=user_agent(request),
        meta={"number": bill.number, "reference": payload.reference, "hold": change},
    )
    # After the response, and so after the commit: a receipt never goes out
    # for a payment that was then rolled back, and the console is not kept
    # waiting on the mail server to record one.
    background.add_task(rent_service.deliver, receipts, what=f"receipt for {bill.number}")
    await set_tenant(session, clinic.id)
    return await _subscription(session, clinic)


@console_router.post(
    "/tenants/{tenant_id}/subscription/invoices/{bill_id}/void", response_model=SubscriptionOut
)
async def void_bill(
    tenant_id: str,
    bill_id: str,
    request: Request,
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> SubscriptionOut:
    """Withdraw a bill raised in error. It stops counting towards the hold."""
    clinic = await _clinic(session, tenant_id)
    bill, change = await rent_service.void(
        session, clinic, parse_uuid(bill_id, what="bill"), by=f"{staff.name} <{staff.email}>"
    )
    await audit.record_for_tenant(
        session, clinic.id, action="platform.bill_voided", actor_kind="platform",
        actor_id=staff.id, actor_label=f"{staff.name} <{staff.email}>",
        entity="subscription_invoice", entity_id=str(bill.id), ip=client_ip(request),
        user_agent=user_agent(request), meta={"number": bill.number, "hold": change},
    )
    await set_tenant(session, clinic.id)
    return await _subscription(session, clinic)
