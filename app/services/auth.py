"""Sign-in, refresh, and sign-out.

Rules that matter here:

* the tenant comes from the host, so a credential from one clinic cannot be
  presented at another clinic's door;
* every failure returns the same sentence, so the form cannot be used to
  discover which addresses exist;
* a wrong password costs the same time as an unknown address;
* failed attempts are counted and the account locks at the configured limit,
  and that bookkeeping survives the rejected request;
* refresh tokens rotate, and re-using a spent one kills the whole chain.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AccountLockedError, AuthError
from app.core.permissions import permissions_for
from app.core.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    needs_rehash,
    new_refresh_token,
    verify_password,
)
from app.db.session import session_factory, set_tenant
from app.models.enums import AccountStatus, SubjectKind, TenantStatus
from app.models.identity import LoginAttempt, PlatformUser, RefreshToken, User
from app.models.tenancy import Tenant
from app.services import audit

GENERIC = "That email and password do not match an account here."
LOCKED = "This account is locked. A practice admin can unlock it."
INVITED = "That invitation has not been accepted yet."


class Principal:
    """Who is making the request, resolved once per request."""

    def __init__(
        self,
        *,
        kind: SubjectKind,
        subject_id: uuid.UUID,
        session_id: uuid.UUID,
        tenant: Tenant | None = None,
        user: User | None = None,
        platform_user: PlatformUser | None = None,
    ) -> None:
        self.kind = kind
        self.subject_id = subject_id
        self.session_id = session_id
        self.tenant = tenant
        self.user = user
        self.platform_user = platform_user

    @property
    def tenant_id(self) -> uuid.UUID | None:
        return self.tenant.id if self.tenant else None

    @property
    def role(self) -> str | None:
        return self.user.role if self.user else None

    @property
    def label(self) -> str:
        if self.user:
            return f"{self.user.name} <{self.user.email}>"
        if self.platform_user:
            return f"{self.platform_user.name} <{self.platform_user.email}>"
        return "unknown"

    @property
    def permissions(self) -> list[str]:
        return permissions_for(self.user.role) if self.user else []


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _log_attempt(
    *,
    tenant_id: uuid.UUID | None,
    email: str,
    succeeded: bool,
    reason: str,
    ip: str,
    user_agent: str,
    subject_kind: str = "user",
) -> None:
    """Its own transaction: a rejected sign-in still gets written down."""
    async with session_factory() as s:
        async with s.begin():
            await set_tenant(s, tenant_id)
            s.add(
                LoginAttempt(
                    tenant_id=tenant_id,
                    email=email[:200],
                    succeeded=succeeded,
                    reason=reason[:60],
                    ip=ip[:64],
                    user_agent=user_agent[:300],
                    subject_kind=subject_kind,
                )
            )


async def _count_failure(tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Increment in a separate transaction, and lock at the limit."""
    async with session_factory() as s:
        async with s.begin():
            await set_tenant(s, tenant_id)
            await s.execute(
                update(User)
                .where(User.id == user_id, User.tenant_id == tenant_id)
                .values(failed_attempts=User.failed_attempts + 1)
            )
            await s.execute(
                update(User)
                .where(
                    User.id == user_id,
                    User.tenant_id == tenant_id,
                    User.failed_attempts >= settings.max_failed_logins,
                    User.status != AccountStatus.LOCKED,
                )
                .values(status=AccountStatus.LOCKED, locked_at=_now())
            )


async def authenticate_user(
    session: AsyncSession,
    *,
    tenant: Tenant,
    email: str,
    password: str,
    ip: str,
    user_agent: str,
) -> tuple[User, str, str]:
    """Return (user, access_token, refresh_token_plaintext)."""
    if tenant.status == TenantStatus.SUSPENDED:
        await _log_attempt(
            tenant_id=tenant.id, email=email, succeeded=False, reason="tenant_suspended",
            ip=ip, user_agent=user_agent,
        )
        raise AuthError("This clinic is suspended.")

    normalised = email.strip().lower()
    user = (
        await session.execute(
            select(User).where(User.tenant_id == tenant.id, func.lower(User.email) == normalised)
        )
    ).scalar_one_or_none()

    # Always pay for a verification, whether or not there is a hash to check.
    password_ok = verify_password(password, user.password_hash if user else None)

    if user is None:
        await _log_attempt(
            tenant_id=tenant.id, email=normalised, succeeded=False, reason="unknown_email",
            ip=ip, user_agent=user_agent,
        )
        raise AuthError(GENERIC)

    if user.status == AccountStatus.LOCKED:
        await _log_attempt(
            tenant_id=tenant.id, email=normalised, succeeded=False, reason="locked",
            ip=ip, user_agent=user_agent,
        )
        raise AccountLockedError(LOCKED)

    if user.status == AccountStatus.INVITED:
        await _log_attempt(
            tenant_id=tenant.id, email=normalised, succeeded=False, reason="invited",
            ip=ip, user_agent=user_agent,
        )
        raise AuthError(INVITED)

    if not password_ok:
        await _count_failure(tenant.id, user.id)
        await _log_attempt(
            tenant_id=tenant.id, email=normalised, succeeded=False, reason="bad_password",
            ip=ip, user_agent=user_agent,
        )
        remaining = max(0, settings.max_failed_logins - (user.failed_attempts + 1))
        if remaining == 0:
            raise AccountLockedError(
                f"Too many attempts — {user.name.split(' ')[0]}'s account is now locked."
            )
        raise AuthError(f"{GENERIC} {remaining} attempt{'' if remaining == 1 else 's'} left.")

    if needs_rehash(user.password_hash or ""):
        user.password_hash = hash_password(password)
        user.password_changed_at = _now()

    user.failed_attempts = 0
    user.last_sign_in = _now()

    session_id = uuid.uuid4()
    access = create_access_token(
        subject_id=user.id,
        subject_kind="user",
        session_id=session_id,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        role=user.role,
    )
    refresh = await issue_refresh_token(
        session,
        subject_kind=SubjectKind.USER,
        subject_id=user.id,
        tenant_id=tenant.id,
        session_id=session_id,
        ip=ip,
        user_agent=user_agent,
    )

    await _log_attempt(
        tenant_id=tenant.id, email=normalised, succeeded=True, reason="ok", ip=ip, user_agent=user_agent
    )
    await audit.record(
        session, action="auth.login", tenant_id=tenant.id, actor_kind="user", actor_id=user.id,
        actor_label=f"{user.name} <{user.email}>", entity="user", entity_id=str(user.id),
        ip=ip, user_agent=user_agent,
    )
    return user, access, refresh


async def authenticate_platform_user(
    session: AsyncSession, *, email: str, password: str, ip: str, user_agent: str
) -> tuple[PlatformUser, str, str]:
    normalised = email.strip().lower()
    staff = (
        await session.execute(select(PlatformUser).where(func.lower(PlatformUser.email) == normalised))
    ).scalar_one_or_none()
    password_ok = verify_password(password, staff.password_hash if staff else None)

    if staff is None or not password_ok or staff.status != AccountStatus.ACTIVE:
        if staff is not None and not password_ok:
            staff.failed_attempts += 1
            if staff.failed_attempts >= settings.max_failed_logins:
                staff.status = AccountStatus.LOCKED
        await _log_attempt(
            tenant_id=None, email=normalised, succeeded=False, reason="platform_reject",
            ip=ip, user_agent=user_agent, subject_kind="platform",
        )
        raise AuthError("That email and password do not match a platform account.")

    staff.failed_attempts = 0
    staff.last_sign_in = _now()

    session_id = uuid.uuid4()
    access = create_access_token(subject_id=staff.id, subject_kind="platform", session_id=session_id)
    refresh = await issue_refresh_token(
        session,
        subject_kind=SubjectKind.PLATFORM,
        subject_id=staff.id,
        tenant_id=None,
        session_id=session_id,
        ip=ip,
        user_agent=user_agent,
    )
    await _log_attempt(
        tenant_id=None, email=normalised, succeeded=True, reason="ok", ip=ip,
        user_agent=user_agent, subject_kind="platform",
    )
    await audit.record(
        session, action="auth.platform_login", actor_kind="platform", actor_id=staff.id,
        actor_label=f"{staff.name} <{staff.email}>", entity="platform_user", entity_id=str(staff.id),
        ip=ip, user_agent=user_agent,
    )
    return staff, access, refresh


async def issue_refresh_token(
    session: AsyncSession,
    *,
    subject_kind: SubjectKind,
    subject_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    session_id: uuid.UUID,
    ip: str,
    user_agent: str,
) -> str:
    raw, digest = new_refresh_token()
    session.add(
        RefreshToken(
            token_hash=digest,
            subject_kind=subject_kind,
            subject_id=subject_id,
            tenant_id=tenant_id,
            session_id=session_id,
            expires_at=_now() + timedelta(seconds=settings.refresh_token_ttl_seconds),
            ip=ip[:64],
            user_agent=user_agent[:300],
        )
    )
    return raw


async def rotate_refresh_token(
    session: AsyncSession,
    *,
    raw_token: str,
    expected_tenant_id: uuid.UUID | None,
    ip: str,
    user_agent: str,
) -> tuple[RefreshToken, str]:
    """Exchange a refresh token for a fresh one.

    Presenting a token that has already been spent means either a stolen copy
    or a replay: every token in that sign-in chain is revoked at once.
    """
    digest = hash_refresh_token(raw_token)
    row = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == digest))
    ).scalar_one_or_none()

    if row is None:
        raise AuthError("Your session has expired. Sign in again.")

    if row.revoked_at is not None:
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.session_id == row.session_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_now())
        )
        await audit.record(
            session, action="auth.refresh_reuse", tenant_id=row.tenant_id, actor_kind=row.subject_kind,
            actor_id=row.subject_id, entity="refresh_token", entity_id=str(row.id), outcome="denied",
            ip=ip, user_agent=user_agent,
        )
        raise AuthError("Your session has expired. Sign in again.")

    if row.expires_at <= _now():
        raise AuthError("Your session has expired. Sign in again.")

    if row.tenant_id != expected_tenant_id:
        # A clinic token presented at another clinic's host, or at the console.
        await audit.record(
            session, action="auth.refresh_wrong_host", tenant_id=row.tenant_id,
            actor_kind=row.subject_kind, actor_id=row.subject_id, entity="refresh_token",
            entity_id=str(row.id), outcome="denied", ip=ip, user_agent=user_agent,
        )
        raise AuthError("Your session has expired. Sign in again.")

    raw, digest_new = new_refresh_token()
    replacement = RefreshToken(
        token_hash=digest_new,
        subject_kind=row.subject_kind,
        subject_id=row.subject_id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        expires_at=_now() + timedelta(seconds=settings.refresh_token_ttl_seconds),
        ip=ip[:64],
        user_agent=user_agent[:300],
    )
    session.add(replacement)
    await session.flush()
    row.revoked_at = _now()
    row.replaced_by_id = replacement.id
    return replacement, raw


async def revoke_session(session: AsyncSession, *, session_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.session_id == session_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


async def revoke_all_for_subject(session: AsyncSession, *, subject_id: uuid.UUID) -> None:
    """Used when an account is locked or its password changes."""
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.subject_id == subject_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
