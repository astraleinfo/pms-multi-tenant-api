"""Tenants, what they have bought, and how they have configured the product.

The four axes of the reference document live in four separate places, and
nothing here conflates them: `Tenant.clinical_type` (what workflow applies),
`TenantFeature` (what is unlocked), `TenantConfig` (how it is set up), and the
`tenant_id` on every other table (who owns the row).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
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
from app.models.enums import (
    Currency,
    FieldEntity,
    FieldScope,
    FieldType,
    TenantFeatureStatus,
    TenantStatus,
)


class ClinicalType(Base):
    __tablename__ = "clinical_types"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    exam_label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    blurb: Mapped[str] = mapped_column(Text, nullable=False, default="")
    complaint_example: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class Feature(Base):
    """The catalogue of modules. Which tenant has which is `TenantFeature`."""

    __tablename__ = "features"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    is_core: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    price_monthly: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    unlocks: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


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
    timezone: Mapped[str] = mapped_column(String(60), nullable=False, default="Europe/London")

    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,39}$'", name="ck_tenants_slug_shape"),
    )


class TenantFeature(Base, TenantScoped, TimestampMixin):
    """§3.2 — entitlement. Never consulted to decide clinical behaviour."""

    __tablename__ = "tenant_features"
    __extra_table_args__ = (UniqueConstraint("tenant_id", "feature_key", name="uq_tenant_feature"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    feature_key: Mapped[str] = mapped_column(String(40), ForeignKey("features.key"), nullable=False)
    status: Mapped[TenantFeatureStatus] = mapped_column(
        Enum(
            TenantFeatureStatus,
            name="tenant_feature_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=TenantFeatureStatus.INACTIVE,
    )
    activated_at: Mapped[date | None] = mapped_column(Date, nullable=True)


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

    mrn_prefix: Mapped[str] = mapped_column(String(8), nullable=False, default="AS")
    mrn_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1501)

    day_start: Mapped[str] = mapped_column(String(5), nullable=False, default="09:00")
    day_end: Mapped[str] = mapped_column(String(5), nullable=False, default="17:30")
    slot_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    default_duration: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

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
