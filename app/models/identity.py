"""Who can sign in.

Clinic staff and platform staff are two tables, not one with a nullable column:
a platform account has no tenant and can never appear inside a clinic, and a
clinic account can never be widened into a platform one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin, uuid_pk
from app.models.enums import AccountStatus, AuthTokenPurpose, PlatformRole, SubjectKind


class User(Base, TenantScoped, TimestampMixin):
    """Clinic staff. `email` is unique *within* a tenant — two clinics may both
    have an `admin@` account and they are different people."""

    __tablename__ = "users"
    __extra_table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        CheckConstraint("failed_attempts >= 0", name="ck_users_attempts"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    email: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    initials: Mapped[str] = mapped_column(String(4), nullable=False, default="")
    speciality: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: What a doctor writes after their name on paper — "MBBS, MD (General
    #: Medicine)" — and the number they are registered under. Both optional;
    #: a printed prescription carries them when they are set.
    qualifications: Mapped[str | None] = mapped_column(String(160), nullable=True)
    registration_no: Mapped[str | None] = mapped_column(String(60), nullable=True)
    #: A doctor's own price for a consultation. NULL means "use the clinic's
    #: default" (the ServicePrice row with code CONSULTATION) — never zero,
    #: which would mean "this doctor's consultation is free."
    consultation_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus, name="account_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=AccountStatus.INVITED,
    )
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_sign_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Set on an account that was given a password it did not choose. Sign-in
    #: still succeeds — the session it hands back is deliberately good for one
    #: thing only, changing the password — because refusing the sign-in would
    #: leave the person with no way to get past it.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    #: A platform support account, created the first time someone opens this
    #: clinic from the console. It has no password hash and never gets one, so
    #: it cannot be signed into; it exists so a support session's writes have a
    #: real row behind them. Hidden from the clinic's staff directory.
    is_support: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    #: The newest invitation or reset email: `queued`, `sent` or `failed`
    #: (`LinkEmailStatus`), and when it last changed. NULL when none was ever
    #: asked for. The link itself is minted by the worker, so it never sits in
    #: the broker; this is how a screen knows whether it went.
    link_email_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    link_email_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: why the last send failed — for the logs and the console, not the clinic
    link_email_error: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    #: How this person has set the interface up for themselves — which is not
    #: clinical data and not the clinic's configuration (§3.3), so it lives
    #: here rather than on the tenant. Only keys the API knows are ever stored.
    ui_preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")


class PlatformUser(Base, TimestampMixin):
    __tablename__ = "platform_users"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    initials: Mapped[str] = mapped_column(String(4), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="")

    #: What this account may reach inside the console. A plain string, exactly
    #: as `User.role` is, so adding a third kind of platform account is a row
    #: change rather than a type change. Everything that existed before this
    #: column did was a super admin, which is why that is the default.
    role: Mapped[str] = mapped_column(
        String(40), nullable=False,
        default=PlatformRole.SUPER_ADMIN.value,
        server_default=PlatformRole.SUPER_ADMIN.value,
    )
    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus, name="account_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=AccountStatus.ACTIVE,
    )
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_sign_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuthToken(Base):
    """A one-time link: "set your password" or "reset your password".

    Only the SHA-256 is stored, exactly as for refresh tokens — the plaintext
    lives long enough to be put in an email and nowhere else. Spending one
    stamps `used_at`; a second presentation of the same link is refused rather
    than quietly working twice.

    Deliberately not `TenantScoped`. The row has to be found by digest alone
    before anyone is authenticated, and its policy is the one the auth tables
    already use: your clinic's rows, or the platform's when no clinic is bound.
    """

    __tablename__ = "auth_tokens"
    __table_args__ = (
        Index("ix_auth_tokens_subject", "subject_kind", "subject_id"),
        Index("ix_auth_tokens_expires", "expires_at"),
        CheckConstraint(
            "(subject_kind = 'user' and tenant_id is not null) "
            "or (subject_kind = 'platform' and tenant_id is null)",
            name="ck_auth_token_subject_tenant",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    purpose: Mapped[AuthTokenPurpose] = mapped_column(
        Enum(
            AuthTokenPurpose,
            name="auth_token_purpose",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    subject_kind: Mapped[SubjectKind] = mapped_column(
        Enum(SubjectKind, name="subject_kind", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Who asked for it — platform staff for an invitation, nobody for a
    #: self-service reset.
    issued_by_label: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(300), nullable=False, default="")

    @property
    def spent(self) -> bool:
        return self.used_at is not None


class RefreshToken(Base):
    """Only the SHA-256 of the token is stored, so a database dump cannot be
    replayed as a session. Rotation is enforced: presenting a token that has
    already been exchanged revokes the whole chain (theft detection)."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_subject", "subject_kind", "subject_id"),
        CheckConstraint(
            "(subject_kind = 'user' and tenant_id is not null) "
            "or (subject_kind = 'platform' and tenant_id is null)",
            name="ck_refresh_subject_tenant",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    subject_kind: Mapped[SubjectKind] = mapped_column(
        Enum(SubjectKind, name="subject_kind", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(300), nullable=False, default="")


class LoginAttempt(Base):
    """Recorded for every attempt, successful or not — the raw material for
    lockout, alerting, and answering "who tried to get in"."""

    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_at", "at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    subject_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    email: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    succeeded: Mapped[bool] = mapped_column(nullable=False, default=False)
    reason: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(300), nullable=False, default="")
