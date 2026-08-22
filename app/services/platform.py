"""§3.1 — what the platform console can do.

Creating a clinic claims its subdomain, seeds the core entitlements, writes a
default configuration, and mints the first admin credential. Cross-tenant reads
are done one tenant at a time with that tenant bound to the session, so even
the console never runs a query that could span two clinics by accident — there
is no RLS bypass role anywhere in this application.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.core.permissions import Role
from app.core.security import hash_password
from app.core.tenancy import slug_problem
from app.db.session import set_tenant
from app.models.clinical import Appointment, Encounter, Patient
from app.models.enums import AccountStatus, TenantFeatureStatus, TenantStatus
from app.models.identity import User
from app.models.ops import Invoice, Payment
from app.models.tenancy import ClinicalType, Feature, Tenant, TenantConfig, TenantFeature
from app.schemas.tenancy import TenantCreateIn
from app.services.tenants import CORE_FEATURES


def initials_of(name: str) -> str:
    parts = [p for p in name.split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


async def list_tenants(session: AsyncSession) -> list[Tenant]:
    return list((await session.execute(select(Tenant).order_by(Tenant.name))).scalars())


async def create_tenant(
    session: AsyncSession, payload: TenantCreateIn, *, today: date
) -> tuple[Tenant, User]:
    taken = set(
        (await session.execute(select(Tenant.slug))).scalars()
    )
    problem = slug_problem(payload.slug, taken)
    if problem:
        raise Conflict(problem)

    clinical_type = await session.get(ClinicalType, payload.clinical_type)
    if clinical_type is None:
        raise NotFound("No such clinical type.")

    prefix = payload.slug[:2].upper()
    tenant = Tenant(
        name=payload.name,
        slug=payload.slug,
        clinical_type=clinical_type.key,
        status=TenantStatus.TRIAL,
        city=payload.city,
        plan="Trial",
        since=today,
    )
    session.add(tenant)
    await session.flush()

    # From here on the session is bound to the new clinic, so every row written
    # below is written *as* that tenant and checked by the same policies.
    await set_tenant(session, tenant.id)

    session.add(
        TenantConfig(
            tenant_id=tenant.id,
            mrn_prefix=prefix,
            token_prefix=prefix[:1],
            invoice_prefix=f"{prefix}-INV",
        )
    )

    core = set(CORE_FEATURES)
    for feature in (await session.execute(select(Feature))).scalars():
        session.add(
            TenantFeature(
                tenant_id=tenant.id,
                feature_key=feature.key,
                status=TenantFeatureStatus.ACTIVE if feature.key in core else TenantFeatureStatus.INACTIVE,
                activated_at=today if feature.key in core else None,
            )
        )

    admin = User(
        tenant_id=tenant.id,
        name=payload.admin_name,
        role=Role.TENANT_ADMIN.value,
        email=str(payload.admin_email).lower(),
        password_hash=hash_password(payload.admin_password),
        initials=initials_of(payload.admin_name),
        status=AccountStatus.ACTIVE,
    )
    session.add(admin)
    await session.flush()
    return tenant, admin


async def set_tenant_status(
    session: AsyncSession, tenant_id: uuid.UUID, status: TenantStatus
) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("No such clinic.")
    tenant.status = status
    await session.flush()
    return tenant


async def snapshot(session: AsyncSession) -> dict:
    """Per-clinic counts for the console.

    Each clinic is counted with the session bound to it, one at a time.
    """
    tenants = await list_tenants(session)
    rows = []
    totals = {"tenants": len(tenants), "patients": 0.0, "encounters": 0.0, "revenue": 0.0}

    for tenant in tenants:
        await set_tenant(session, tenant.id)
        counts = {
            "users": await _count(session, User, tenant.id),
            "patients": await _count(session, Patient, tenant.id),
            "appointments": await _count(session, Appointment, tenant.id),
            "encounters": await _count(session, Encounter, tenant.id),
            "invoices": await _count(session, Invoice, tenant.id),
        }
        revenue = float(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.tenant_id == tenant.id)
                )
            ).scalar()
            or 0
        )
        last_activity = (
            await session.execute(
                select(func.max(Encounter.date)).where(Encounter.tenant_id == tenant.id)
            )
        ).scalar()
        active = set(
            (
                await session.execute(
                    select(TenantFeature.feature_key).where(
                        TenantFeature.tenant_id == tenant.id,
                        TenantFeature.status == TenantFeatureStatus.ACTIVE,
                    )
                )
            ).scalars()
        )

        rows.append(
            {
                "tenant": tenant,
                "active_features": sorted(active),
                "revenue": round(revenue, 2),
                "last_activity": last_activity.isoformat() if last_activity else None,
                **counts,
            }
        )
        totals["patients"] += counts["patients"]
        totals["encounters"] += counts["encounters"]
        totals["revenue"] += revenue

    await set_tenant(session, None)
    totals["revenue"] = round(totals["revenue"], 2)
    return {"rows": rows, "totals": totals}


async def _count(session: AsyncSession, model, tenant_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count(model.id)).where(model.tenant_id == tenant_id)
            )
        ).scalar()
        or 0
    )
