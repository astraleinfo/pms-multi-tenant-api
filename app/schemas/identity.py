"""Authentication payloads and the staff directory.

No password, and no password hash, ever leaves this API — the web app's demo
`password` field simply has no counterpart here.
"""

from __future__ import annotations

from pydantic import EmailStr, Field

from app.models.enums import AccountStatus
from app.schemas.common import ApiModel, InputModel, day


class UserPreferences(ApiModel):
    """Per-person interface state.

    A closed set of known keys: whatever a client sends that is not listed here
    is dropped, so this cannot become an unbounded blob attached to a staff
    record.
    """

    #: The tour version this person has finished. Raising `TOUR_VERSION` in the
    #: web app shows the tour again — to everyone, once — without a migration.
    tour_version_seen: int | None = None
    #: Their choice to skip it, kept apart from having finished it.
    tour_dismissed: bool = False


class PreferencesPatch(InputModel):
    tour_version_seen: int | None = Field(default=None, ge=0, le=1000)
    tour_dismissed: bool | None = None


class LoginIn(InputModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class UserOut(ApiModel):
    id: str
    tenant_id: str
    name: str
    role: str
    email: str
    initials: str
    speciality: str | None = None
    status: AccountStatus
    failed_attempts: int
    last_sign_in: str | None

    @classmethod
    def of(cls, u, tz: str | None = None) -> UserOut:
        return cls(
            id=str(u.id), tenant_id=str(u.tenant_id), name=u.name, role=u.role, email=u.email,
            initials=u.initials, speciality=u.speciality, status=u.status,
            failed_attempts=u.failed_attempts, last_sign_in=day(u.last_sign_in, tz),
        )


class PlatformUserOut(ApiModel):
    id: str
    name: str
    email: str
    initials: str
    title: str

    @classmethod
    def of(cls, u) -> PlatformUserOut:
        return cls(id=str(u.id), name=u.name, email=u.email, initials=u.initials, title=u.title)


class SessionOut(ApiModel):
    """What the browser gets on a successful sign-in.

    The refresh token is *not* here: it is set as an HttpOnly cookie, so script
    running in the page cannot read it.
    """

    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - a scheme name, not a secret
    expires_in: int
    user: UserOut | None = None
    platform_user: PlatformUserOut | None = None
    tenant_id: str | None = None
    permissions: list[str] = []
    #: Sent with the session so the interface knows on the first paint whether
    #: this person has seen the tour — rather than flashing it and taking it
    #: away again a moment later.
    preferences: UserPreferences = UserPreferences()


class MeOut(ApiModel):
    user: UserOut | None = None
    platform_user: PlatformUserOut | None = None
    tenant_id: str | None = None
    role: str | None = None
    permissions: list[str] = []
    features: list[str] = []
    preferences: UserPreferences = UserPreferences()


class UserStatusIn(InputModel):
    status: AccountStatus


class UserCreateIn(InputModel):
    name: str = Field(min_length=2, max_length=160)
    email: EmailStr
    role: str = Field(max_length=40)
    speciality: str | None = Field(default=None, max_length=120)
    password: str | None = Field(default=None, min_length=12, max_length=200)


class PasswordSetIn(InputModel):
    """What a practice admin sends to give a colleague a password.

    There is no current password here, and that is the point of the route: the
    admin does not know it and must not need to. Proving your own password
    before changing it is a different act, and not this one.
    """

    password: str = Field(min_length=12, max_length=200)


class PasswordChangeIn(InputModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=12, max_length=200)
