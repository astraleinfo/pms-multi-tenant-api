"""Sign in, refresh, sign out.

The access token is returned in the body (short-lived, held in memory by the
browser). The refresh token only ever exists as an HttpOnly cookie, so script
in the page cannot read it, and it is scoped to the auth path so it is not sent
with ordinary API calls.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
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
from app.core.errors import AuthError, NotFound, ValidationFailed
from app.core.permissions import permissions_for
from app.core.rate_limit import Rate, limiter
from app.core.security import decode_access_token, hash_password, verify_password
from app.core.tenancy import HostContext
from app.db.session import set_tenant
from app.models.enums import AccountStatus, AuthTokenPurpose, SubjectKind
from app.models.identity import PlatformUser, User
from app.models.tenancy import Tenant
from app.schemas.identity import (
    AcceptedOut,
    InvitationOut,
    LoginIn,
    MeOut,
    PasswordChangeIn,
    PasswordForgotIn,
    PasswordResetIn,
    PlatformUserOut,
    PreferencesPatch,
    SessionOut,
    UserOut,
    UserPreferences,
)
from app.services import audit, invitations
from app.services import auth as auth_service
from app.services import tenants as tenant_service

router = APIRouter(tags=["auth"])

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


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


async def _clinic_at(session: AsyncSession, host: HostContext) -> Tenant:
    """The clinic this address belongs to, with the session bound to it.

    Every route below runs before anyone is authenticated, so the host is the
    only thing that says which clinic is being talked about — and binding the
    session here is what stops the queries that follow from seeing any other.
    """
    if host.kind != "tenant" or not host.slug:
        raise NotFound("This address does not belong to a clinic.")
    tenant = await tenant_service.tenant_by_slug(session, host.slug)
    if tenant is None:
        raise NotFound("This address does not belong to a clinic.")
    await set_tenant(session, tenant.id)
    return tenant


def _login_rate_key(request: Request, email: str) -> str:
    return f"login:{client_ip(request)}:{email.strip().lower()}"


@router.post("/auth/login", response_model=SessionOut)
async def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(db_session, scope="function"),
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
        must_change_password=user.must_change_password,
    )


@router.post("/auth/refresh", response_model=SessionOut)
async def refresh_session(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(db_session, scope="function"),
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
            must_change_password=user.must_change_password,
        )

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
    session: AsyncSession = Depends(db_session, scope="function"),
) -> Response:
    """Revokes the whole sign-in chain, not just this token."""
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
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
        platform_user=(
            PlatformUserOut.of(ctx.principal.platform_user) if ctx.impersonated else None
        ),
        tenant_id=str(ctx.tenant_id),
        role=ctx.user.role,
        permissions=permissions_for(ctx.user.role),
        preferences=UserPreferences(**(ctx.user.ui_preferences or {})),
        must_change_password=ctx.user.must_change_password,
        impersonated=ctx.impersonated,
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


# --- setting your own password ------------------------------------------------
#
# Three routes and one idea: nobody is ever handed a password. An account is
# created without one, its owner is emailed a single-use link, and the password
# that account ends up with is one only they have ever seen.
#
# All three are served on a clinic's own address. The tenant comes from the
# host here exactly as it does for sign-in, so a link minted for one clinic is
# inert at another's door.


@router.get("/auth/invitation", response_model=InvitationOut)
async def inspect_invitation(
    token: str,
    session: AsyncSession = Depends(db_session, scope="function"),
    host: HostContext = Depends(host_context),
) -> InvitationOut:
    """What the "choose a password" page shows before anyone is signed in.

    It answers with `valid: false` and a sentence rather than an error status,
    because "this link expired" is a page to render, not a failure to handle.
    """
    tenant = await _clinic_at(session, host)

    try:
        row, user = await invitations.resolve(session, raw_token=token, tenant_id=tenant.id)
    except ValidationFailed as exc:
        return InvitationOut(valid=False, purpose="", problem=exc.message)

    return InvitationOut(
        valid=True,
        purpose=AuthTokenPurpose(row.purpose).value,
        name=user.name,
        email=user.email,
        clinic=tenant.name,
    )


@router.post("/auth/password/forgot", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    payload: PasswordForgotIn,
    request: Request,
    session: AsyncSession = Depends(db_session, scope="function"),
    host: HostContext = Depends(host_context),
) -> dict:
    """Ask for a reset link.

    Always 202, always the same sentence. Whether that address has an account
    here is not something this route will tell you — the difference between
    "sent" and "nothing to send" is visible only in the recipient's inbox.
    """
    limiter.check(f"reset:{client_ip(request)}", Rate.parse(settings.password_reset_rate_limit))

    answer = {
        "status": "accepted",
        "message": "If that address has an account here, a reset link is on its way.",
    }

    tenant = await _clinic_at(session, host)

    normalised = str(payload.email).strip().lower()
    user = (
        await session.execute(
            select(User).where(
                User.tenant_id == tenant.id,
                func.lower(User.email) == normalised,
                User.is_support.is_(False),
            )
        )
    ).scalar_one_or_none()

    if user is None:
        return answer

    # A locked account is not released by asking for a reset link — that is an
    # admin's decision, and letting a stranger undo a lockout by email would
    # make locking meaningless.
    if user.status == AccountStatus.LOCKED:
        return answer

    # An account that has never had a password gets its invitation again rather
    # than a reset, because that is what it is.
    purpose = AuthTokenPurpose.INVITE if user.password_hash is None else AuthTokenPurpose.RESET
    await invitations.invite_user(
        session,
        user=user,
        tenant=tenant,
        purpose=purpose,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )
    await audit.record(
        session, action="auth.reset_requested", tenant_id=tenant.id, actor_kind="user",
        actor_id=user.id, actor_label=f"{user.name} <{user.email}>", entity="user",
        entity_id=str(user.id), ip=client_ip(request), user_agent=user_agent(request),
        meta={"purpose": purpose.value},
    )
    return answer


@router.post("/auth/password/reset", response_model=AcceptedOut)
async def reset_password(
    payload: PasswordResetIn,
    request: Request,
    session: AsyncSession = Depends(db_session, scope="function"),
    host: HostContext = Depends(host_context),
) -> Response:
    """Spend a link and choose a password.

    Doing this ends every session the account had — including any opened with
    the password being replaced, which is the whole point when the reason for
    the reset is that somebody else knew it. No session is handed back: they
    sign in with the password they just chose, which proves it went in as they
    intended.
    """
    limiter.check(f"reset-use:{client_ip(request)}", Rate.parse(settings.password_reset_rate_limit))

    tenant = await _clinic_at(session, host)
    row, user = await invitations.resolve(session, raw_token=payload.token, tenant_id=tenant.id)

    accepting_invitation = user.password_hash is None

    user.password_hash = hash_password(payload.password)
    user.password_changed_at = now_utc()
    # They chose this one themselves, so whatever was forcing a change is done.
    user.must_change_password = False
    # The counter counted failures against a password that no longer exists.
    user.failed_attempts = 0
    if user.status == AccountStatus.INVITED:
        user.status = AccountStatus.ACTIVE

    await invitations.spend(session, row)
    await auth_service.revoke_all_for_subject(session, subject_id=user.id)
    await session.flush()

    await audit.record(
        session,
        action="auth.invitation_accepted" if accepting_invitation else "auth.password_reset",
        tenant_id=tenant.id, actor_kind="user", actor_id=user.id,
        actor_label=f"{user.name} <{user.email}>", entity="user", entity_id=str(user.id),
        ip=client_ip(request), user_agent=user_agent(request),
    )

    # Any half-finished session in this browser is not theirs to keep.
    out = JSONResponse(AcceptedOut(email=user.email).model_dump())
    _clear_refresh_cookie(out)
    return out


@router.post("/auth/password/change", response_model=UserOut)
async def change_own_password(
    payload: PasswordChangeIn,
    request: Request,
    ctx: TenantContext = Depends(current_tenant),
) -> UserOut:
    """Change your own password, having proved you know the current one.

    This is the route that clears `must_change_password`, so it is also the way
    out of a session that was good for one thing only. Every *other* session
    this account holds is revoked; the browser doing it is spared, or they
    would sign themselves out in the act.
    """
    if ctx.impersonated:
        raise ValidationFailed("A support session cannot change a clinic account's password.")

    if not verify_password(payload.current_password, ctx.user.password_hash):
        raise AuthError("That is not your current password.")
    if payload.current_password == payload.new_password:
        raise ValidationFailed("Choose a password you have not been using.")

    ctx.user.password_hash = hash_password(payload.new_password)
    ctx.user.password_changed_at = now_utc()
    ctx.user.must_change_password = False
    ctx.user.failed_attempts = 0

    await auth_service.revoke_all_for_subject(
        ctx.session, subject_id=ctx.user.id, except_session_id=ctx.principal.session_id
    )
    await ctx.session.flush()
    await audit.record(
        ctx.session, action="auth.password_change", tenant_id=ctx.tenant_id, actor_kind="user",
        actor_id=ctx.user.id, actor_label=ctx.principal.label, entity="user",
        entity_id=str(ctx.user.id), ip=client_ip(request), user_agent=user_agent(request),
    )
    return UserOut.of(ctx.user, ctx.tz)


@router.post("/platform/auth/login", response_model=SessionOut, tags=["platform"])
async def platform_login(
    payload: LoginIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(db_session, scope="function"),
    host: HostContext = Depends(host_context),
) -> SessionOut:
    limiter.check(_login_rate_key(request, payload.email), Rate.parse(settings.login_rate_limit))

    if not host.is_admin:
        raise NotFound("The console is served on the admin address only.")

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
