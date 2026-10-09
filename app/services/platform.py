"""§3.1 — what the platform console can do.

Creating a clinic claims its subdomain, writes a default configuration, and
mints the first admin credential. There is nothing to switch on: every clinic
has every module from the moment it exists. Cross-tenant reads
are done one tenant at a time with that tenant bound to the session, so even
the console never runs a query that could span two clinics by accident — there
is no RLS bypass role anywhere in this application.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.core.permissions import Role
from app.core.tenancy import slug_problem
from app.db.session import set_tenant
from app.models.clinical import Appointment, Encounter, Patient
from app.models.enums import AccountStatus, TenantStatus
from app.models.identity import PlatformUser, User
from app.models.ops import Invoice, Payment
from app.models.tenancy import (
    ClinicalType,
    LabTest,
    MedicineSuggestion,
    PlatformSettings,
    Tenant,
    TenantConfig,
)
from app.schemas.tenancy import TenantCreateIn, TenantPatchIn

PLATFORM_SETTINGS_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@dataclass(frozen=True)
class Timings:
    """The operational knobs `platform_settings` carries, resolved once per
    unit of work (one billing run, one invitation) and passed down as a plain
    value from there — never read from a module-level global inside a pure
    function, so a run already in progress cannot see two different configs
    partway through (or bill one clinic at one fee and the next at another)."""

    invite_token_ttl_seconds: int
    reset_token_ttl_seconds: int
    rent_due_days: int
    rent_grace_days: int
    rent_reminder_days_before_due: int
    #: the flat monthly clinic fee — one figure for every module, every clinic
    subscription_fee_monthly: Decimal = Decimal("0")


async def get_platform_settings(session: AsyncSession) -> PlatformSettings:
    """The one row. Created on first read if the migration's seed somehow
    hasn't run yet — belt to that braces, never expected to fire in practice."""
    row = await session.get(PlatformSettings, PLATFORM_SETTINGS_ID)
    if row is None:
        row = PlatformSettings(id=PLATFORM_SETTINGS_ID)
        session.add(row)
        await session.flush()
    return row


async def get_timings(session: AsyncSession) -> Timings:
    row = await get_platform_settings(session)
    return Timings(
        invite_token_ttl_seconds=row.invite_token_ttl_seconds,
        reset_token_ttl_seconds=row.reset_token_ttl_seconds,
        rent_due_days=row.rent_due_days,
        rent_grace_days=row.rent_grace_days,
        rent_reminder_days_before_due=row.rent_reminder_days_before_due,
        subscription_fee_monthly=Decimal(str(row.subscription_fee_monthly or 0)).quantize(Decimal("0.01")),
    )


async def update_platform_settings(session: AsyncSession, changes: dict) -> PlatformSettings:
    row = await get_platform_settings(session)
    for key, value in changes.items():
        setattr(row, key, value)
    await session.flush()
    return row


async def create_lab_test(
    session: AsyncSession, code: str, name: str, panel: str, clinical_types: list[str] | None,
) -> LabTest:
    existing = await session.get(LabTest, code)
    if existing is not None:
        raise Conflict("A lab test with that code already exists.")
    row = LabTest(code=code, name=name, panel=panel, clinical_types=clinical_types)
    session.add(row)
    await session.flush()
    return row


async def update_lab_test(session: AsyncSession, code: str, changes: dict) -> LabTest:
    row = await session.get(LabTest, code)
    if row is None:
        raise NotFound("No such lab test.")
    for key, value in changes.items():
        setattr(row, key, value)
    await session.flush()
    return row


async def delete_lab_test(session: AsyncSession, code: str) -> None:
    row = await session.get(LabTest, code)
    if row is None:
        raise NotFound("No such lab test.")
    await session.delete(row)
    await session.flush()


async def create_medicine(
    session: AsyncSession, medicine: str, strength: str, dosage: str, frequency: str,
    duration: str, clinical_types: list[str] | None,
) -> MedicineSuggestion:
    row = MedicineSuggestion(
        medicine=medicine, strength=strength, dosage=dosage, frequency=frequency,
        duration=duration, clinical_types=clinical_types,
    )
    session.add(row)
    await session.flush()
    return row


async def update_medicine(session: AsyncSession, medicine_id: uuid.UUID, changes: dict) -> MedicineSuggestion:
    row = await session.get(MedicineSuggestion, medicine_id)
    if row is None:
        raise NotFound("No such medicine suggestion.")
    for key, value in changes.items():
        setattr(row, key, value)
    await session.flush()
    return row


async def delete_medicine(session: AsyncSession, medicine_id: uuid.UUID) -> None:
    row = await session.get(MedicineSuggestion, medicine_id)
    if row is None:
        raise NotFound("No such medicine suggestion.")
    await session.delete(row)
    await session.flush()


async def update_clinical_type_copy(session: AsyncSession, key: str, changes: dict) -> ClinicalType:
    row = await session.get(ClinicalType, key)
    if row is None:
        raise NotFound("No such clinical type.")
    for field, value in changes.items():
        setattr(row, field, value)
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

    # the consent form for its kind of practice, copied from the system template
    from app.services import consent as consent_service  # deferred: consent imports tenancy models

    await consent_service.create_form(
        session, tenant, consent_service.consent_templates.default_for(tenant.clinical_type).key,
        actor_label="System", actor_id=None,
    )
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
        rows.append(
            {
                "tenant": tenant,
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
