"""The super-admin console (§3.1).

Served on `admin.<apex>` only, to platform staff who have no tenant of their
own — `current_platform_user` refuses these routes on any other address, so a
clinic subdomain is not a second door into the console.

Creating a clinic is done here, never by a self-service form inside a tenant.
Nobody is handed a password: the first administrator is created without one and
emailed a single-use link.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    client_ip,
    current_platform_user,
    current_super_admin,
    db_session,
    parse_uuid,
    user_agent,
)
from app.core.config import settings
from app.core.errors import NotFound
from app.db.session import set_tenant
from app.models.enums import AuthTokenPurpose
from app.models.identity import PlatformUser
from app.models.tenancy import ClinicalType, Feature, LabTest, MedicineSuggestion, Tenant
from app.schemas.common import zone
from app.schemas.identity import PlatformUserOut, UserOut
from app.schemas.support import SupportRequestOut
from app.schemas.tenancy import (
    ClinicalTypeOut,
    FeatureOut,
    FeaturePriceIn,
    ImpersonationOut,
    LabTestAdminOut,
    LabTestIn,
    LabTestPatch,
    MedicineAdminOut,
    MedicineIn,
    MedicinePatch,
    PlatformSettingsOut,
    PlatformSettingsPatch,
    PlatformSnapshotOut,
    PlatformTenantSummary,
    SlugCheckOut,
    TenantCreatedOut,
    TenantCreateIn,
    TenantFeatureOut,
    TenantFeatureSetIn,
    TenantOut,
    TenantPatchIn,
    TenantStatusIn,
)
from app.services import audit, invitations, mailer
from app.services import auth as auth_service
from app.services import platform as platform_service
from app.services import rent as rent_service
from app.services import support as support_service
from app.services import tenants as tenant_service
from app.services.tenants import get_config

log = logging.getLogger("pms.platform")

router = APIRouter(prefix="/platform", tags=["platform"])


@router.get("/me", response_model=PlatformUserOut)
async def me(staff: PlatformUser = Depends(current_platform_user)) -> PlatformUserOut:
    return PlatformUserOut.of(staff)


@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants(
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> list[TenantOut]:
    rows = await platform_service.list_tenants(session)
    return [TenantOut.of(t) for t in rows]


@router.get("/tenants/slug-check", response_model=SlugCheckOut)
async def check_slug(
    slug: str = Query(min_length=1, max_length=40),
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> SlugCheckOut:
    """Is this subdomain free, and if not, why not.

    Answered while somebody types, so the create form can say "taken" before
    the submit rather than after it. It reserves nothing — the unique index is
    still what settles two consoles reaching for the same name at once.
    """
    normalised = slug.strip().lower()
    available, problem = await platform_service.check_slug(session, normalised)
    return SlugCheckOut(
        slug=normalised,
        available=available,
        problem=problem,
        url=settings.tenant_origin(normalised),
    )


@router.post("/tenants", response_model=TenantCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    payload: TenantCreateIn,
    request: Request,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> TenantCreatedOut:
    """Claims the subdomain, seeds core entitlements, clones the clinical-type
    field set, writes a configuration, and invites the first administrator.

    The console never sees a password, because none is created. The admin row
    is written with no hash at all, and the person named on it is emailed a
    single-use link to choose one. If that email cannot be sent the clinic
    still exists — undoing the subdomain claim would be worse — but the
    response says so plainly, because a clinic nobody can sign into is not a
    finished job.
    """
    tenant, admin = await platform_service.create_tenant(session, payload, today=date.today())
    await get_config(session, tenant.id)
    await audit.record_for_tenant(
        session, tenant.id, action="platform.tenant_create", actor_kind="platform",
        actor_id=staff.id, actor_label=f"{staff.name} <{staff.email}>", entity="tenant",
        entity_id=str(tenant.id), ip=client_ip(request), user_agent=user_agent(request),
        meta={"slug": tenant.slug},
    )

    sent, url = await _invite(
        session, user=admin, tenant=tenant, staff=staff, request=request,
        purpose=AuthTokenPurpose.INVITE,
    )
    return TenantCreatedOut(
        tenant=TenantOut.of(tenant),
        admin_user_id=str(admin.id),
        admin_email=admin.email,
        invitation_sent=sent,
        invitation_url=url,
    )


async def _invite(
    session: AsyncSession,
    *,
    user,
    tenant: Tenant,
    staff: PlatformUser,
    request: Request,
    purpose: AuthTokenPurpose,
) -> tuple[bool, str | None]:
    """Mint a link, email it, and record what happened.

    The token is written whether or not the email leaves — so a send that fails
    can be retried with "resend" instead of stranding the account. The URL is
    returned to the console only under the `console` mail transport, which is
    development; with a real transport the link exists in one inbox and nowhere
    else.
    """
    # `record_for_tenant` releases the binding when it is done, and a console
    # session has none of its own — so the clinic is bound again here. The
    # token row names a tenant and its policy is checked like any other write:
    # the console gets no special path through row-level security either.
    await set_tenant(session, tenant.id)

    _, raw = await invitations.issue_for_user(
        session,
        user=user,
        tenant=tenant,
        purpose=purpose,
        issued_by_label=f"{staff.name} <{staff.email}>",
        ip=client_ip(request),
        user_agent=user_agent(request),
    )
    url = invitations.link_for(slug=tenant.slug, purpose=purpose, raw_token=raw)

    sent = True
    try:
        await invitations.send_link(user=user, tenant=tenant, purpose=purpose, raw_token=raw)
    except mailer.MailError as exc:
        sent = False
        log.error("could not send %s to %s: %s", purpose.value, user.email, exc)

    await audit.record_for_tenant(
        session, tenant.id, action=f"platform.{purpose.value}_sent", actor_kind="platform",
        actor_id=staff.id, actor_label=f"{staff.name} <{staff.email}>", entity="user",
        entity_id=str(user.id), outcome="ok" if sent else "failed",
        ip=client_ip(request), user_agent=user_agent(request),
        meta={"email": user.email},
    )
    return sent, url if settings.mail_provider == "console" else None


@router.patch("/tenants/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: str,
    payload: TenantPatchIn,
    request: Request,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> TenantOut:
    """Edit a clinic's record.

    Name, city, plan, timezone and clinical type. Not the subdomain — that is
    the clinic's address, it is inside every live session token, and changing
    it would sign everybody out and break every bookmark at once.
    """
    tenant = await platform_service.update_tenant(
        session, parse_uuid(tenant_id, what="clinic"), payload
    )
    changed = sorted(payload.model_dump(exclude_unset=True, exclude_none=True))
    if "billing_exempt" in changed:
        # Exempting a clinic lifts any hold at once; un-exempting it puts a
        # clinic with an overdue bill straight back on hold. Either way the
        # hold follows the bills, not the order the switches were thrown in.
        await set_tenant(session, tenant.id)
        await rent_service.reconcile_hold(session, tenant, rent_service.clinic_today(tenant))
        await set_tenant(session, None)
    await audit.record_for_tenant(
        session, tenant.id, action="platform.tenant_update", actor_kind="platform",
        actor_id=staff.id, actor_label=f"{staff.name} <{staff.email}>", entity="tenant",
        entity_id=str(tenant.id), ip=client_ip(request), user_agent=user_agent(request),
        meta={"fields": changed},
    )
    return TenantOut.of(tenant)


@router.get("/tenants/{tenant_id}/features", response_model=list[TenantFeatureOut])
async def tenant_features(
    tenant_id: str,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> list[TenantFeatureOut]:
    """Which modules this clinic has, as the console sees them."""
    clinic = await _clinic(session, tenant_id)
    rows = await tenant_service.list_features(session, clinic.id)
    return [TenantFeatureOut.of(r) for r in rows]


@router.put("/tenants/{tenant_id}/features/{feature_key}", response_model=TenantFeatureOut)
async def set_tenant_feature(
    tenant_id: str,
    feature_key: str,
    payload: TenantFeatureSetIn,
    request: Request,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> TenantFeatureOut:
    """Switch a module on or off for one clinic.

    The clinic-side route records a clinic *buying* something; this is the
    platform's own hand on the switch, for onboarding, for a trial, and for
    turning off what was never paid for. Both write the same row, so there is
    one answer to "is this on" and the feature guard reads it either way.
    """
    clinic = await _clinic(session, tenant_id)
    row = await platform_service.set_tenant_feature(
        session,
        clinic.id,
        feature_key,
        status=payload.status,
        today=datetime.now(zone(clinic.timezone)).date(),
    )
    await audit.record_for_tenant(
        session, clinic.id, action=f"platform.feature_{payload.status.value}",
        actor_kind="platform", actor_id=staff.id, actor_label=f"{staff.name} <{staff.email}>",
        entity="feature", entity_id=feature_key, ip=client_ip(request),
        user_agent=user_agent(request),
    )
    return TenantFeatureOut.of(row)


@router.get("/tenants/{tenant_id}/support-requests", response_model=list[SupportRequestOut])
async def tenant_support_requests(
    tenant_id: str,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> list[SupportRequestOut]:
    """What this clinic has asked the desk for, newest first — the list a
    super admin works from when granting modules."""
    clinic = await _clinic(session, tenant_id)
    rows = await support_service.list_for_tenant(session, clinic.id)
    return [SupportRequestOut.of(r, clinic.timezone) for r in rows]


@router.get("/tenants/{tenant_id}/users", response_model=list[UserOut])
async def tenant_users(
    tenant_id: str,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> list[UserOut]:
    """This clinic's staff, so the console can see who is still waiting on an
    invitation. Support accounts are left out — they are the console's own
    plumbing, not people who work here."""
    from app.models.identity import User

    clinic = await _clinic(session, tenant_id)
    rows = (
        await session.execute(
            select(User)
            .where(User.tenant_id == clinic.id, User.is_support.is_(False))
            .order_by(User.name)
        )
    ).scalars()
    return [UserOut.of(u, clinic.timezone) for u in rows]


@router.post("/tenants/{tenant_id}/users/{user_id}/invite", response_model=TenantCreatedOut)
async def resend_invitation(
    tenant_id: str,
    user_id: str,
    request: Request,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> TenantCreatedOut:
    """Send this account its link again.

    Issuing a new one spends the previous, so the email that just went out is
    the only one that works — a link that has been sitting in an old inbox
    stops being a way in the moment support sends a fresh one.
    """
    from app.models.identity import User

    clinic = await _clinic(session, tenant_id)
    user = (
        await session.execute(
            select(User).where(
                User.id == parse_uuid(user_id, what="account"),
                User.tenant_id == clinic.id,
                User.is_support.is_(False),
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFound("No such account.")

    purpose = (
        AuthTokenPurpose.INVITE if user.password_hash is None else AuthTokenPurpose.RESET
    )
    sent, url = await _invite(
        session, user=user, tenant=clinic, staff=staff, request=request, purpose=purpose
    )
    return TenantCreatedOut(
        tenant=TenantOut.of(clinic),
        admin_user_id=str(user.id),
        admin_email=user.email,
        invitation_sent=sent,
        invitation_url=url,
    )


@router.post("/tenants/{tenant_id}/impersonate", response_model=ImpersonationOut)
async def impersonate(
    tenant_id: str,
    request: Request,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> ImpersonationOut:
    """Open a support session inside a clinic.

    What comes back is an access token and nothing else — no refresh token and
    no cookie — so it lasts one access-token lifetime and then has to be opened
    again from here. "Somebody from the platform looked at this clinic" stays a
    thing that happened at a moment, rather than a standing key.

    Inside, it carries the permissions of a practice admin, because that is the
    point of it. Everything it does is recorded against the platform person who
    opened it, not against the clinic account it writes through, and opening it
    is itself an audit line.
    """
    clinic = await _clinic(session, tenant_id)
    acting = await platform_service.support_user_for(session, tenant=clinic, staff=staff)
    token, _ = await auth_service.open_support_session(
        session,
        staff=staff,
        tenant=clinic,
        acting_user=acting,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )
    return ImpersonationOut(
        access_token=token,
        expires_in=settings.access_token_ttl_seconds,
        tenant=TenantOut.of(clinic),
        url=settings.tenant_origin(clinic.slug),
    )


async def _clinic(session: AsyncSession, tenant_id: str) -> Tenant:
    """One clinic by id, with the session bound to it.

    Console routes that touch a clinic's own rows bind the tenant first and
    read only within it — there is no bypass role here either.
    """
    clinic = await session.get(Tenant, parse_uuid(tenant_id, what="clinic"))
    if clinic is None:
        raise NotFound("No such clinic.")
    await set_tenant(session, clinic.id)
    return clinic


@router.patch("/tenants/{tenant_id}/status", response_model=TenantOut)
async def set_tenant_status(
    tenant_id: str,
    payload: TenantStatusIn,
    request: Request,
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
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
    session: AsyncSession = Depends(db_session, scope="function"),
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


@router.get("/settings", response_model=PlatformSettingsOut)
async def get_platform_settings_route(
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> PlatformSettingsOut:
    row = await platform_service.get_platform_settings(session)
    return PlatformSettingsOut.of(row)


@router.patch("/settings", response_model=PlatformSettingsOut)
async def update_platform_settings_route(
    payload: PlatformSettingsPatch,
    request: Request,
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> PlatformSettingsOut:
    """What every new clinic starts with, and where a clinic that can't
    self-serve is told to write. Super-admin only, like every other write
    in this file that shapes what the whole platform does next."""
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    row = await platform_service.update_platform_settings(session, changes)
    await audit.record(
        session, action="platform_settings.update", actor_kind="platform",
        actor_id=staff.id, actor_label=staff.name, entity="platform_settings",
        entity_id=str(row.id), ip=client_ip(request), user_agent=user_agent(request),
        meta={"changes": changes},
    )
    return PlatformSettingsOut.of(row)


@router.get("/catalog/lab-tests", response_model=list[LabTestAdminOut])
async def platform_lab_tests(
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> list[LabTestAdminOut]:
    rows = (await session.execute(select(LabTest).order_by(LabTest.panel, LabTest.name))).scalars()
    return [LabTestAdminOut.of(t) for t in rows]


@router.post("/catalog/lab-tests", response_model=LabTestAdminOut, status_code=status.HTTP_201_CREATED)
async def create_platform_lab_test(
    payload: LabTestIn,
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> LabTestAdminOut:
    row = await platform_service.create_lab_test(
        session, payload.code, payload.name, payload.panel, payload.clinical_types
    )
    return LabTestAdminOut.of(row)


@router.patch("/catalog/lab-tests/{code}", response_model=LabTestAdminOut)
async def update_platform_lab_test(
    code: str,
    payload: LabTestPatch,
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> LabTestAdminOut:
    changes = payload.model_dump(exclude_unset=True)
    row = await platform_service.update_lab_test(session, code, changes)
    return LabTestAdminOut.of(row)


@router.delete("/catalog/lab-tests/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_platform_lab_test(
    code: str,
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> None:
    await platform_service.delete_lab_test(session, code)


@router.get("/catalog/medicines", response_model=list[MedicineAdminOut])
async def platform_medicines(
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> list[MedicineAdminOut]:
    rows = (await session.execute(select(MedicineSuggestion).order_by(MedicineSuggestion.medicine))).scalars()
    return [MedicineAdminOut.of(m) for m in rows]


@router.post("/catalog/medicines", response_model=MedicineAdminOut, status_code=status.HTTP_201_CREATED)
async def create_platform_medicine(
    payload: MedicineIn,
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> MedicineAdminOut:
    row = await platform_service.create_medicine(
        session, payload.medicine, payload.strength, payload.dosage,
        payload.frequency, payload.duration, payload.clinical_types,
    )
    return MedicineAdminOut.of(row)


@router.patch("/catalog/medicines/{medicine_id}", response_model=MedicineAdminOut)
async def update_platform_medicine(
    medicine_id: str,
    payload: MedicinePatch,
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> MedicineAdminOut:
    changes = payload.model_dump(exclude_unset=True)
    row = await platform_service.update_medicine(session, parse_uuid(medicine_id, what="medicine"), changes)
    return MedicineAdminOut.of(row)


@router.delete("/catalog/medicines/{medicine_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_platform_medicine(
    medicine_id: str,
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> None:
    await platform_service.delete_medicine(session, parse_uuid(medicine_id, what="medicine"))


@router.get("/catalog/clinical-types", response_model=list[ClinicalTypeOut])
async def clinical_types(
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> list[ClinicalTypeOut]:
    rows = (await session.execute(select(ClinicalType).order_by(ClinicalType.label))).scalars()
    return [ClinicalTypeOut.of(c) for c in rows]


@router.get("/catalog/features", response_model=list[FeatureOut])
async def features(
    staff: PlatformUser = Depends(current_platform_user),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> list[FeatureOut]:
    rows = (await session.execute(select(Feature).order_by(Feature.name))).scalars()
    return [FeatureOut.of(f) for f in rows]


@router.patch("/catalog/features/{feature_key}", response_model=FeatureOut)
async def set_feature_price(
    feature_key: str,
    payload: FeaturePriceIn,
    request: Request,
    staff: PlatformUser = Depends(current_super_admin),
    session: AsyncSession = Depends(db_session, scope="function"),
) -> FeatureOut:
    """Set what a module costs per month.

    A super admin's call, not the desk's: the price is what every clinic on
    the platform is quoted and what the console counts revenue from, so the
    `support` role is refused with a 403 — it can switch a module on for a
    clinic, but not decide what that clinic will be charged for it.

    The row is the catalogue's, not a clinic's, so the audit line names no
    tenant. It records the old figure beside the new one, because "who changed
    the price of Billing, and from what" is exactly the question it is for.
    """
    feature, previous = await platform_service.set_feature_price(
        session, feature_key, payload.price_monthly
    )
    await audit.record(
        session, action="platform.feature_price", actor_kind="platform",
        actor_id=staff.id, actor_label=f"{staff.name} <{staff.email}>",
        entity="feature", entity_id=feature.key, ip=client_ip(request),
        user_agent=user_agent(request),
        meta={"from": float(previous), "to": float(feature.price_monthly)},
    )
    return FeatureOut.of(feature)
