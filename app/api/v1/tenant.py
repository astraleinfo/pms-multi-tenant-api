"""The clinic itself: its record, its entitlements, its configuration, its
dynamic-form schema, and its staff."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select

from app.api.deps import TenantContext, current_tenant, parse_uuid, require_permission
from app.core.errors import Conflict, NotFound
from app.core.permissions import Permission, Role, permissions_for
from app.core.security import hash_password
from app.models.enums import AccountStatus, FieldEntity, FieldScope
from app.models.identity import User
from app.models.tenancy import Feature, FieldDefinition
from app.schemas.identity import UserCreateIn, UserOut, UserStatusIn
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
from app.services import audit
from app.services import auth as auth_service
from app.services import tenants as tenant_service

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
        ctx.session, action="feature.purchase", tenant_id=ctx.tenant_id, actor_kind="user",
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
        ctx.session, action="feature.deactivate", tenant_id=ctx.tenant_id, actor_kind="user",
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="feature", entity_id=feature_key,
    )
    return TenantFeatureOut.of(row)


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
        ctx.session, action="config.update", tenant_id=ctx.tenant_id, actor_kind="user",
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
        ctx.session, action="field.create", tenant_id=ctx.tenant_id, actor_kind="user",
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
        ctx.session, action="field.delete", tenant_id=ctx.tenant_id, actor_kind="user",
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
            select(User).where(User.tenant_id == ctx.tenant_id).order_by(User.name)
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
            .where(User.tenant_id == ctx.tenant_id, User.role == Role.DOCTOR.value)
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
        password_hash=hash_password(payload.password) if payload.password else None,
        initials="".join(p[0] for p in payload.name.split()[:2]).upper(),
        speciality=payload.speciality,
        status=AccountStatus.ACTIVE if payload.password else AccountStatus.INVITED,
    )
    ctx.session.add(user)
    await ctx.session.flush()
    await audit.record(
        ctx.session, action="user.create", tenant_id=ctx.tenant_id, actor_kind="user",
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="user", entity_id=str(user.id),
        meta={"role": user.role},
    )
    return UserOut.of(user, ctx.tz)


@router.patch("/users/{user_id}/status", response_model=UserOut)
async def set_user_status(
    user_id: str,
    payload: UserStatusIn,
    ctx: TenantContext = Depends(require_permission(Permission.USER_MANAGE)),
) -> UserOut:
    """Unlocking clears the failed-attempt counter; locking someone signs them
    out everywhere, immediately."""
    user = (
        await ctx.session.execute(
            select(User).where(
                User.id == parse_uuid(user_id, what="account"), User.tenant_id == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFound("No such account.")
    if user.id == ctx.actor_id and payload.status != AccountStatus.ACTIVE:
        raise Conflict("You cannot lock your own account.")

    user.status = payload.status
    if payload.status == AccountStatus.ACTIVE:
        user.failed_attempts = 0
        user.locked_at = None
    else:
        await auth_service.revoke_all_for_subject(ctx.session, subject_id=user.id)

    await ctx.session.flush()
    await audit.record(
        ctx.session, action=f"user.{payload.status.value}", tenant_id=ctx.tenant_id, actor_kind="user",
        actor_id=ctx.actor_id, actor_label=ctx.principal.label, entity="user", entity_id=str(user.id),
    )
    return UserOut.of(user, ctx.tz)


@router.get("/users/permissions", response_model=dict)
async def role_matrix(ctx: TenantContext = Depends(current_tenant)) -> dict:
    """What each role may do — the same table the guards consult."""
    return {role.value: permissions_for(role.value) for role in Role}
