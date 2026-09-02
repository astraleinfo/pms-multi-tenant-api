"""Tenant, entitlement, configuration and dynamic-field payloads."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import EmailStr, Field, field_validator

from app.core.tenancy import SLUG_RE
from app.models.enums import (
    Currency,
    FieldEntity,
    FieldScope,
    FieldType,
    TenantFeatureStatus,
    TenantStatus,
)
from app.schemas.common import ApiModel, InputModel, iso_date, money, zone


class TenantOut(ApiModel):
    id: str
    name: str
    slug: str
    clinical_type: str
    status: TenantStatus
    city: str
    plan: str
    since: str
    timezone: str
    #: The clinic's own date. Clients render "today" from this rather than from
    #: the browser clock, so a practice and its screens never disagree about
    #: which day it is.
    today: str

    @classmethod
    def of(cls, t) -> TenantOut:
        return cls(
            id=str(t.id), name=t.name, slug=t.slug, clinical_type=t.clinical_type,
            status=t.status, city=t.city, plan=t.plan, since=iso_date(t.since) or "",
            timezone=t.timezone, today=datetime.now(zone(t.timezone)).date().isoformat(),
        )


class ClinicalTypeOut(ApiModel):
    key: str
    label: str
    examLabel: str
    blurb: str
    complaintExample: str

    @classmethod
    def of(cls, c) -> ClinicalTypeOut:
        return cls(
            key=c.key, label=c.label, examLabel=c.exam_label,
            blurb=c.blurb, complaintExample=c.complaint_example,
        )


class FeatureOut(ApiModel):
    key: str
    name: str
    is_core: bool
    summary: str
    price_monthly: float
    unlocks: list[str]

    @classmethod
    def of(cls, f) -> FeatureOut:
        return cls(
            key=f.key, name=f.name, is_core=f.is_core, summary=f.summary,
            price_monthly=money(f.price_monthly), unlocks=list(f.unlocks or []),
        )


class TenantFeatureOut(ApiModel):
    tenant_id: str
    feature_key: str
    status: TenantFeatureStatus
    activated_at: str | None

    @classmethod
    def of(cls, tf) -> TenantFeatureOut:
        return cls(
            tenant_id=str(tf.tenant_id), feature_key=tf.feature_key,
            status=tf.status, activated_at=iso_date(tf.activated_at),
        )


class TenantConfigOut(ApiModel):
    tenant_id: str
    contact_phone: str
    contact_email: str
    address: str
    mrn_prefix: str
    day_start: str
    day_end: str
    slot_minutes: int
    default_duration: int
    token_prefix: str
    urgent_first: bool
    invoice_prefix: str
    tax_pct: float
    payment_terms_days: int
    currency: Currency
    timezone: str

    @classmethod
    def of(cls, c, tz: str) -> TenantConfigOut:
        return cls(
            tenant_id=str(c.tenant_id),
            contact_phone=c.contact_phone, contact_email=c.contact_email, address=c.address,
            mrn_prefix=c.mrn_prefix, day_start=c.day_start, day_end=c.day_end,
            slot_minutes=c.slot_minutes, default_duration=c.default_duration,
            token_prefix=c.token_prefix, urgent_first=c.urgent_first,
            invoice_prefix=c.invoice_prefix, tax_pct=money(c.tax_pct),
            payment_terms_days=c.payment_terms_days, currency=c.currency, timezone=tz,
        )


_HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class TenantConfigPatch(InputModel):
    contact_phone: str | None = Field(default=None, max_length=40)
    contact_email: str | None = Field(default=None, max_length=160)
    address: str | None = Field(default=None, max_length=500)
    mrn_prefix: str | None = Field(default=None, pattern=r"^[A-Z0-9]{1,8}$")
    day_start: str | None = Field(default=None, pattern=_HHMM)
    day_end: str | None = Field(default=None, pattern=_HHMM)
    slot_minutes: Literal[15, 20, 30] | None = None
    default_duration: int | None = Field(default=None, ge=5, le=240)
    token_prefix: str | None = Field(default=None, pattern=r"^[A-Z0-9]{1,4}$")
    urgent_first: bool | None = None
    invoice_prefix: str | None = Field(default=None, pattern=r"^[A-Z0-9-]{1,12}$")
    tax_pct: float | None = Field(default=None, ge=0, le=100)
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    currency: Currency | None = None


class FieldDefinitionOut(ApiModel):
    id: str
    scope: FieldScope
    clinical_type: str | None
    tenant_id: str | None
    entity: FieldEntity
    field_key: str
    label: str
    type: FieldType
    required: bool
    order: int
    options: list[str] | None = None
    help: str | None = None
    span: int | None = None
    group: str | None = None

    @classmethod
    def of(cls, f) -> FieldDefinitionOut:
        return cls(
            id=str(f.id), scope=f.scope, clinical_type=f.clinical_type,
            tenant_id=str(f.tenant_id) if f.tenant_id else None,
            entity=f.entity, field_key=f.field_key, label=f.label, type=f.type,
            required=f.required, order=f.order, options=list(f.options) if f.options else None,
            help=f.help, span=f.span, group=f.group,
        )


class FieldDefinitionIn(InputModel):
    """A tenant custom field. `scope` and `tenant_id` are not accepted — the
    server sets both, so this route can only ever create a field for the
    caller's own clinic."""

    entity: FieldEntity
    field_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,59}$")
    label: str = Field(min_length=1, max_length=160)
    type: FieldType
    required: bool = False
    order: int = Field(default=200, ge=0, le=10_000)
    options: list[str] | None = Field(default=None, max_length=50)
    help: str | None = Field(default=None, max_length=300)
    span: Literal[1, 2] | None = 1
    group: str | None = Field(default=None, max_length=80)

    @field_validator("options")
    @classmethod
    def _trim_options(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [o.strip()[:120] for o in v if o.strip()]


class TenantCreateIn(InputModel):
    """§3.1 — performed by the platform, never self-service inside a clinic."""

    name: str = Field(min_length=2, max_length=160)
    slug: str = Field(min_length=3, max_length=40)
    city: str = Field(default="", max_length=120)
    clinical_type: str = Field(max_length=40)
    admin_name: str = Field(min_length=2, max_length=160)
    admin_email: EmailStr
    #: No password. The console cannot choose one, and neither can this
    #: payload: the account is created without a hash and the person named
    #: here is emailed a link to set their own.

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not SLUG_RE.match(v) or v.endswith("-"):
            raise ValueError("Use lowercase letters, numbers and hyphens.")
        return v


class TenantCreatedOut(ApiModel):
    tenant: TenantOut
    admin_user_id: str
    admin_email: str
    #: Whether the invitation actually left the building. False means the
    #: clinic exists but nobody can get into it yet, and the console should say
    #: so rather than reporting a clean success.
    invitation_sent: bool = True
    #: The link itself, and only when the mail transport is `console` — that is
    #: development, where there is no inbox to check. Never populated once a
    #: real transport is configured.
    invitation_url: str | None = None


class TenantPatchIn(InputModel):
    """What the console may change about a clinic after it exists.

    `slug` is absent deliberately. It is the clinic's address and it is baked
    into every live session token; renaming it would sign everyone out and
    break every bookmark. Status has its own route, because suspending a clinic
    is not an edit — it takes the subdomain offline.
    """

    name: str | None = Field(default=None, min_length=2, max_length=160)
    city: str | None = Field(default=None, max_length=120)
    plan: str | None = Field(default=None, max_length=60)
    timezone: str | None = Field(default=None, max_length=60)
    clinical_type: str | None = Field(default=None, max_length=40)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if zone(v).key != v:
            raise ValueError("That is not a timezone this server knows.")
        return v


class TenantStatusIn(InputModel):
    status: TenantStatus


class SlugCheckOut(ApiModel):
    """Answered while someone types a subdomain into the create form.

    Advisory only — it reserves nothing. The unique index is what actually
    settles two consoles reaching for the same name at the same moment.
    """

    slug: str
    available: bool
    problem: str | None = None
    #: The address the clinic would answer on, so the form can show it.
    url: str


class TenantFeatureSetIn(InputModel):
    """The platform's own hand on a module switch."""

    status: TenantFeatureStatus


class ImpersonationOut(ApiModel):
    """A support session, opened from the console.

    An access token and nothing else: no refresh token and no cookie, so it
    expires and has to be opened again rather than quietly persisting.
    """

    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - a scheme name, not a secret
    expires_in: int
    tenant: TenantOut
    #: Where to take the browser. The clinic's own address, because that is the
    #: only host the token will be accepted on.
    url: str


class FeaturePurchaseIn(InputModel):
    """Payment happens elsewhere (§3.2); this records the outcome."""

    payment_reference: str | None = Field(default=None, max_length=120)


class PlatformTenantSummary(ApiModel):
    tenant: TenantOut
    active_features: list[str]
    users: int
    patients: int
    appointments: int
    encounters: int
    invoices: int
    revenue: float
    last_activity: str | None


class PlatformSnapshotOut(ApiModel):
    tenants: list[PlatformTenantSummary]
    totals: dict[str, float]


def uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None
