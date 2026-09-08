"""The tariff — what this clinic charges (§3.12).

Reading a price is something most of the building does: the composer quotes
them, the lab quotes them, reception reads them off an invoice. Setting one is
the practice admin's alone, which is why the write routes ask for
`tenant:config:write` — the same permission that governs every other decision
about how this clinic runs.

Nothing here is behind a feature guard. A tariff is configuration, not a
module: a clinic that has not bought the lab can still be told what it would
charge for a scan.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import TenantContext, parse_uuid, require_permission
from app.core.permissions import Permission
from app.models.enums import ServiceKind
from app.schemas.identity import UserOut
from app.schemas.ops import (
    DoctorFeeIn,
    LabCatalogueImportIn,
    ServicePriceIn,
    ServicePriceOut,
    ServicePricePatch,
)
from app.services import audit
from app.services import pricing as pricing_service

router = APIRouter(prefix="/pricing", tags=["pricing"])


@router.get("/services", response_model=list[ServicePriceOut])
async def list_prices(
    kind: ServiceKind | None = Query(default=None),
    active: bool = Query(default=False, description="Only what is currently offered."),
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_CONFIG_READ)),
) -> list[ServicePriceOut]:
    rows = await pricing_service.list_prices(
        ctx.session, ctx.tenant_id, kind=kind, active_only=active
    )
    return [ServicePriceOut.of(row) for row in rows]


@router.post("/services", response_model=ServicePriceOut, status_code=status.HTTP_201_CREATED)
async def create_price(
    payload: ServicePriceIn,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_CONFIG_WRITE)),
) -> ServicePriceOut:
    row = await pricing_service.create_price(
        ctx.session,
        ctx.tenant_id,
        kind=payload.kind,
        code=payload.code,
        name=payload.name,
        price=payload.price,
        tax_pct=payload.tax_pct,
        active=payload.active,
        note=payload.note,
    )
    await audit.record(
        ctx.session, action="pricing.create", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="service_price",
        entity_id=str(row.id), meta={"kind": row.kind.value, "code": row.code, "price": float(row.price)},
    )
    return ServicePriceOut.of(row)


@router.patch("/services/{price_id}", response_model=ServicePriceOut)
async def update_price(
    price_id: str,
    payload: ServicePricePatch,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_CONFIG_WRITE)),
) -> ServicePriceOut:
    row = await pricing_service.update_price(
        ctx.session,
        ctx.tenant_id,
        parse_uuid(price_id, what="price"),
        payload.model_dump(exclude_unset=True),
    )
    await audit.record(
        ctx.session, action="pricing.update", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="service_price",
        entity_id=str(row.id), meta={"price": float(row.price)},
    )
    return ServicePriceOut.of(row)


@router.delete("/services/{price_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_price(
    price_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_CONFIG_WRITE)),
) -> None:
    await pricing_service.delete_price(
        ctx.session, ctx.tenant_id, parse_uuid(price_id, what="price")
    )
    await audit.record(
        ctx.session, action="pricing.delete", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="service_price",
        entity_id=price_id,
    )


@router.post(
    "/services/import-lab-catalogue",
    response_model=list[ServicePriceOut],
    status_code=status.HTTP_201_CREATED,
)
async def import_lab_catalogue(
    payload: LabCatalogueImportIn,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_CONFIG_WRITE)),
) -> list[ServicePriceOut]:
    """Fill the lab half of the tariff from the order catalogue.

    Setting a price per scan means having a row per scan; this creates the ones
    that are missing at a single starting figure, ready to be edited. Anything
    already priced is untouched.
    """
    created = await pricing_service.import_lab_catalogue(
        ctx.session,
        ctx.tenant_id,
        clinical_type=ctx.tenant.clinical_type,
        price=payload.price,
        tax_pct=payload.tax_pct,
    )
    await audit.record(
        ctx.session, action="pricing.import", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="service_price",
        entity_id="", meta={"created": len(created)},
    )
    return [ServicePriceOut.of(row) for row in created]


@router.patch("/doctors/{doctor_id}", response_model=UserOut)
async def update_doctor_fee(
    doctor_id: str,
    payload: DoctorFeeIn,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_CONFIG_WRITE)),
) -> UserOut:
    doctor = await pricing_service.set_doctor_fee(
        ctx.session, ctx.tenant_id, parse_uuid(doctor_id, what="clinician"), payload.consultation_fee
    )
    await audit.record(
        ctx.session, action="pricing.doctor_fee", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="user",
        entity_id=str(doctor.id), meta={"consultation_fee": payload.consultation_fee},
    )
    return UserOut.of(doctor)
