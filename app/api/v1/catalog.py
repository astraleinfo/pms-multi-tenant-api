"""Reference data and the one thing an unauthenticated visitor may ask:
*is there a clinic at this address?*
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import TenantContext, current_tenant, db_session, host_tenant
from app.models.enums import TenantStatus
from app.models.tenancy import ClinicalType, LabTest, MedicineSuggestion
from app.schemas.common import ApiModel
from app.schemas.tenancy import ClinicalTypeOut

router = APIRouter(tags=["catalog"])


class PublicTenantOut(ApiModel):
    """Just enough for a sign-in page to name the clinic. No staff, no
    counts, nothing about the people inside."""

    name: str
    slug: str
    clinical_type: str
    status: TenantStatus


class LabTestOut(ApiModel):
    code: str
    name: str
    panel: str


class MedicineOut(ApiModel):
    medicine: str
    strength: str
    dosage: str
    frequency: str
    duration: str


@router.get("/public/tenant", response_model=PublicTenantOut)
async def public_tenant(tenant=Depends(host_tenant)) -> PublicTenantOut:
    return PublicTenantOut(
        name=tenant.name, slug=tenant.slug, clinical_type=tenant.clinical_type, status=tenant.status
    )


@router.get("/catalog/clinical-types", response_model=list[ClinicalTypeOut])
async def clinical_types(
    session: AsyncSession = Depends(db_session, scope="function"),
) -> list[ClinicalTypeOut]:
    rows = (await session.execute(select(ClinicalType).order_by(ClinicalType.label))).scalars()
    return [ClinicalTypeOut.of(c) for c in rows]


@router.get("/catalog/lab-tests", response_model=list[LabTestOut])
async def lab_tests(ctx: TenantContext = Depends(current_tenant)) -> list[LabTestOut]:
    """Filtered to this clinic's discipline — a dental practice is not offered
    a visual field test."""
    rows = (await ctx.session.execute(select(LabTest).order_by(LabTest.panel, LabTest.name))).scalars()
    return [
        LabTestOut(code=t.code, name=t.name, panel=t.panel)
        for t in rows
        if not t.clinical_types or ctx.tenant.clinical_type in t.clinical_types
    ]


@router.get("/catalog/medicines", response_model=list[MedicineOut])
async def medicines(ctx: TenantContext = Depends(current_tenant)) -> list[MedicineOut]:
    rows = (
        await ctx.session.execute(select(MedicineSuggestion).order_by(MedicineSuggestion.medicine))
    ).scalars()
    return [
        MedicineOut(
            medicine=m.medicine, strength=m.strength, dosage=m.dosage,
            frequency=m.frequency, duration=m.duration,
        )
        for m in rows
        if not m.clinical_types or ctx.tenant.clinical_type in m.clinical_types
    ]
