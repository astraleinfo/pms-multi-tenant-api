"""The middleware stack of §4, expressed as dependencies and applied in order:

    1. auth              — who is this, from the token alone
    2. tenant isolation   — bind the session to their clinic, and only theirs
    3. feature guard      — is the module they are asking for switched on
    4. RBAC guard         — does their role allow this action
    5. schema resolver    — merge the field definitions for dynamic-form routes

Nothing downstream may re-derive the tenant: `TenantContext.tenant` is the only
tenant a handler can see, and it came from the host and the token agreeing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from app.core.config import settings
from app.core.errors import AuthError, FeatureNotEnabled, NotFound, PermissionDenied
from app.core.permissions import Permission, role_has
from app.core.security import TokenError, decode_access_token
from app.core.tenancy import HostContext, resolve_host
from app.db.session import session_factory, set_tenant
from app.models.enums import AccountStatus, FieldEntity, TenantStatus
from app.models.identity import PlatformUser, User
from app.models.tenancy import Tenant, TenantConfig
from app.schemas.common import zone
from app.services import tenants as tenant_service
from app.services.auth import Principal

bearer = HTTPBearer(auto_error=False)


async def db_session() -> AsyncIterator[AsyncSession]:
    """One transaction per request.

    `app.tenant_id` is transaction-local, so it is impossible for one request's
    tenant to leak into the next request that borrows the same connection.
    """
    async with session_factory() as session:
        async with session.begin():
            yield session


def host_context(connection: HTTPConnection) -> HostContext:
    """Which clinic this request — or socket — arrived at.

    In development the API often sits behind a proxy that rewrites `Host`, so
    an explicit header may stand in — and a browser cannot set headers on a
    WebSocket at all, so a `?tenant=` parameter stands in there. Both are
    accepted only when switched on, and never in production. Neither is an
    authorisation input: the token still carries the tenant, and the two must
    agree before anything runs.
    """
    if settings.allow_tenant_header and not settings.is_production:
        slug = (
            connection.headers.get("x-tenant-slug")
            or connection.query_params.get("tenant")
            or ""
        ).strip().lower()
        if slug:
            return HostContext("tenant", slug, settings.app_domain, "")
        if connection.headers.get("x-platform-console") == "1":
            return HostContext("platform", None, settings.app_domain, "")
    return resolve_host(connection.headers.get("host"))


def client_ip(request: Request) -> str:
    # X-Forwarded-For is only trusted when a proxy is configured to set it;
    # uvicorn's --proxy-headers rewrites request.client for us in that case.
    return request.client.host if request.client else ""


def user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:300]


@dataclass
class TenantContext:
    """Everything a clinic handler is allowed to know about who is asking."""

    principal: Principal
    tenant: Tenant
    config: TenantConfig
    user: User
    session: AsyncSession
    active_features: set[str]

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.id

    @property
    def tz(self) -> str:
        return self.tenant.timezone

    @property
    def today(self) -> date:
        return datetime.now(zone(self.tenant.timezone)).date()

    @property
    def actor_id(self) -> uuid.UUID:
        return self.user.id

    def has_feature(self, key: str) -> bool:
        return key in self.active_features


def _token_from(request: Request, credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials and credentials.scheme.lower() == "bearer" and credentials.credentials:
        return credentials.credentials
    # Same-site cookie fallback, for browsers that prefer not to hold the token
    # in JavaScript at all.
    cookie = request.cookies.get("pms_access")
    if cookie:
        return cookie
    raise AuthError("Sign in to continue.")


async def current_tenant(
    request: Request,
    session: AsyncSession = Depends(db_session),
    host: HostContext = Depends(host_context),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> TenantContext:
    """Steps 1 and 2. Every clinic route depends on this, directly or not."""
    token = _token_from(request, credentials)

    try:
        claims = decode_access_token(token)
    except TokenError as exc:
        raise AuthError("Your session has expired. Sign in again.") from exc

    if claims.subject_kind != "user" or claims.tenant_id is None:
        raise AuthError("That token is not for a clinic.")

    if host.kind != "tenant" or not host.slug:
        raise AuthError("A clinic session can only be used on that clinic's address.")

    # The host and the token must name the *same* clinic. A valid token from
    # one clinic presented at another clinic's address is refused here, before
    # any query runs.
    if claims.tenant_slug and claims.tenant_slug != host.slug:
        raise AuthError("A clinic session can only be used on that clinic's address.")

    tenant = await tenant_service.tenant_by_slug(session, host.slug)
    if tenant is None or tenant.id != claims.tenant_id:
        raise AuthError("A clinic session can only be used on that clinic's address.")
    if tenant.status == TenantStatus.SUSPENDED:
        raise AuthError("This clinic is suspended.")

    # Step 2 — from here the database itself refuses rows from anyone else.
    await set_tenant(session, tenant.id)

    user = await session.get(User, claims.subject_id)
    if user is None or user.tenant_id != tenant.id:
        raise AuthError("Sign in to continue.")
    if user.status != AccountStatus.ACTIVE:
        raise AuthError("This account is not active.")

    principal = Principal(
        kind="user",
        subject_id=user.id,
        session_id=claims.session_id,
        tenant=tenant,
        user=user,
    )
    config = await tenant_service.get_config(session, tenant.id)
    active = await tenant_service.active_feature_keys(session, tenant.id)

    request.state.tenant_id = str(tenant.id)
    request.state.actor_id = str(user.id)

    return TenantContext(
        principal=principal,
        tenant=tenant,
        config=config,
        user=user,
        session=session,
        active_features=active,
    )


async def current_platform_user(
    request: Request,
    session: AsyncSession = Depends(db_session),
    host: HostContext = Depends(host_context),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> PlatformUser:
    """Platform staff, and only on the apex host."""
    token = _token_from(request, credentials)

    try:
        claims = decode_access_token(token)
    except TokenError as exc:
        raise AuthError("Your session has expired. Sign in again.") from exc

    if claims.subject_kind != "platform":
        raise AuthError("That token is not for the platform console.")
    if host.kind != "platform":
        raise AuthError("The console is served on the main domain only.")

    staff = await session.get(PlatformUser, claims.subject_id)
    if staff is None or staff.status != AccountStatus.ACTIVE:
        raise AuthError("Sign in to continue.")

    request.state.actor_id = str(staff.id)
    return staff


def require_permission(permission: Permission):
    """Step 4 — RBAC. Handlers name a permission, never a role."""

    async def guard(ctx: TenantContext = Depends(current_tenant)) -> TenantContext:
        if not role_has(ctx.user.role, permission):
            raise PermissionDenied("Your role does not allow that.")
        return ctx

    return guard


def require_feature(feature_key: str, permission: Permission | None = None):
    """Step 3 — entitlement, then step 4 if a permission is named.

    The front end hides modules it has not bought; this is what actually stops
    a request reaching one.
    """

    async def guard(ctx: TenantContext = Depends(current_tenant)) -> TenantContext:
        if not ctx.has_feature(feature_key):
            raise FeatureNotEnabled(
                "That module is not enabled for this clinic.", details={"feature": feature_key}
            )
        if permission is not None and not role_has(ctx.user.role, permission):
            raise PermissionDenied("Your role does not allow that.")
        return ctx

    return guard


async def resolved_schema(entity: FieldEntity, ctx: TenantContext) -> list:
    """Step 5 — the schema resolver, for routes that render dynamic forms."""
    return await tenant_service.effective_schema(ctx.session, ctx.tenant, entity)


def parse_uuid(value: str, *, what: str = "record") -> uuid.UUID:
    """Ids are opaque to clients; a malformed one is simply not found."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise NotFound(f"No such {what}.") from exc


async def host_tenant(
    session: AsyncSession = Depends(db_session), host: HostContext = Depends(host_context)
) -> Tenant:
    """The clinic this host belongs to — used by the sign-in page, before
    anyone is authenticated."""
    if host.kind != "tenant" or not host.slug:
        raise NotFound("This address does not belong to a clinic.")
    tenant = await tenant_service.tenant_by_slug(session, host.slug)
    if tenant is None:
        raise NotFound("This address does not belong to a clinic.")
    return tenant
