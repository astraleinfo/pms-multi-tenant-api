"""Tenant, configuration, dynamic-field and subscription payloads."""

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
    TenantStatus,
)
from app.schemas.common import ApiModel, InputModel, iso_date, local_iso, money, zone
from app.services.colour import MIN_CONTRAST, contrast_with_white


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
    #: on hold for an unpaid subscription bill — the clinic's staff see the
    #: bill and nothing else until it is settled
    billing_hold: bool = False
    billing_hold_since: str | None = None
    #: never billed by the platform
    billing_exempt: bool = False
    #: NULL until the clinic mints one from its own Settings
    referral_code: str | None = None

    @classmethod
    def of(cls, t) -> TenantOut:
        return cls(
            id=str(t.id), name=t.name, slug=t.slug, clinical_type=t.clinical_type,
            status=t.status, city=t.city, plan=t.plan, since=iso_date(t.since) or "",
            timezone=t.timezone, today=datetime.now(zone(t.timezone)).date().isoformat(),
            billing_hold=t.billing_hold_since is not None,
            billing_hold_since=local_iso(t.billing_hold_since, t.timezone),
            billing_exempt=bool(t.billing_exempt),
            referral_code=t.referral_code,
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


class AppointmentType(InputModel):
    """One kind of visit a clinic books, and how long it takes.

    The length is also the slot interval for that type: a 45-minute root canal
    is offered at 45-minute steps, a 15-minute review at 15-minute ones.
    """

    id: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    name: str = Field(min_length=1, max_length=60)
    minutes: int = Field(ge=5, le=240, multiple_of=5)

    @field_validator("name")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("an appointment type needs a name")
        return value


class TenantConfigOut(ApiModel):
    tenant_id: str
    contact_phone: str
    contact_email: str
    address: str
    registration_no: str
    gstin: str
    brand_color: str
    mrn_prefix: str
    day_start: str
    day_end: str
    slot_minutes: int
    default_duration: int
    appointment_types: list[dict]
    token_prefix: str
    urgent_first: bool
    invoice_prefix: str
    tax_pct: float
    payment_terms_days: int
    expiry_alert_days: int
    currency: Currency
    timezone: str
    has_logo: bool

    @classmethod
    def of(cls, c, tz: str) -> TenantConfigOut:
        return cls(
            tenant_id=str(c.tenant_id),
            contact_phone=c.contact_phone, contact_email=c.contact_email, address=c.address,
            registration_no=c.registration_no, gstin=c.gstin, brand_color=c.brand_color,
            mrn_prefix=c.mrn_prefix, day_start=c.day_start, day_end=c.day_end,
            slot_minutes=c.slot_minutes, default_duration=c.default_duration,
            appointment_types=list(c.appointment_types or []),
            token_prefix=c.token_prefix, urgent_first=c.urgent_first,
            invoice_prefix=c.invoice_prefix, tax_pct=money(c.tax_pct),
            payment_terms_days=c.payment_terms_days, expiry_alert_days=c.expiry_alert_days,
            currency=c.currency, timezone=tz, has_logo=c.logo_filename is not None,
        )


_HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class TenantConfigPatch(InputModel):
    contact_phone: str | None = Field(default=None, max_length=40)
    contact_email: str | None = Field(default=None, max_length=160)
    address: str | None = Field(default=None, max_length=500)
    registration_no: str | None = Field(default=None, max_length=60)
    gstin: str | None = Field(default=None, max_length=20)
    #: the colour of the band on the branded documents; white text must be readable on it
    brand_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    mrn_prefix: str | None = Field(default=None, pattern=r"^[A-Z0-9]{1,8}$")
    day_start: str | None = Field(default=None, pattern=_HHMM)
    day_end: str | None = Field(default=None, pattern=_HHMM)
    slot_minutes: Literal[15, 20, 30] | None = None
    default_duration: int | None = Field(default=None, ge=5, le=240)
    #: the whole list, replaced as one — order is the order the diary offers them
    appointment_types: list[AppointmentType] | None = Field(default=None, max_length=20)
    token_prefix: str | None = Field(default=None, pattern=r"^[A-Z0-9]{1,4}$")
    urgent_first: bool | None = None
    invoice_prefix: str | None = Field(default=None, pattern=r"^[A-Z0-9-]{1,12}$")
    tax_pct: float | None = Field(default=None, ge=0, le=100)
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    expiry_alert_days: int | None = Field(default=None, ge=1, le=730)
    currency: Currency | None = None

    @field_validator("brand_color")
    @classmethod
    def _readable_with_white(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if contrast_with_white(value) < MIN_CONTRAST:
            raise ValueError("That colour is too light for white text to be read on it. Pick a darker one.")
        return value.lower()

    @field_validator("appointment_types")
    @classmethod
    def _distinct(cls, value: list[AppointmentType] | None) -> list[AppointmentType] | None:
        if value is None:
            return value
        ids = [t.id for t in value]
        names = [t.name.casefold() for t in value]
        if len(set(ids)) != len(ids) or len(set(names)) != len(names):
            raise ValueError("appointment types must have distinct names")
        return value


class PlatformSettingsOut(ApiModel):
    support_email: str
    support_phone: str
    support_hours: str
    default_day_start: str
    default_day_end: str
    default_slot_minutes: int
    default_duration: int
    default_tax_pct: float
    default_payment_terms_days: int
    default_expiry_alert_days: int
    default_currency: Currency
    invite_token_ttl_seconds: int
    reset_token_ttl_seconds: int
    rent_due_days: int
    rent_grace_days: int
    rent_reminder_days_before_due: int
    #: the flat monthly fee every billable clinic pays; covers every module
    subscription_fee_monthly: float
    #: what one completed referral earns its referrer
    referral_credit_amount: float

    @classmethod
    def of(cls, row) -> PlatformSettingsOut:
        return cls(
            support_email=row.support_email,
            support_phone=row.support_phone,
            support_hours=row.support_hours,
            default_day_start=row.default_day_start,
            default_day_end=row.default_day_end,
            default_slot_minutes=row.default_slot_minutes,
            default_duration=row.default_duration,
            default_tax_pct=money(row.default_tax_pct),
            default_payment_terms_days=row.default_payment_terms_days,
            default_expiry_alert_days=row.default_expiry_alert_days,
            default_currency=row.default_currency,
            invite_token_ttl_seconds=row.invite_token_ttl_seconds,
            reset_token_ttl_seconds=row.reset_token_ttl_seconds,
            rent_due_days=row.rent_due_days,
            rent_grace_days=row.rent_grace_days,
            rent_reminder_days_before_due=row.rent_reminder_days_before_due,
            subscription_fee_monthly=money(row.subscription_fee_monthly),
            referral_credit_amount=money(row.referral_credit_amount),
        )


class PlatformSettingsPatch(InputModel):
    support_email: str | None = Field(default=None, max_length=200)
    support_phone: str | None = Field(default=None, max_length=40)
    support_hours: str | None = Field(default=None, max_length=200)
    default_day_start: str | None = Field(default=None, pattern=_HHMM)
    default_day_end: str | None = Field(default=None, pattern=_HHMM)
    default_slot_minutes: Literal[15, 20, 30] | None = None
    default_duration: int | None = Field(default=None, ge=5, le=240)
    default_tax_pct: float | None = Field(default=None, ge=0, le=100)
    default_payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    default_expiry_alert_days: int | None = Field(default=None, ge=1, le=730)
    default_currency: Currency | None = None
    invite_token_ttl_seconds: int | None = Field(default=None, ge=86400, le=2592000)
    reset_token_ttl_seconds: int | None = Field(default=None, ge=3600, le=259200)
    rent_due_days: int | None = Field(default=None, ge=1, le=60)
    rent_grace_days: int | None = Field(default=None, ge=0, le=60)
    rent_reminder_days_before_due: int | None = Field(default=None, ge=0, le=30)
    #: `Numeric(10, 2)` — the bound is the column's, so an absurd figure is a
    #: 422 here rather than a database error on flush
    subscription_fee_monthly: float | None = Field(default=None, ge=0, le=99_999_999.99)
    referral_credit_amount: float | None = Field(default=None, ge=0, le=999_999.99)


class LabTestAdminOut(ApiModel):
    code: str
    name: str
    panel: str
    clinical_types: list[str] | None

    @classmethod
    def of(cls, row) -> LabTestAdminOut:
        return cls(code=row.code, name=row.name, panel=row.panel, clinical_types=row.clinical_types)


class LabTestIn(InputModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=160)
    panel: str = Field(default="", max_length=80)
    clinical_types: list[str] | None = None


class LabTestPatch(InputModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    panel: str | None = Field(default=None, max_length=80)
    clinical_types: list[str] | None = None


class MedicineAdminOut(ApiModel):
    id: str
    medicine: str
    strength: str
    dosage: str
    frequency: str
    duration: str
    clinical_types: list[str] | None

    @classmethod
    def of(cls, row) -> MedicineAdminOut:
        return cls(
            id=str(row.id), medicine=row.medicine, strength=row.strength, dosage=row.dosage,
            frequency=row.frequency, duration=row.duration, clinical_types=row.clinical_types,
        )


class MedicineIn(InputModel):
    medicine: str = Field(min_length=1, max_length=160)
    strength: str = Field(default="", max_length=60)
    dosage: str = Field(default="", max_length=80)
    frequency: str = Field(default="", max_length=80)
    duration: str = Field(default="", max_length=80)
    clinical_types: list[str] | None = None


class MedicinePatch(InputModel):
    medicine: str | None = Field(default=None, min_length=1, max_length=160)
    strength: str | None = Field(default=None, max_length=60)
    dosage: str | None = Field(default=None, max_length=80)
    frequency: str | None = Field(default=None, max_length=80)
    duration: str | None = Field(default=None, max_length=80)
    clinical_types: list[str] | None = None


class ClinicalTypeCopyPatch(InputModel):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    blurb: str | None = Field(default=None, max_length=2000)
    exam_label: str | None = Field(default=None, max_length=120)
    complaint_example: str | None = Field(default=None, max_length=200)


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
    #: quoted by the clinic that referred this one — resolved against
    #: `tenants.referral_code` by the service, not validated here (that needs
    #: a database read)
    referral_code: str | None = Field(default=None, max_length=20)
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
    #: The invitation email is the worker's, so it is `queued` here. Whether it
    #: left is the account's `link_email_status` on the clinic's staff list,
    #: which the console reads until it says `sent` or `failed`.
    invitation_status: str = "queued"


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
    #: never bill this clinic (and lift any hold it is under)
    billing_exempt: bool | None = None

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


class PlatformTenantSummary(ApiModel):
    tenant: TenantOut
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


# --- referral credits ---------------------------------------------------------


class ReferralOut(ApiModel):
    id: str
    referred_tenant_name: str
    code_used: str
    credit_amount: float
    currency: str
    status: str
    created_at: str
    vested_at: str | None

    @classmethod
    def of(cls, r, referred_name: str) -> ReferralOut:
        return cls(
            id=str(r.id), referred_tenant_name=referred_name, code_used=r.code_used,
            credit_amount=money(r.credit_amount), currency=r.currency, status=r.status.value,
            created_at=local_iso(r.created_at) or "", vested_at=local_iso(r.vested_at),
        )


class ReferralProgramOut(ApiModel):
    """A clinic's own referral programme: its code (if it has minted one),
    what a referral is worth right now, its spendable balance, and every
    referral it has made."""

    code: str | None
    #: `XXXX-XXXX`, for display — `None` when `code` is `None`
    formatted_code: str | None
    #: what a *new* referral earns today — already-earned referrals keep
    #: whatever was promised when they were created
    credit_amount: float
    currency: str
    balance: float
    referrals: list[ReferralOut]


class ReferredByOut(ApiModel):
    referrer_tenant_name: str
    code_used: str
    status: str
    created_at: str


class ConsoleReferralsOut(ApiModel):
    """The console's view of one clinic's referral story: who referred it
    (if anyone), and its own programme as a referrer."""

    referred_by: ReferredByOut | None
    program: ReferralProgramOut


# --- subscription rent ---------------------------------------------------------


class SubscriptionLineOut(ApiModel):
    """One charge on a bill. Today that is always the flat clinic fee; a bill
    raised before the flat fee may still carry a `feature_key` in its stored
    JSON, which is ignored."""

    name: str
    amount: float

    @classmethod
    def of(cls, line: dict) -> SubscriptionLineOut:
        return cls(name=str(line.get("name", "")), amount=float(line.get("amount", 0)))


class SubscriptionInvoiceOut(ApiModel):
    id: str
    tenant_id: str
    number: str
    period_start: str
    period_end: str
    currency: str
    amount: float
    lines: list[SubscriptionLineOut]
    issued_on: str
    due_on: str
    #: the first day the clinic is held if this is still open
    hold_on: str
    status: str
    reminder_stage: str
    reminders_sent: int
    paid_at: str | None
    payment_reference: str
    settled_by: str

    @classmethod
    def of(cls, b, hold_on) -> SubscriptionInvoiceOut:
        return cls(
            id=str(b.id), tenant_id=str(b.tenant_id), number=b.number,
            period_start=iso_date(b.period_start) or "", period_end=iso_date(b.period_end) or "",
            currency=b.currency, amount=money(b.amount),
            lines=[SubscriptionLineOut.of(line) for line in (b.lines or [])],
            issued_on=iso_date(b.issued_on) or "", due_on=iso_date(b.due_on) or "",
            hold_on=iso_date(hold_on) or "", status=b.status.value,
            reminder_stage=b.reminder_stage, reminders_sent=b.reminders_sent,
            paid_at=local_iso(b.paid_at),
            payment_reference=b.payment_reference, settled_by=b.settled_by,
        )


class SubscriptionSummaryOut(ApiModel):
    on_hold: bool
    hold_since: str | None
    exempt: bool
    currency: str
    outstanding: float
    open_count: int
    next_due_on: str | None
    next_hold_on: str | None
    overdue: bool
    due_days: int
    grace_days: int


class SubscriptionPlanOut(ApiModel):
    """What the clinic pays the platform a month. One flat fee covers every
    module; there is nothing to switch on or price per module."""

    #: the platform's flat monthly clinic fee today
    price_monthly: float
    #: whether next month's bill charges this clinic
    billed: bool
    #: why not, when `billed` is false
    not_billed_reason: Literal["exempt", "trial", "suspended", "no_fee"] | None = None
    #: `price_monthly` when billed, else 0 — the bill before any referral credit
    monthly_total: float
    #: spendable referral credit right now
    credit_balance: float
    #: what the next bill will actually come to, after credit — this is the
    #: figure that should be shown as "what you'll pay"
    amount_due: float


class SubscriptionOut(ApiModel):
    summary: SubscriptionSummaryOut
    invoices: list[SubscriptionInvoiceOut]
    plan: SubscriptionPlanOut | None = None


class SettleIn(InputModel):
    reference: str = Field(default="", max_length=120)


class BillingRunOut(ApiModel):
    clinics: int
    raised: list[str]
    reminded: list[str]
    held: list[str]
    lifted: list[str]
    failed: list[str]


class PlatformBillingRowOut(ApiModel):
    tenant: TenantOut
    summary: SubscriptionSummaryOut
    invoices: list[SubscriptionInvoiceOut]


class PlatformBillingOut(ApiModel):
    enabled: bool
    currency: str
    #: the flat monthly clinic fee (`platform_settings.subscription_fee_monthly`)
    fee_monthly: float
    due_days: int
    grace_days: int
    reminder_days_before_due: int
    clinics: list[PlatformBillingRowOut]
