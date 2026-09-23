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
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.core.permissions import Role
from app.core.tenancy import slug_problem
from app.db.session import set_tenant
from app.models.clinical import Appointment, Encounter, Patient
from app.models.enums import AccountStatus, TenantFeatureStatus, TenantStatus
from app.models.identity import PlatformUser, User
from app.models.ops import Invoice, Payment
from app.models.tenancy import ClinicalType, Feature, PlatformSettings, Tenant, TenantConfig, TenantFeature
from app.schemas.tenancy import TenantCreateIn, TenantPatchIn
from app.services.tenants import CORE_FEATURES

PLATFORM_SETTINGS_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def get_platform_settings(session: AsyncSession) -> PlatformSettings:
    """The one row. Created on first read if the migration's seed somehow
    hasn't run yet — belt to that braces, never expected to fire in practice."""
    row = await session.get(PlatformSettings, PLATFORM_SETTINGS_ID)
    if row is None:
        row = PlatformSettings(id=PLATFORM_SETTINGS_ID)
        session.add(row)
        await session.flush()
    return row


async def update_platform_settings(session: AsyncSession, changes: dict) -> PlatformSettings:
    row = await get_platform_settings(session)
    for key, value in changes.items():
        setattr(row, key, value)
    await session.flush()
    return row


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

    defaults = await get_platform_settings(session)
    session.add(
        TenantConfig(
            tenant_id=tenant.id,
            mrn_prefix=prefix,
            token_prefix=prefix[:1],
            invoice_prefix=f"{prefix}-INV",
            day_start=defaults.default_day_start,
            day_end=defaults.default_day_end,
            slot_minutes=defaults.default_slot_minutes,
            default_duration=defaults.default_duration,
            tax_pct=defaults.default_tax_pct,
            payment_terms_days=defaults.default_payment_terms_days,
            expiry_alert_days=defaults.default_expiry_alert_days,
            currency=defaults.default_currency,
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

    # No password is chosen here, by anyone. The account exists, has no hash,
    # and cannot be signed into; the caller mints an invitation and emails a
    # link, and the person who receives it picks their own.
    admin = User(
        tenant_id=tenant.id,
        name=payload.admin_name,
        role=Role.TENANT_ADMIN.value,
        email=str(payload.admin_email).lower(),
        password_hash=None,
        initials=initials_of(payload.admin_name),
        status=AccountStatus.INVITED,
    )
    session.add(admin)
    await session.flush()
    return tenant, admin


async def check_slug(session: AsyncSession, slug: str) -> tuple[bool, str | None]:
    """Whether a subdomain can be claimed, and why not when it cannot.

    The same `slug_problem` the create path uses, so the answer a form gets
    while someone types is the answer they will get when they submit. It is a
    convenience, not a reservation: two consoles racing for one subdomain are
    still settled by the unique index.
    """
    normalised = (slug or "").strip().lower()
    taken = set((await session.execute(select(Tenant.slug))).scalars())
    problem = slug_problem(normalised, taken)
    return problem is None, problem


async def update_tenant(
    session: AsyncSession, tenant_id: uuid.UUID, payload: TenantPatchIn
) -> Tenant:
    """Edit a clinic's record from the console.

    `slug` is not here and cannot be: it is the clinic's address, every session
    token carries it, and changing it would strand every signed-in browser and
    every link anyone has bookmarked. A clinic that needs a different subdomain
    is a migration, not a field.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("No such clinic.")

    changes = payload.model_dump(exclude_unset=True, exclude_none=True)

    if "clinical_type" in changes:
        clinical_type = await session.get(ClinicalType, changes["clinical_type"])
        if clinical_type is None:
            raise NotFound("No such clinical type.")

    for key, value in changes.items():
        setattr(tenant, key, value)
    await session.flush()
    return tenant


async def set_tenant_feature(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    feature_key: str,
    *,
    status: TenantFeatureStatus,
    today: date,
) -> TenantFeature:
    """Switch a module on or off for one clinic, from the console.

    The clinic-side route (`/tenant/features/{key}/purchase`) records a clinic
    buying something. This one is the platform's own hand on the switch — for
    onboarding, for a trial, and for turning something off that was never paid
    for. Both write the same row, so there is one answer to "is this on".

    A core feature cannot be taken away: the product does not work without it.
    """
    if feature_key in CORE_FEATURES and status != TenantFeatureStatus.ACTIVE:
        raise Conflict("That module is part of the core product and cannot be switched off.")

    feature = await session.get(Feature, feature_key)
    if feature is None:
        raise NotFound("No such module.")

    await set_tenant(session, tenant_id)
    row = (
        await session.execute(
            select(TenantFeature).where(
                TenantFeature.tenant_id == tenant_id, TenantFeature.feature_key == feature_key
            )
        )
    ).scalar_one_or_none()

    was_active = row is not None and row.status == TenantFeatureStatus.ACTIVE
    if row is None:
        row = TenantFeature(tenant_id=tenant_id, feature_key=feature_key, status=status)
        session.add(row)
    else:
        row.status = status

    # `activated_at` records when it was last switched on — the console counts
    # billing from it, so a module switched back on starts again from today
    # rather than looking billed for the months it was off. Re-sending "on"
    # for a module already on leaves it alone, and a deactivation leaves the
    # last date in place.
    if status == TenantFeatureStatus.ACTIVE and not was_active:
        row.activated_at = today

    await session.flush()
    return row


async def set_feature_price(
    session: AsyncSession, feature_key: str, price_monthly: float
) -> tuple[Feature, Decimal]:
    """Change what a module costs, for every clinic at once.

    The catalogue is the platform's, not a clinic's, so this is one row and no
    tenant is bound. A core module carries no price — it is in every plan and
    cannot be bought or dropped — so pricing one is refused rather than stored
    and then contradicted by every screen that says "included".

    Returns the row and the price it replaced, for the audit line.
    """
    feature = await session.get(Feature, feature_key)
    if feature is None:
        raise NotFound("No such module.")
    if feature.is_core or feature_key in CORE_FEATURES:
        raise Conflict("That module is part of the core product and carries no price.")

    previous = Decimal(feature.price_monthly or 0)
    feature.price_monthly = Decimal(str(price_monthly)).quantize(Decimal("0.01"))
    await session.flush()
    return feature, previous


SUPPORT_EMAIL_DOMAIN = "platform.invalid"


async def support_user_for(
    session: AsyncSession, *, tenant: Tenant, staff: PlatformUser
) -> User:
    """The clinic account a platform person acts through, created on demand.

    A support session needs a real `users` row: half the product stamps an
    author onto what it writes, and those columns have foreign keys. Inventing
    an id would fail on the first write.

    So there is one row per platform person per clinic. It has no password hash
    and is never given one, so it is not a way in — `authenticate_user` refuses
    it by name as well. It is `is_support`, so the clinic's staff directory does
    not show it, and its email is at a domain that cannot receive mail.
    """
    await set_tenant(session, tenant.id)
    email = f"support+{staff.id}@{SUPPORT_EMAIL_DOMAIN}"
    row = (
        await session.execute(
            select(User).where(User.tenant_id == tenant.id, User.email == email)
        )
    ).scalar_one_or_none()
    if row is not None:
        # Kept in step with the platform record, so the audit trail and any
        # screen that renders an author stay truthful after a rename.
        row.name = f"{staff.name} (platform support)"
        row.status = AccountStatus.ACTIVE
        row.password_hash = None
        await session.flush()
        return row

    row = User(
        tenant_id=tenant.id,
        name=f"{staff.name} (platform support)",
        role=Role.TENANT_ADMIN.value,
        email=email,
        password_hash=None,
        initials=initials_of(staff.name),
        status=AccountStatus.ACTIVE,
        is_support=True,
    )
    session.add(row)
    await session.flush()
    return row


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
            # Staff only: the support account a console session works
            # through is plumbing, and the clinic's own Team tab leaves it out.
            "users": int(
                (
                    await session.execute(
                        select(func.count(User.id)).where(
                            User.tenant_id == tenant.id, User.is_support.is_(False)
                        )
                    )
                ).scalar()
                or 0
            ),
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
