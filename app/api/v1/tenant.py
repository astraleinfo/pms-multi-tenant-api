"""The clinic itself: its record, its entitlements, its configuration, its
dynamic-form schema, and its staff."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from sqlalchemy import func, select

from app.api.deps import (
    TenantContext,
    client_ip,
    current_tenant,
    parse_uuid,
    require_permission,
    user_agent,
)
from app.core.config import settings
from app.core.errors import Conflict, NotFound
from app.core.permissions import Permission, Role, permissions_for
from app.core.rate_limit import Rate, limiter
from app.core.security import hash_password
from app.models.enums import AccountStatus, AuthTokenPurpose, FieldEntity, FieldScope
from app.models.identity import User
from app.models.tenancy import Feature, FieldDefinition
from app.schemas.identity import PasswordSetIn, UserCreateIn, UserOut, UserStatusIn
from app.schemas.support import SupportRequestIn, SupportRequestOut
from app.schemas.tenancy import (
    FeatureOut,
    FeaturePurchaseIn,
    FieldDefinitionIn,
    FieldDefinitionOut,
    TenantConfigOut,
    TenantConfigPatch,
    TenantFeatureOut,
    TenantOut,
)
from app.services import audit, invitations, mailer
from app.services import auth as auth_service
from app.services import support as support_service
from app.services import tenants as tenant_service

log = logging.getLogger("pms.tenant")

router = APIRouter(tags=["tenant"])


@router.get("/tenant", response_model=TenantOut)
async def current_clinic(ctx: TenantContext = Depends(current_tenant)) -> TenantOut:
    return TenantOut.of(ctx.tenant)


# --- entitlements (§3.2) -----------------------------------------------------


@router.get("/tenant/features", response_model=list[TenantFeatureOut])
async def list_features(ctx: TenantContext = Depends(current_tenant)) -> list[TenantFeatureOut]:
    rows = await tenant_service.list_features(ctx.session, ctx.tenant_id)
    return [TenantFeatureOut.of(r) for r in rows]


@router.get("/tenant/feature-catalog", response_model=list[FeatureOut])
async def feature_catalog(ctx: TenantContext = Depends(current_tenant)) -> list[FeatureOut]:
    rows = (
        await ctx.session.execute(select(Feature).order_by(Feature.is_core.desc(), Feature.name))
    ).scalars()
    return [FeatureOut.of(f) for f in rows]


@router.post("/tenant/features/{feature_key}/purchase", response_model=TenantFeatureOut)
async def purchase_feature(
    feature_key: str,
    payload: FeaturePurchaseIn,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_FEATURE_MANAGE)),
) -> TenantFeatureOut:
    row = await tenant_service.set_feature(
        ctx.session, ctx.tenant_id, feature_key, active=True, today=ctx.today
    )
    await audit.record(
        ctx.session, action="feature.purchase", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="feature", entity_id=feature_key,
        meta={"payment_reference": payload.payment_reference},
    )
    return TenantFeatureOut.of(row)


@router.post("/tenant/features/{feature_key}/deactivate", response_model=TenantFeatureOut)
async def deactivate_feature(
    feature_key: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_FEATURE_MANAGE)),
) -> TenantFeatureOut:
    row = await tenant_service.set_feature(
        ctx.session, ctx.tenant_id, feature_key, active=False, today=ctx.today
    )
    await audit.record(
        ctx.session, action="feature.deactivate", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="feature", entity_id=feature_key,
    )
    return TenantFeatureOut.of(row)


# --- the support desk ---------------------------------------------------------
#
# A clinic cannot switch a module on for itself; it asks, and a super admin
# grants it from the console. The ask is stored here and the desk is emailed by
# the Celery worker, so a slow or broken mail server never holds up the clinic.


@router.post(
    "/tenant/support-requests",
    response_model=SupportRequestOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_support_request(
    payload: SupportRequestIn,
    request: Request,
    response: Response,
    tasks: BackgroundTasks,
    ctx: TenantContext = Depends(current_tenant),
) -> SupportRequestOut:
    """Ask the platform for a module, a walkthrough of one, or anything else.

    Any signed-in member of staff may ask; the email says who did. The same
    person repeating the same ask inside a few minutes gets the earlier request
    back with `200` instead of `201`, and the desk is not emailed again.
    """
    limiter.check(f"support:{ctx.user.id}", Rate.parse(settings.support_request_rate_limit))
    row, created = await support_service.create(
        ctx.session,
        tenant=ctx.tenant,
        user=ctx.user,
        actor_kind=ctx.actor_kind,
        actor_label=ctx.principal.label,
        kind=payload.kind,
        feature_key=payload.feature_key,
        message=payload.message,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )
    if created:
        # Imported here so the API process only touches Celery on this route.
        from app.worker.tasks import enqueue_support_request

        # Publishing is blocking I/O, and slow when Redis is down, so it runs in
        # the threadpool after the response has gone — never on the event loop,
        # and never between the clinic and "your request is in".
        tasks.add_task(enqueue_support_request, ctx.tenant_id, row.id)
    else:
        response.status_code = status.HTTP_200_OK
    return SupportRequestOut.of(row, ctx.tz)


@router.get("/tenant/support-requests", response_model=list[SupportRequestOut])
async def list_support_requests(
    ctx: TenantContext = Depends(current_tenant),
) -> list[SupportRequestOut]:
    """This clinic's recent asks, newest first — so a module page can say
    "requested on …" instead of offering the same button again."""
    rows = await support_service.list_for_tenant(ctx.session, ctx.tenant_id)
    return [SupportRequestOut.of(r, ctx.tz) for r in rows]


# --- configuration (§3.3) ----------------------------------------------------


@router.get("/tenant/config", response_model=TenantConfigOut)
async def get_config(
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_CONFIG_READ)),
) -> TenantConfigOut:
    return TenantConfigOut.of(ctx.config, ctx.tz)


@router.patch("/tenant/config", response_model=TenantConfigOut)
async def update_config(
    payload: TenantConfigPatch,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_CONFIG_WRITE)),
) -> TenantConfigOut:
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in changes.items():
        setattr(ctx.config, key, value)
    await ctx.session.flush()
    await audit.record(
        ctx.session, action="config.update", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="tenant_config",
        entity_id=str(ctx.tenant_id), meta={"fields": sorted(changes)},
    )
    return TenantConfigOut.of(ctx.config, ctx.tz)


# --- dynamic fields (§3.3) ---------------------------------------------------


@router.get("/tenant/fields", response_model=list[FieldDefinitionOut])
async def list_fields(ctx: TenantContext = Depends(current_tenant)) -> list[FieldDefinitionOut]:
    rows = await tenant_service.field_definitions(ctx.session, ctx.tenant_id)
    return [FieldDefinitionOut.of(r) for r in rows]


@router.get("/tenant/schema/{entity}", response_model=list[FieldDefinitionOut])
async def entity_schema(
    entity: FieldEntity, ctx: TenantContext = Depends(current_tenant)
) -> list[FieldDefinitionOut]:
    """common → clinical_type → tenant, merged on `field_key` (step 5)."""
    rows = await tenant_service.effective_schema(ctx.session, ctx.tenant, entity)
    return [FieldDefinitionOut.of(r) for r in rows]


@router.post("/tenant/fields", response_model=FieldDefinitionOut, status_code=status.HTTP_201_CREATED)
async def add_field(
    payload: FieldDefinitionIn,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_FIELD_MANAGE)),
) -> FieldDefinitionOut:
    existing = (
        await ctx.session.execute(
            select(FieldDefinition).where(
                FieldDefinition.tenant_id == ctx.tenant_id,
                FieldDefinition.entity == payload.entity,
                FieldDefinition.field_key == payload.field_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict("That field key already exists for this clinic.")

    row = FieldDefinition(
        scope=FieldScope.TENANT,
        clinical_type=None,
        tenant_id=ctx.tenant_id,  # stamped server-side, never taken from the body
        entity=payload.entity,
        field_key=payload.field_key,
        label=payload.label,
        type=payload.type,
        required=payload.required,
        order=payload.order,
        options=payload.options,
        help=payload.help,
        span=payload.span,
        group=payload.group,
    )
    ctx.session.add(row)
    await ctx.session.flush()
    await audit.record(
        ctx.session, action="field.create", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="field_definition",
        entity_id=str(row.id),
    )
    return FieldDefinitionOut.of(row)


@router.delete("/tenant/fields/{field_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_field(
    field_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_FIELD_MANAGE)),
) -> None:
    """Only a field this clinic added: the shared common and clinical-type rows
    belong to the platform and are not deletable from inside a tenant."""
    row = (
        await ctx.session.execute(
            select(FieldDefinition).where(
                FieldDefinition.id == parse_uuid(field_id, what="field"),
                FieldDefinition.tenant_id == ctx.tenant_id,
                FieldDefinition.scope == FieldScope.TENANT,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("No such field.")
    await ctx.session.delete(row)
    await audit.record(
        ctx.session, action="field.delete", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="field_definition",
        entity_id=field_id,
    )


# --- staff -------------------------------------------------------------------


@router.get("/users", response_model=list[UserOut])
async def list_users(
    ctx: TenantContext = Depends(require_permission(Permission.USER_READ)),
) -> list[UserOut]:
    rows = (
        await ctx.session.execute(
            select(User)
            .where(User.tenant_id == ctx.tenant_id, User.is_support.is_(False))
            .order_by(User.name)
        )
    ).scalars()
    return [UserOut.of(u, ctx.tz) for u in rows]


@router.get("/users/doctors", response_model=list[UserOut])
async def list_doctors(
    ctx: TenantContext = Depends(require_permission(Permission.USER_READ)),
) -> list[UserOut]:
    rows = (
        await ctx.session.execute(
            select(User)
            .where(
                User.tenant_id == ctx.tenant_id,
                User.role == Role.DOCTOR.value,
                User.is_support.is_(False),
            )
            .order_by(User.name)
        )
    ).scalars()
    return [UserOut.of(u, ctx.tz) for u in rows]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateIn,
    ctx: TenantContext = Depends(require_permission(Permission.USER_MANAGE)),
) -> UserOut:
    if payload.role not in {r.value for r in Role}:
        raise Conflict("That is not a role this product has.")

    email = str(payload.email).lower()
    clash = (
        await ctx.session.execute(
            select(func.count(User.id)).where(
                User.tenant_id == ctx.tenant_id, func.lower(User.email) == email
            )
        )
    ).scalar()
    if clash:
        raise Conflict("Someone here already uses that email.")

    user = User(
        tenant_id=ctx.tenant_id,
        name=payload.name,
        role=payload.role,
        email=email,
        # A password given here is a temporary one by definition — the admin
        # typing it knows it, so the account is not really this person's until
        # they have replaced it. `must_change_password` is what makes that
        # true rather than merely advisable.
        password_hash=hash_password(payload.password) if payload.password else None,
        initials="".join(p[0] for p in payload.name.split()[:2]).upper(),
        speciality=payload.speciality,
        status=AccountStatus.ACTIVE if payload.password else AccountStatus.INVITED,
        must_change_password=bool(payload.password),
    )
    ctx.session.add(user)
    await ctx.session.flush()

    # No password means the ordinary path: the account has no hash at all and
    # their own link is on its way. A send that fails is logged and reported,
    # not raised — the row is written and "resend" exists for exactly this.
    invited = False
    if payload.password is None:
        try:
            await invitations.invite_user(
                ctx.session,
                user=user,
                tenant=ctx.tenant,
                issued_by_label=ctx.principal.label,
            )
            invited = True
        except mailer.MailError as exc:
            log.error("could not send invitation to %s: %s", user.email, exc)

    await audit.record(
        ctx.session, action="user.create", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="user", entity_id=str(user.id),
        meta={"role": user.role, "invited": invited},
    )
    return UserOut.of(user, ctx.tz)


@router.post("/users/{user_id}/invite", response_model=UserOut)
async def resend_invitation(
    user_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.USER_MANAGE)),
) -> UserOut:
    """Send this colleague their link again.

    Works whether they never accepted an invitation or have simply forgotten a
    password — the two produce different emails and the route picks the right
    one. Issuing a link spends every earlier one, so the newest email is the
    only one that opens anything.

    A locked account is refused: releasing it is `PATCH /users/{id}/status`,
    and letting a resend quietly reinstate someone an admin shut out would make
    locking meaningless.
    """
    user = await _staff_member(ctx, user_id)
    if user.status == AccountStatus.LOCKED:
        raise Conflict("Unlock the account first.")

    purpose = (
        AuthTokenPurpose.INVITE if user.password_hash is None else AuthTokenPurpose.RESET
    )
    sent = True
    try:
        await invitations.invite_user(
            ctx.session,
            user=user,
            tenant=ctx.tenant,
            purpose=purpose,
            issued_by_label=ctx.principal.label,
        )
    except mailer.MailError as exc:
        sent = False
        log.error("could not send %s to %s: %s", purpose.value, user.email, exc)

    await audit.record(
        ctx.session, action=f"user.{purpose.value}_sent", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="user", entity_id=str(user.id), outcome="ok" if sent else "failed",
        meta={"email": user.email},
    )
    return UserOut.of(user, ctx.tz)


async def _staff_member(ctx: TenantContext, user_id: str) -> User:
    """One colleague by id. A support account is not one of them."""
    user = (
        await ctx.session.execute(
            select(User).where(
                User.id == parse_uuid(user_id, what="account"),
                User.tenant_id == ctx.tenant_id,
                User.is_support.is_(False),
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFound("No such account.")
    return user


@router.patch("/users/{user_id}/status", response_model=UserOut)
async def set_user_status(
    user_id: str,
    payload: UserStatusIn,
    ctx: TenantContext = Depends(require_permission(Permission.USER_MANAGE)),
) -> UserOut:
    """Unlocking clears the failed-attempt counter; locking someone signs them
    out everywhere, immediately."""
    user = await _staff_member(ctx, user_id)
    if user.id == ctx.actor_id and payload.status != AccountStatus.ACTIVE:
        raise Conflict("You cannot lock your own account.")

    user.status = payload.status
    if payload.status == AccountStatus.ACTIVE:
        user.failed_attempts = 0
        user.locked_at = None
    else:
        await auth_service.revoke_all_for_subject(ctx.session, subject_id=user.id)
        # Sessions are not the only way in while a link is outstanding.
        await invitations.revoke_all_for_user(ctx.session, user_id=user.id)

    await ctx.session.flush()
    await audit.record(
        ctx.session, action=f"user.{payload.status.value}", tenant_id=ctx.tenant_id,
        actor_kind=ctx.actor_kind, actor_id=ctx.actor_id, actor_label=ctx.principal.label,
        entity="user", entity_id=str(user.id),
    )
    return UserOut.of(user, ctx.tz)


@router.post("/users/{user_id}/password", response_model=UserOut)
async def set_user_password(
    user_id: str,
    payload: PasswordSetIn,
    ctx: TenantContext = Depends(require_permission(Permission.USER_MANAGE)),
) -> UserOut:
    """Give someone a password they did not choose.

    The ordinary way to complete an invitation is the emailed link, and this
    route is the fallback for when that cannot happen — a colleague whose
    address bounces, someone standing at the desk. It is not the nicer path,
    and the shape of it says so: whoever types the password here knows it, so
    the account is marked `must_change_password` and the person it belongs to
    is held at the change-password screen until they have chosen their own.

    Setting your own password here does not raise that flag, because you chose
    it and nobody else saw it.

    It is deliberately not an unlock. A locked account stays locked — releasing
    it is `PATCH /users/{id}/status`, and letting a password reset quietly
    reinstate someone an admin had shut out would make locking meaningless.
    """
    user = await _staff_member(ctx, user_id)

    accepting_invitation = user.status == AccountStatus.INVITED
    own = user.id == ctx.actor_id

    user.password_hash = hash_password(payload.password)
    user.password_changed_at = datetime.now(timezone.utc)
    user.must_change_password = not own
    # The counter counted failures against a password that no longer exists.
    user.failed_attempts = 0
    if accepting_invitation:
        user.status = AccountStatus.ACTIVE

    # Any link this account is holding is now stale, and a stale link that
    # still works is a second password nobody is tracking.
    await invitations.revoke_all_for_user(ctx.session, user_id=user.id)

    # A new password ends every session opened with the old one. The browser
    # making this request is spared when an admin is setting their own, or they
    # would sign themselves out in the act of doing it.
    await auth_service.revoke_all_for_subject(
        ctx.session,
        subject_id=user.id,
        except_session_id=ctx.principal.session_id if own else None,
    )

    await ctx.session.flush()
    await audit.record(
        ctx.session, action="user.password_set", tenant_id=ctx.tenant_id, actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="user", entity_id=str(user.id),
        meta={
            "self": own,
            "accepted_invitation": accepting_invitation,
            "must_change_password": user.must_change_password,
        },
    )
    return UserOut.of(user, ctx.tz)


@router.get("/users/permissions", response_model=dict)
async def role_matrix(ctx: TenantContext = Depends(current_tenant)) -> dict:
    """What each role may do — the same table the guards consult."""
    return {role.value: permissions_for(role.value) for role in Role}
