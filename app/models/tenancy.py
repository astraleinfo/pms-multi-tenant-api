"""Tenants, and how they have configured the product.

Three axes live in three separate places, and nothing here conflates them: the
`tenant_id` on every other table (who owns the row), `Tenant.clinical_type`
(what clinical workflow applies), and customisation — `TenantConfig` (how the
clinic is set up) plus its own `FieldDefinition` rows (what extra it records).

There is no fourth axis for modules. Every tenant has every module; what a
person can reach inside one is decided by their role's permissions.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.core.config import settings
from app.db.base import Base, TenantScoped, TimestampMixin, uuid_pk
from app.models.enums import (
    Currency,
    FieldEntity,
    FieldScope,
    FieldType,
    TenantStatus,
)
from app.services.colour import DEFAULT_BRAND


class ClinicalType(Base):
    __tablename__ = "clinical_types"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    exam_label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    blurb: Mapped[str] = mapped_column(Text, nullable=False, default="")
    complaint_example: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)
    clinical_type: Mapped[str] = mapped_column(
        String(40), ForeignKey("clinical_types.key"), nullable=False
    )
    status: Mapped[TenantStatus] = mapped_column(
        Enum(TenantStatus, name="tenant_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=TenantStatus.TRIAL,
    )
    city: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    plan: Mapped[str] = mapped_column(String(60), nullable=False, default="Trial")
    since: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(60), nullable=False, default=lambda: settings.pms_timezone)
    #: Set when the clinic is put on hold for an unpaid subscription bill, and
    #: cleared the moment it is settled. Distinct from `status`: a suspension
    #: is a decision somebody made, a hold is arithmetic, and paying must lift
    #: the one without undoing the other.
    billing_hold_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Never billed — an internal or demo clinic, or one on a separate contract.
    billing_exempt: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: minted on demand from the clinic's own Settings — NULL until it asks.
    #: Unique: it is how a signup resolves which clinic referred it.
    referral_code: Mapped[str | None] = mapped_column(String(16), nullable=True, unique=True)

    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,39}$'", name="ck_tenants_slug_shape"),
    )


class TenantConfig(Base, TenantScoped, TimestampMixin):
    """§3.3 — how this clinic has set the product up."""

    __tablename__ = "tenant_configs"
    __extra_table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_config_tenant"),
        CheckConstraint("slot_minutes in (15, 20, 30)", name="ck_config_slot_minutes"),
        CheckConstraint("default_duration between 5 and 240", name="ck_config_duration"),
        CheckConstraint("tax_pct >= 0 and tax_pct <= 100", name="ck_config_tax"),
        CheckConstraint("payment_terms_days between 0 and 365", name="ck_config_terms"),
        CheckConstraint("expiry_alert_days between 1 and 730", name="ck_config_expiry_alert"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()

    contact_phone: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    contact_email: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    address: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: What a printed document says about who this clinic is — all optional, and
    #: printed only when set. `brand_color` is the colour of the branded
    #: documents' band (an invoice, say); it always has one, and always one that
    #: white text can be read on (the schema refuses any other).
    registration_no: Mapped[str] = mapped_column(String(60), nullable=False, default="", server_default="")
    gstin: Mapped[str] = mapped_column(String(20), nullable=False, default="", server_default="")
    brand_color: Mapped[str] = mapped_column(
        String(7), nullable=False, default=DEFAULT_BRAND, server_default=DEFAULT_BRAND
    )

    mrn_prefix: Mapped[str] = mapped_column(String(8), nullable=False, default="AS")
    mrn_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1501)

    day_start: Mapped[str] = mapped_column(String(5), nullable=False, default="09:00")
    day_end: Mapped[str] = mapped_column(String(5), nullable=False, default="17:30")
    slot_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    default_duration: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    #: The kinds of visit this clinic books — ``[{"id", "name", "minutes"}]``.
    #: A booking of a type is that long and its slots step at that interval;
    #: ``slot_minutes`` is only the fallback for a clinic that has defined none.
    appointment_types: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    token_prefix: Mapped[str] = mapped_column(String(4), nullable=False, default="A")
    urgent_first: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    invoice_prefix: Mapped[str] = mapped_column(String(12), nullable=False, default="AS-INV")
    invoice_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=2044)
    tax_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    payment_terms_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    expiry_alert_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    currency: Mapped[Currency] = mapped_column(
        Enum(Currency, name="currency", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=Currency.INR,
    )

    #: How an invoice emailed to a patient reads, and where a reply goes. The
    #: mail itself goes out through the platform's transport — no clinic ever
    #: stores a mail password here. All optional: empty means the default
    #: (`services/invoice_email.py` says what each one falls back to).
    invoice_email_reply_to: Mapped[str] = mapped_column(
        String(160), nullable=False, default="", server_default=""
    )
    invoice_email_sender_name: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=""
    )
    invoice_email_subject: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )
    invoice_email_message: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    #: The clinic's own mark — shown in its own rail and printed on its PDFs.
    #: Unset by default; a clinic that has not uploaded one gets no image
    #: rather than a placeholder. Same two-places-to-live split as
    #: `LabAttachment`: `logo_data` in the row, or `logo_storage_key` in S3,
    #: never both — `logo_filename` is null exactly when neither is set.
    logo_filename: Mapped[str | None] = mapped_column(String(200), nullable=True)
    logo_content_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    logo_data: Mapped[bytes | None] = deferred(mapped_column(LargeBinary, nullable=True))
    logo_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)


class PlatformSettings(Base, TimestampMixin):
    """§ platform settings — how the platform itself is configured, as
    opposed to how one clinic has set itself up (`TenantConfig`).

    A singleton, as a database fact rather than an application convention:
    the `ck_platform_settings_singleton` check constraint refuses any row
    whose id is not this fixed value, and the id is the primary key, so no
    second row can ever exist. Not tenant-scoped and carries no row-level
    security — this table belongs to the platform, never to a clinic.
    """

    __tablename__ = "platform_settings"
    __table_args__ = (
        CheckConstraint(
            "id = '00000000-0000-0000-0000-000000000001'", name="ck_platform_settings_singleton"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)

    # support desk
    support_email: Mapped[str] = mapped_column(String(200), nullable=False, default="astraleinfo@gmail.com")
    support_phone: Mapped[str] = mapped_column(String(40), nullable=False, default="7305961254")
    support_hours: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    # new-tenant defaults — read once, when a clinic's TenantConfig is first
    # created (see `services/tenants.py::get_config`); never re-applied.
    default_day_start: Mapped[str] = mapped_column(String(5), nullable=False, default="09:00")
    default_day_end: Mapped[str] = mapped_column(String(5), nullable=False, default="17:30")
    default_slot_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    default_duration: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    default_tax_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    default_payment_terms_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    default_expiry_alert_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    default_currency: Mapped[Currency] = mapped_column(
        Enum(Currency, name="currency", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=Currency.INR,
    )

    # timing knobs — read via `services/platform.py::get_timings`, resolved
    # once per unit of work and passed down as a `Timings` value. See
    # docs/superpowers/specs/2026-09-24-console-commercial-pages-design.md's
    # Decisions section for why this is a value, not a re-read per call.
    invite_token_ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=604800)
    reset_token_ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=7200)
    rent_due_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    rent_grace_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    rent_reminder_days_before_due: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    # the platform's monthly bill — one flat fee per billable clinic, covering
    # every module. Read into `Timings` with the knobs above, so one billing run
    # bills every clinic at the same figure.
    subscription_fee_monthly: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=0, server_default="0"
    )

    # referral programme — what one completed referral is worth. Read
    # directly by `platform.create_tenant`, not folded into `Timings`: it is
    # not an operational clock, and threading it through that dataclass would
    # make every `Timings` consumer carry a field it never uses.
    referral_credit_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("100.00")
    )


class FieldDefinition(Base, TimestampMixin):
    """§3.3 — the dynamic-form schema.

    Deliberately *not* `TenantScoped`: common and clinical-type rows belong to
    the platform and are shared. Its RLS policy allows a row whose `tenant_id`
    is null, or one that matches the caller's tenant — nothing else.
    """

    __tablename__ = "field_definitions"

    id: Mapped[uuid.UUID] = uuid_pk()
    scope: Mapped[FieldScope] = mapped_column(
        Enum(FieldScope, name="field_scope", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    clinical_type: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("clinical_types.key"), nullable=True
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    entity: Mapped[FieldEntity] = mapped_column(
        Enum(FieldEntity, name="field_entity", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    field_key: Mapped[str] = mapped_column(String(60), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    type: Mapped[FieldType] = mapped_column(
        Enum(FieldType, name="field_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    options: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    help: Mapped[str | None] = mapped_column(Text, nullable=True)
    span: Mapped[int | None] = mapped_column(Integer, nullable=True)
    group: Mapped[str | None] = mapped_column(String(80), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "(scope = 'tenant' and tenant_id is not null) or (scope <> 'tenant' and tenant_id is null)",
            name="ck_field_scope_tenant",
        ),
        CheckConstraint(
            "(scope = 'clinical_type' and clinical_type is not null) "
            "or (scope <> 'clinical_type' and clinical_type is null)",
            name="ck_field_scope_clinical",
        ),
        CheckConstraint("field_key ~ '^[a-z][a-z0-9_]{0,59}$'", name="ck_field_key_shape"),
        CheckConstraint("span is null or span in (1, 2)", name="ck_field_span"),
        UniqueConstraint("tenant_id", "entity", "field_key", name="uq_field_tenant_entity_key"),
    )


class LabTest(Base):
    """Shared order catalogue, filtered by clinical type when offered."""

    __tablename__ = "lab_tests"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    panel: Mapped[str] = mapped_column(String(80), nullable=False)
    clinical_types: Mapped[list | None] = mapped_column(JSONB, nullable=True)


class MedicineSuggestion(Base):
    __tablename__ = "medicine_suggestions"

    id: Mapped[uuid.UUID] = uuid_pk()
    medicine: Mapped[str] = mapped_column(String(160), nullable=False)
    strength: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    dosage: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    frequency: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    duration: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    clinical_types: Mapped[list | None] = mapped_column(JSONB, nullable=True)


class AuditEvent(Base):
    """Append-only trail. Tenant-nullable so platform actions are recorded too;
    its RLS policy matches `field_definitions` — your tenant's rows or none."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor_label: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    entity: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    entity_id: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
