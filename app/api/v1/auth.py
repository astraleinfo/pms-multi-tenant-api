"""Sign in, refresh, sign out.

The access token is returned in the body (short-lived, held in memory by the
browser). The refresh token only ever exists as an HttpOnly cookie, so script
in the page cannot read it, and it is scoped to the auth path so it is not sent
with ordinary API calls.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    TenantContext,
    client_ip,
    current_tenant,
    db_session,
    host_context,
    user_agent,
)
from app.core.config import settings
from app.core.errors import AuthError, NotFound
from app.core.permissions import permissions_for
from app.core.rate_limit import Rate, limiter
from app.core.security import decode_access_token
from app.core.tenancy import HostContext
from app.db.session import set_tenant
from app.models.enums import SubjectKind
from app.schemas.identity import (
    LoginIn,
    MeOut,
    PlatformUserOut,
    PreferencesPatch,
    SessionOut,
    UserOut,
    UserPreferences,
)
from app.services import auth as auth_service
from app.services import tenants as tenant_service

router = APIRouter(tags=["auth"])

REFRESH_COOKIE = "pms_refresh"
REFRESH_PATH = settings.refresh_cookie_path


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=REFRESH_PATH,
        domain=settings.cookie_domain or None,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE, path=REFRESH_PATH, domain=settings.cookie_domain or None
    )


def _login_rate_key(request: Request, email: str) -> str:
    return f"login:{client_ip(request)}:{email.strip().lower()}"


@router.post("/auth/login", response_model=SessionOut)
async def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(db_session),
    host: HostContext = Depends(host_context),
) -> SessionOut:
    """The tenant comes from the address, never from the request body."""
    limiter.check(_login_rate_key(request, payload.email), Rate.parse(settings.login_rate_limit))
    limiter.check(f"login-ip:{client_ip(request)}", Rate.parse(settings.login_rate_limit))

    if host.kind != "tenant" or not host.slug:
        raise NotFound("This address does not belong to a clinic.")

    tenant = await tenant_service.tenant_by_slug(session, host.slug)
    if tenant is None:
        raise NotFound("This address does not belong to a clinic.")

    await set_tenant(session, tenant.id)

    user, access, refresh = await auth_service.authenticate_user(
        session,
        tenant=tenant,
        email=payload.email,
        password=payload.password,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )

    _set_refresh_cookie(response, refresh)
    return SessionOut(
        access_token=access,
        expires_in=settings.access_token_ttl_seconds,
        user=UserOut.of(user, tenant.timezone),
        tenant_id=str(tenant.id),
        permissions=permissions_for(user.role),
        preferences=UserPreferences(**(user.ui_preferences or {})),
    )


@router.post("/auth/refresh", response_model=SessionOut)
async def refresh_session(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(db_session),
    host: HostContext = Depends(host_context),
) -> SessionOut:
    """Rotates the refresh token. Re-using a spent one kills the whole chain."""
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise AuthError("Sign in to continue.")

    tenant = None
    if host.kind == "tenant" and host.slug:
        tenant = await tenant_service.tenant_by_slug(session, host.slug)
        if tenant is None:
            raise NotFound("This address does not belong to a clinic.")
        await set_tenant(session, tenant.id)

    row, new_raw = await auth_service.rotate_refresh_token(
        session,
        raw_token=raw,
        expected_tenant_id=tenant.id if tenant else None,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )
    _set_refresh_cookie(response, new_raw)

    if row.subject_kind == SubjectKind.USER:
        from app.models.identity import User

        user = await session.get(User, row.subject_id)
        if user is None or user.status != "active" or tenant is None:
            raise AuthError("Sign in to continue.")
        access = auth_service.create_access_token(
            subject_id=user.id,
            subject_kind="user",
            session_id=row.session_id,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            role=user.role,
        )
        return SessionOut(
            access_token=access,
            expires_in=settings.access_token_ttl_seconds,
            user=UserOut.of(user, tenant.timezone),
            tenant_id=str(tenant.id),
            permissions=permissions_for(user.role),
            preferences=UserPreferences(**(user.ui_preferences or {})),
        )

    from app.models.identity import PlatformUser

    staff = await session.get(PlatformUser, row.subject_id)
    if staff is None or staff.status != "active":
        raise AuthError("Sign in to continue.")
    access = auth_service.create_access_token(
        subject_id=staff.id, subject_kind="platform", session_id=row.session_id
    )
    return SessionOut(
        access_token=access,
        expires_in=settings.access_token_ttl_seconds,
        platform_user=PlatformUserOut.of(staff),
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(db_session),
) -> Response:
    """Revokes the whole sign-in chain, not just this token."""
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        from sqlalchemy import select

        from app.core.security import hash_refresh_token
        from app.models.identity import RefreshToken

        row = (
            await session.execute(
                select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw))
            )
        ).scalar_one_or_none()
        if row is not None:
            await auth_service.revoke_session(session, session_id=row.session_id)

    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        try:
            claims = decode_access_token(authorization.split(" ", 1)[1])
            await auth_service.revoke_session(session, session_id=claims.session_id)
        except Exception as exc:  # noqa: BLE001 - a bad token simply has nothing to revoke
            logging.getLogger("pms.auth").debug("logout with an unusable token: %s", exc)

    out = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(out)
    return out


@router.get("/auth/me", response_model=MeOut)
async def me(ctx: TenantContext = Depends(current_tenant)) -> MeOut:
    return MeOut(
        user=UserOut.of(ctx.user, ctx.tz),
        tenant_id=str(ctx.tenant_id),
        role=ctx.user.role,
        permissions=permissions_for(ctx.user.role),
        features=sorted(ctx.active_features),
        preferences=UserPreferences(**(ctx.user.ui_preferences or {})),
    )


@router.patch("/auth/me/preferences", response_model=UserPreferences)
async def update_preferences(
    payload: PreferencesPatch, ctx: TenantContext = Depends(current_tenant)
) -> UserPreferences:
    """How this person has set the interface up for themselves.

    Their own row and nobody else's: there is no user id in this route, because
    the only account it can touch is the one that made the request. Unknown keys
    are dropped by the schema, so this cannot grow into arbitrary storage
    hanging off a staff record.
    """
    changes = payload.model_dump(exclude_unset=True)
    merged = {**(ctx.user.ui_preferences or {}), **changes}
    # An explicit null clears a preference rather than storing one, which is how
    # "show me the tour again" is expressed.
    merged = {key: value for key, value in merged.items() if value is not None}

    preferences = UserPreferences(**merged)
    # Store the validated shape, so the column only ever holds known keys.
    ctx.user.ui_preferences = preferences.model_dump()
    await ctx.session.flush()
    return preferences


@router.post("/platform/auth/login", response_model=SessionOut, tags=["platform"])
async def platform_login(
    payload: LoginIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(db_session),
    host: HostContext = Depends(host_context),
) -> SessionOut:
    limiter.check(_login_rate_key(request, payload.email), Rate.parse(settings.login_rate_limit))

    if host.kind != "platform":
        raise NotFound("The console is served on the main domain only.")

    staff, access, refresh = await auth_service.authenticate_platform_user(
        session,
        email=payload.email,
        password=payload.password,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )
    _set_refresh_cookie(response, refresh)
    return SessionOut(
        access_token=access,
        expires_in=settings.access_token_ttl_seconds,
        platform_user=PlatformUserOut.of(staff),
    )
