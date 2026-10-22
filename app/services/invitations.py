"""One-time links: "set your password" and "reset your password".

Nobody is ever handed a password. Creating a clinic mints an invitation for its
first administrator and emails a link; the person who receives it chooses their
own password and only then has a usable account. A forgotten password takes the
same road.

Three rules hold the whole thing up:

*   **the plaintext exists once.** It goes into an email and is never written
    down — the row holds a SHA-256, so a database dump is not a set of working
    links.
*   **a link is spent when it is used.** `used_at` is stamped inside the same
    transaction that changes the password, so a second presentation of the same
    URL is refused rather than quietly working twice.
*   **issuing one invalidates the others.** Asking again does not leave the
    previous email live; the newest link is the only one that works.

The URL is built from configuration, never from the request's `Host`. A forged
header must not be able to decide where an invitation points.

The email is the Celery worker's job, and so is the link. A request only
`queue`s one — it marks the account `queued` and hands the worker who to write
to — and `deliver` mints the link and sends it in the worker. Minting it there
is what keeps rule one: the plaintext is never a task argument, so it never
sits in the broker or shows up in the job monitor. Nothing re-sends on its own:
a send that fails, or a hand-off the broker never got, shows as `retryable`
and is sent again with the Resend button.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.errors import ValidationFailed
from app.core.security import hash_refresh_token, new_auth_token
from app.db.session import set_tenant
from app.models.enums import AccountStatus, AuthTokenPurpose, LinkEmailStatus, SubjectKind
from app.models.identity import AuthToken, User
from app.models.tenancy import Tenant
from app.services import audit, mailer
from app.services.platform import Timings, get_timings

log = logging.getLogger("pms.invitations")

EXPIRED = "That link has expired. Ask for a new one."
SPENT = "That link has already been used."
UNUSABLE = "That link is not valid. Ask for a new one."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ttl(purpose: AuthTokenPurpose, timings: Timings) -> int:
    return (
        timings.invite_token_ttl_seconds
        if purpose == AuthTokenPurpose.INVITE
        else timings.reset_token_ttl_seconds
    )


def link_for(*, slug: str, purpose: AuthTokenPurpose, raw_token: str) -> str:
    """Where the recipient lands. Their clinic's own address, always."""
    page = "accept-invitation" if purpose == AuthTokenPurpose.INVITE else "reset-password"
    return f"{settings.tenant_origin(slug)}/{page}?token={quote(raw_token, safe='')}"


async def issue_for_user(
    session: AsyncSession,
    *,
    user: User,
    tenant: Tenant,
    purpose: AuthTokenPurpose,
    timings: Timings,
    issued_by_label: str = "",
    ip: str = "",
    user_agent: str = "",
) -> tuple[AuthToken, str]:
    """Mint a link for a clinic account. Returns (row, plaintext token).

    Every link this account already holds for the same purpose is spent first,
    so an old email in an inbox stops working the moment a new one is sent.
    """
    await session.execute(
        update(AuthToken)
        .where(
            AuthToken.subject_id == user.id,
            AuthToken.purpose == purpose,
            AuthToken.used_at.is_(None),
        )
        .values(used_at=_now())
    )

    raw, digest = new_auth_token()
    row = AuthToken(
        token_hash=digest,
        purpose=purpose,
        subject_kind=SubjectKind.USER,
        subject_id=user.id,
        tenant_id=tenant.id,
        expires_at=_now() + timedelta(seconds=_ttl(purpose, timings)),
        issued_by_label=issued_by_label[:160],
        ip=ip[:64],
        user_agent=user_agent[:300],
    )
    session.add(row)
    await session.flush()
    return row, raw


async def send_link(
    *, user: User, tenant: Tenant, purpose: AuthTokenPurpose, raw_token: str, timings: Timings
) -> None:
    """Email the link. Raises `mailer.MailError` when it does not leave."""
    url = link_for(slug=tenant.slug, purpose=purpose, raw_token=raw_token)
    if purpose == AuthTokenPurpose.INVITE:
        email = mailer.invitation(
            to=user.email,
            name=user.name,
            clinic=tenant.name,
            url=url,
            expires_in_days=max(1, timings.invite_token_ttl_seconds // 86400),
        )
    else:
        email = mailer.password_reset(
            to=user.email,
            name=user.name,
            clinic=tenant.name,
            url=url,
            expires_in_hours=max(1, timings.reset_token_ttl_seconds // 3600),
        )
    await mailer.send(email)


# --- the request's half -------------------------------------------------------------

#: How long an email may sit `queued` after its hand-off before it counts as
#: lost. Longer than the enqueue's countdown, so it never races a hand-off that
#: is still on its way.
STUCK_AFTER = timedelta(seconds=30)


def purpose_for(user: User) -> AuthTokenPurpose:
    """An account that has never had a password is invited; any other is reset."""
    return AuthTokenPurpose.INVITE if user.password_hash is None else AuthTokenPurpose.RESET


async def queue(session: AsyncSession, user: User) -> None:
    """Mark the account's link email as on its way. The caller hands it to the
    worker (`app.worker.tasks.enqueue_link_email`) once this transaction has
    committed, so the worker finds the row."""
    user.link_email_status = LinkEmailStatus.QUEUED
    user.link_email_at = _now()
    user.link_email_error = ""
    await session.flush()


def retryable(user: User) -> bool:
    """Failed, or queued long enough that its hand-off must have been lost."""
    if user.link_email_status == LinkEmailStatus.FAILED:
        return True
    return (
        user.link_email_status == LinkEmailStatus.QUEUED
        and user.link_email_at is not None
        and user.link_email_at <= _now() - STUCK_AFTER
    )


# --- the worker's half --------------------------------------------------------------


async def deliver(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    purpose: AuthTokenPurpose,
    *,
    issued_by_label: str = "",
    ip: str = "",
    user_agent: str = "",
) -> str:
    """Mint this account's link and email it, once.

    Returns `sent`, `failed`, `skipped` (nothing queued any more, or the account
    no longer wants this link) or `missing` (not visible yet — the request that
    queued it has not committed, or another worker holds the row).

    The row is locked for the length of the send, and only a `queued` one is
    sent, so a redelivered job — or two Resend clicks — cannot email twice for
    one hand-off. Minting spends every earlier link, as it always has.
    """
    async with factory() as session:
        async with session.begin():
            await set_tenant(session, tenant_id)
            user = (
                await session.execute(
                    select(User)
                    .where(User.id == user_id, User.tenant_id == tenant_id)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if user is None:
                return "missing"
            if user.link_email_status != LinkEmailStatus.QUEUED:
                return "skipped"
            # Things may have moved since the request: an invitation accepted
            # in the meantime is not sent, and a locked account gets nothing.
            if (
                user.is_support
                or user.status == AccountStatus.LOCKED
                or (purpose == AuthTokenPurpose.INVITE and user.password_hash is not None)
            ):
                user.link_email_status = None
                user.link_email_at = _now()
                return "skipped"

            tenant = await session.get(Tenant, tenant_id)
            timings = await get_timings(session)
            _, raw = await issue_for_user(
                session,
                user=user,
                tenant=tenant,
                purpose=purpose,
                timings=timings,
                issued_by_label=issued_by_label,
                ip=ip,
                user_agent=user_agent,
            )
            outcome = "sent"
            try:
                await send_link(user=user, tenant=tenant, purpose=purpose, raw_token=raw, timings=timings)
                user.link_email_status = LinkEmailStatus.SENT
                user.link_email_error = ""
            except mailer.MailError as exc:
                outcome = "failed"
                user.link_email_status = LinkEmailStatus.FAILED
                user.link_email_error = str(exc)[:2000]
                log.error("could not send %s to %s: %s", purpose.value, user.email, exc)
            user.link_email_at = _now()

            meta = {"email": user.email}
            if outcome == "failed":
                meta["error"] = user.link_email_error[:300]
            await audit.record(
                session, action=f"auth.{purpose.value}_email", tenant_id=tenant_id,
                entity="user", entity_id=str(user.id), outcome="ok" if outcome == "sent" else "failed",
                meta=meta,
            )
            return outcome


async def resolve(
    session: AsyncSession, *, raw_token: str, tenant_id: uuid.UUID
) -> tuple[AuthToken, User]:
    """Find the account a link belongs to, or say why it cannot be used.

    The clinic is passed in from the host, not read from the row: a link minted
    for one clinic presented at another clinic's address resolves to nothing.
    """
    row = (
        await session.execute(
            select(AuthToken).where(
                AuthToken.token_hash == hash_refresh_token(raw_token),
                AuthToken.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        raise ValidationFailed(UNUSABLE)
    if row.used_at is not None:
        raise ValidationFailed(SPENT)
    if row.expires_at <= _now():
        raise ValidationFailed(EXPIRED)

    user = (
        await session.execute(
            select(User).where(User.id == row.subject_id, User.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if user is None or user.is_support:
        raise ValidationFailed(UNUSABLE)

    return row, user


async def revoke_all_for_user(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    """Spend every link this account is holding, without using any of them.

    Called wherever the account's password stops being the one the links were
    minted against — an admin setting a new one, a lockout. A link that still
    worked after that would be a second password nobody was tracking.
    """
    await session.execute(
        update(AuthToken)
        .where(AuthToken.subject_id == user_id, AuthToken.used_at.is_(None))
        .values(used_at=_now())
    )


async def spend(session: AsyncSession, row: AuthToken) -> None:
    """Mark a link used. Called in the same transaction as the password write,
    so the two cannot come apart."""
    row.used_at = _now()
    await session.flush()
