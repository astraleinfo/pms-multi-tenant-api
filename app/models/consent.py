"""Consent forms: the clinic's own copy of a template, its versions, and the
consents patients give against one exact version.

    system template (code, read-only)
        → consent_forms              one clinic's form, made from a template
        → consent_form_versions      v1, v2, … — one draft and one published at most
        → patient_consents           a patient's answer to one exact version

A patient consent copies what it was asked against — the version's content,
the patient's details as they stood, the clinician — so it reads the same in
ten years, whatever the clinic has published since. Once signed or declined it
cannot change: the service refuses, and a trigger refuses underneath it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, LargeBinary, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantScoped, TimestampMixin, uuid_pk
from app.models.enums import ConsentStatus, ConsentVersionStatus


def _enum(cls, name: str):
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class ConsentForm(Base, TenantScoped, TimestampMixin):
    __tablename__ = "consent_forms"
    __extra_table_args__ = (Index("ix_consent_forms_tenant_template", "tenant_id", "template_key"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    #: the system template this form was made from — what "reset" goes back to
    template_key: Mapped[str] = mapped_column(String(40), nullable=False)
    #: the form's name in lists; the printed title lives in each version's content
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    created_by_label: Mapped[str] = mapped_column(String(160), nullable=False, default="")

    versions: Mapped[list[ConsentFormVersion]] = relationship(
        "ConsentFormVersion",
        lazy="selectin",
        order_by="ConsentFormVersion.number.desc()",
        back_populates="form",
    )


class ConsentFormVersion(Base, TenantScoped, TimestampMixin):
    __tablename__ = "consent_form_versions"
    __extra_table_args__ = (
        Index("uq_consent_versions_form_number", "form_id", "number", unique=True),
        # one draft and one published version per form, at most
        Index(
            "uq_consent_versions_one_draft", "form_id", unique=True, postgresql_where=text("status = 'draft'")
        ),
        Index(
            "uq_consent_versions_one_published",
            "form_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    form_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("consent_forms.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ConsentVersionStatus] = mapped_column(
        _enum(ConsentVersionStatus, "consent_version_status"),
        nullable=False,
        default=ConsentVersionStatus.DRAFT,
    )
    #: the structured form — see `app.schemas.consent.ConsentContent`
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)

    created_by_label: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    updated_by_label: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    published_by_label: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    form: Mapped[ConsentForm] = relationship("ConsentForm", back_populates="versions")


class PatientConsent(Base, TenantScoped, TimestampMixin):
    __tablename__ = "patient_consents"
    __extra_table_args__ = (
        Index("ix_patient_consents_tenant_patient", "tenant_id", "patient_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    form_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("consent_forms.id", ondelete="RESTRICT"), nullable=False
    )
    #: the exact version asked — RESTRICT, so a version a patient answered can never go
    version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("consent_form_versions.id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    #: what the patient was shown, copied at the time
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: who they were at the time — name, record number, date of birth …
    patient_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: what the clinic filled in for this patient: the procedure, the site …
    field_values: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    provider_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    provider_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")

    status: Mapped[ConsentStatus] = mapped_column(
        _enum(ConsentStatus, "consent_status"),
        nullable=False,
        default=ConsentStatus.PENDING,
    )
    requested_by_label: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: the acknowledgement ids the patient ticked
    acknowledged: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    signer_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    signer_relationship: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    patient_signature: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    provider_signature: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    witnessed_by_label: Mapped[str] = mapped_column(String(160), nullable=False, default="")

    declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decline_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
