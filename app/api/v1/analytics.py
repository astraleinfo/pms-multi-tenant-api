"""§3.14 — the dashboard read model."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.api.deps import TenantContext, require_permission
from app.core.permissions import Permission, role_has
from app.schemas.ops import AnalyticsOut
from app.services import analytics as analytics_service
from app.services import tenants as tenant_service
from app.services.documents import build_report_pdf

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/dashboard", response_model=AnalyticsOut)
async def dashboard(
    days: int = Query(default=30, ge=1, le=365, description="Trailing window, ending today."),
    ctx: TenantContext = Depends(require_permission(Permission.ANALYTICS_READ)),
) -> AnalyticsOut:
    """Every figure the dashboard and the reports page show.

    Core metrics and every module panel, always; clinical panels driven by
    `clinical_ext` alone. All of it is aggregated in
    PostgreSQL under this clinic's policy, so the client is sent figures rather
    than the rows behind them.
    """
    data = await analytics_service.dashboard(
        ctx.session,
        ctx.tenant_id,
        today=ctx.today,
        days=days,
        clinical_type=ctx.tenant.clinical_type,
    )
    # The takings are the practice admin's. Taken out here, on the server, so
    # a role without them is never sent the figures to hide.
    if not role_has(ctx.user.role, Permission.REVENUE_READ):
        data = analytics_service.without_revenue(data)
    return AnalyticsOut(**data)


@router.get("/report.pdf")
async def report_pdf(
    days: int = Query(default=30, ge=1, le=365, description="Trailing window, ending today."),
    ctx: TenantContext = Depends(require_permission(Permission.ANALYTICS_READ)),
) -> Response:
    """The reports page as a PDF — the same read model, so the printed figures
    are the figures on screen, every module section included, exactly as on
    the page."""
    data = await analytics_service.dashboard(
        ctx.session,
        ctx.tenant_id,
        today=ctx.today,
        days=days,
        clinical_type=ctx.tenant.clinical_type,
    )
    if not role_has(ctx.user.role, Permission.REVENUE_READ):
        data = analytics_service.without_revenue(data)
    content = build_report_pdf(
        data,
        clinic=ctx.tenant.name,
        config=ctx.config,
        currency=ctx.config.currency.value if ctx.config.currency else "",
        generated_by=ctx.principal.label,
        tz=ctx.tz,
        logo=await tenant_service.logo_for(ctx.session, ctx.tenant_id),
    )
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="practice-report-{ctx.today.isoformat()}-{days}d.pdf"',
            "Cache-Control": "private, no-store",
        },
    )
