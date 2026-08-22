"""Password hashing and token minting.

Access tokens are short-lived JWTs. Refresh tokens are opaque random strings —
only their hash is stored, so a database read cannot be replayed as a session.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import settings

_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)

# A real argon2 hash of a random string. Verifying against it burns the same CPU
# as a real check, so "no such user" and "wrong password" take the same time.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))

TokenKind = Literal["access", "refresh"]
SubjectKind = Literal["user", "platform"]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-ish time. A missing hash still pays for a verification."""
    try:
        _hasher.verify(password_hash or _DUMMY_HASH, password)
        return password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def new_refresh_token() -> tuple[str, str]:
    """Return (plaintext, sha256) — only the digest is ever persisted."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def refresh_tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


@dataclass(frozen=True)
class AccessClaims:
    subject_id: uuid.UUID
    subject_kind: SubjectKind
    tenant_id: uuid.UUID | None
    role: str | None
    session_id: uuid.UUID
    tenant_slug: str | None
    expires_at: datetime


def create_access_token(
    *,
    subject_id: uuid.UUID,
    subject_kind: SubjectKind,
    session_id: uuid.UUID,
    tenant_id: uuid.UUID | None = None,
    tenant_slug: str | None = None,
    role: str | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject_id),
        "kind": subject_kind,
        "sid": str(session_id),
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.access_token_ttl_seconds)).timestamp()),
        "jti": secrets.token_urlsafe(12),
        "iss": "pms-api",
        "aud": "pms-web",
    }
    if tenant_id is not None:
        payload["tid"] = str(tenant_id)
        payload["slug"] = tenant_slug
        payload["role"] = role
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


class TokenError(Exception):
    """Malformed, expired, or otherwise unusable token."""


def decode_access_token(token: str) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience="pms-web",
            issuer="pms-api",
            options={"require": ["exp", "iat", "sub", "kind", "sid"]},
        )
    except jwt.PyJWTError as exc:  # expired, bad signature, wrong audience…
        raise TokenError(str(exc)) from exc

    kind = payload.get("kind")
    if kind not in ("user", "platform"):
        raise TokenError("unknown subject kind")

    try:
        subject_id = uuid.UUID(payload["sub"])
        session_id = uuid.UUID(payload["sid"])
        tenant_id = uuid.UUID(payload["tid"]) if payload.get("tid") else None
    except (ValueError, KeyError, TypeError) as exc:
        raise TokenError("malformed identifiers") from exc

    if kind == "user" and tenant_id is None:
        raise TokenError("a clinic token must carry a tenant")
    if kind == "platform" and tenant_id is not None:
        raise TokenError("a platform token must not carry a tenant")

    return AccessClaims(
        subject_id=subject_id,
        subject_kind=kind,
        tenant_id=tenant_id,
        role=payload.get("role"),
        session_id=session_id,
        tenant_slug=payload.get("slug"),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
    )
