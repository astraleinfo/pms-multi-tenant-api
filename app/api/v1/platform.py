"""The platform console (§3.1).

Served on the apex host only, to platform staff who have no tenant of their own.
Creating a clinic is done here — never by a self-service form inside a tenant.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, current_platform_user, db_session, parse_uuid, user_agent
from app.models.identity import PlatformUser
from app.models.tenancy import ClinicalType, Feature
from app.schemas.identity import PlatformUserOut
from app.schemas.tenancy import (
    ClinicalTypeOut,
    FeatureOut,
    PlatformSnapshotOut,
    PlatformTenantSummary,
    TenantCreatedOut,
    TenantCreateIn,
    TenantOut,
    TenantStatusIn,
)
from app.services import audit
from app.services import platform as platform_service
from app.services.tenants import get_config

router = APIRouter(prefix="/platform", tags=["platform"])


@router.get("/me", response_model=PlatformUserOut)
async def me(staff: PlatformUser = Depends(current_platform_user)) -> PlatformUserOut:
    return PlatformUserOut.of(staff)


@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants(
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session),
) -> list[TenantOut]:
    rows = await platform_service.list_tenants(session)
    return [TenantOut.of(t) for t in rows]


@router.post("/tenants", response_model=TenantCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    payload: TenantCreateIn,
    request: Request,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session),
) -> TenantCreatedOut:
    """Claims the subdomain, seeds core entitlements, clones the clinical-type
    field set, writes a configuration, and mints the first admin credential."""
    tenant, admin = await platform_service.create_tenant(session, payload, today=date.today())
    await get_config(session, tenant.id)
    await audit.record_for_tenant(
        session, tenant.id, action="platform.tenant_create", actor_kind="platform",
        actor_id=staff.id, actor_label=f"{staff.name} <{staff.email}>", entity="tenant",
        entity_id=str(tenant.id), ip=client_ip(request), user_agent=user_agent(request),
        meta={"slug": tenant.slug},
    )
    return TenantCreatedOut(tenant=TenantOut.of(tenant), admin_user_id=str(admin.id))


@router.patch("/tenants/{tenant_id}/status", response_model=TenantOut)
async def set_tenant_status(
    tenant_id: str,
    payload: TenantStatusIn,
    request: Request,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session),
) -> TenantOut:
    """Suspending a clinic takes its subdomain offline: sign-in is refused and
    every existing session stops resolving."""
    tenant = await platform_service.set_tenant_status(
        session, parse_uuid(tenant_id, what="clinic"), payload.status
    )
    await audit.record_for_tenant(
        session, tenant.id, action=f"platform.tenant_{payload.status.value}",
        actor_kind="platform", actor_id=staff.id, actor_label=f"{staff.name} <{staff.email}>",
        entity="tenant", entity_id=str(tenant.id), ip=client_ip(request), user_agent=user_agent(request),
    )
    return TenantOut.of(tenant)


@router.get("/snapshot", response_model=PlatformSnapshotOut)
async def snapshot(
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session),
) -> PlatformSnapshotOut:
    """Counts per clinic, gathered one clinic at a time with that clinic bound
    to the session — the console never runs an unscoped query either."""
    data = await platform_service.snapshot(session)
    return PlatformSnapshotOut(
        tenants=[
            PlatformTenantSummary(
                tenant=TenantOut.of(row["tenant"]),
                active_features=row["active_features"],
                users=row["users"],
                patients=row["patients"],
                appointments=row["appointments"],
                encounters=row["encounters"],
                invoices=row["invoices"],
                revenue=row["revenue"],
                last_activity=row["last_activity"],
            )
            for row in data["rows"]
        ],
        totals=data["totals"],
    )


@router.get("/catalog/clinical-types", response_model=list[ClinicalTypeOut])
async def clinical_types(
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session),
) -> list[ClinicalTypeOut]:
    rows = (await session.execute(select(ClinicalType).order_by(ClinicalType.label))).scalars()
    return [ClinicalTypeOut.of(c) for c in rows]


@router.get("/catalog/features", response_model=list[FeatureOut])
async def features(
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session),
) -> list[FeatureOut]:
    rows = (await session.execute(select(Feature).order_by(Feature.name))).scalars()
    return [FeatureOut.of(f) for f in rows]
