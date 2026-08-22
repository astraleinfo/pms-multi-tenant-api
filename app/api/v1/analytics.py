"""§3.14 — the dashboard read model."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import TenantContext, require_permission
from app.core.permissions import Permission
from app.schemas.ops import AnalyticsOut
from app.services import analytics as analytics_service

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/dashboard", response_model=AnalyticsOut)
async def dashboard(
    days: int = Query(default=30, ge=1, le=365, description="Trailing window, ending today."),
    ctx: TenantContext = Depends(require_permission(Permission.ANALYTICS_READ)),
) -> AnalyticsOut:
    """Every figure the dashboard and the reports page show.

    Core metrics always; module panels only where the feature is active;
    clinical panels driven by `clinical_ext` alone. All of it is aggregated in
    PostgreSQL under this clinic's policy, so the client is sent figures rather
    than the rows behind them.
    """
    data = await analytics_service.dashboard(
        ctx.session,
        ctx.tenant_id,
        today=ctx.today,
        days=days,
        active_features=ctx.active_features,
        clinical_type=ctx.tenant.clinical_type,
    )
    return AnalyticsOut(**data)
