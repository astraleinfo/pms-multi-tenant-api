"""The clinical core: patients, appointments, the queue, encounters,
prescriptions and lab orders.

`clinical_ext` is the one place a clinical type changes the shape of the data;
every other column is common to all disciplines, exactly as in §3.7.
"""

from __future__ import annotations

import uuid
from datetime import date as date_t
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, deferred, mapped_column, relationship

from app.db.base import Base, TenantScoped, TimestampMixin, uuid_pk
from app.models.enums import (
    AppointmentChannel,
    AppointmentStatus,
    DispenseStatus,
    EncounterStatus,
    FollowUpOutcome,
    LabOrderStatus,
    QueuePriority,
    QueueStatus,
    Sex,
)

if TYPE_CHECKING:
    from app.models.identity import User


def _enum(cls, name: str):
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class Patient(Base, TenantScoped, TimestampMixin):
    __tablename__ = "patients"
    __extra_table_args__ = (
        UniqueConstraint("tenant_id", "mrn", name="uq_patients_tenant_mrn"),
        Index("ix_patients_tenant_name", "tenant_id", "full_name"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    mrn: Mapped[str] = mapped_column(String(40), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    dob: Mapped[date_t] = mapped_column(Date, nullable=False)
    sex: Mapped[Sex] = mapped_column(_enum(Sex, "sex"), nullable=False, default=Sex.OTHER)
    phone: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    blood_group: Mapped[str | None] = mapped_column(String(8), nullable=True)
    allergies: Mapped[str | None] = mapped_column(Text, nullable=True)
    registered_at: Mapped[date_t] = mapped_column(Date, nullable=False)
    last_visit: Mapped[date_t | None] = mapped_column(Date, nullable=True)
    clinical_ext: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


class Appointment(Base, TenantScoped, TimestampMixin):
    __tablename__ = "appointments"
    __extra_table_args__ = (
        Index("ix_appointments_tenant_date", "tenant_id", "date"),
        Index("ix_appointments_doctor_day", "tenant_id", "doctor_id", "date"),
        CheckConstraint("duration_min between 5 and 480", name="ck_appt_duration"),
        CheckConstraint('"time" ~ \'^([01][0-9]|2[0-3]):[0-5][0-9]$\'', name="ck_appt_time"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    date: Mapped[date_t] = mapped_column(Date, nullable=False)
    time: Mapped[str] = mapped_column(String(5), nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    reason: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    status: Mapped[AppointmentStatus] = mapped_column(
        _enum(AppointmentStatus, "appointment_status"), nullable=False, default=AppointmentStatus.SCHEDULED
    )
    created_via: Mapped[AppointmentChannel] = mapped_column(
        _enum(AppointmentChannel, "appointment_channel"),
        nullable=False,
        default=AppointmentChannel.FRONT_DESK,
    )

    # Loaded with the row, so a list response can name its patient without the
    # client holding the whole register. `selectin` keeps it to one extra query
    # per page, and the load runs on the same tenant-bound session as the query
    # that produced these rows.
    patient: Mapped[Patient] = relationship("Patient", lazy="selectin")


class PatientNote(Base, TenantScoped, TimestampMixin):
    """A clinician's note on a patient, outside any one consultation.

    Notes are part of the record, so they are never rewritten: a note is
    corrected by writing another that points at it (`amends_id`), and both stay.
    A trigger refuses any change to a saved note's words, so no path through
    the database can quietly overwrite one either.
    """

    __tablename__ = "patient_notes"
    __extra_table_args__ = (
        Index("ix_patient_notes_patient", "tenant_id", "patient_id", "created_at"),
        CheckConstraint("length(btrim(body)) > 0", name="ck_patient_note_body"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Who wrote it, as they were then: an account can be renamed, change role
    # or be removed, and the note must still say who wrote it and in what capacity.
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author_label: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    author_role: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    #: the earlier note this one corrects, if it is a correction
    amends_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patient_notes.id", ondelete="CASCADE"), nullable=True
    )


class FollowUpCall(Base, TenantScoped, TimestampMixin):
    """One call to a patient about an upcoming appointment, and what came of it.

    The log is append-only and is the whole history of a follow-up: every
    attempt, who made it, and when. Where the follow-up stands now is worked
    out from the last of these and the appointment itself, never stored twice.
    """

    __tablename__ = "follow_up_calls"
    __extra_table_args__ = (
        Index("ix_follow_up_calls_appointment", "tenant_id", "appointment_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    outcome: Mapped[FollowUpOutcome] = mapped_column(
        _enum(FollowUpOutcome, "follow_up_outcome"), nullable=False
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # The person, and their name as it was: an account can be renamed or
    # removed, and the log must still say who rang.
    called_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    called_by_label: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    # Only a `rescheduled` call moves anything: `YYYY-MM-DD HH:mm`, either side.
    moved_from: Mapped[str | None] = mapped_column(String(16), nullable=True)
    moved_to: Mapped[str | None] = mapped_column(String(16), nullable=True)


class QueueEntry(Base, TenantScoped, TimestampMixin):
    """§3.6. `token_no` is unique per tenant per day — walk-ins and checked-in
    appointments draw from the same sequence."""

    __tablename__ = "queue_entries"
    __extra_table_args__ = (
        UniqueConstraint("tenant_id", "queue_date", "token_no", name="uq_queue_token_per_day"),
        Index("ix_queue_tenant_date_status", "tenant_id", "queue_date", "status"),
        CheckConstraint("token_no > 0", name="ck_queue_token_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    queue_date: Mapped[date_t] = mapped_column(Date, nullable=False)
    token_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[QueueStatus] = mapped_column(
        _enum(QueueStatus, "queue_status"), nullable=False, default=QueueStatus.WAITING
    )
    priority: Mapped[QueuePriority] = mapped_column(
        _enum(QueuePriority, "queue_priority"), nullable=False, default=QueuePriority.ROUTINE
    )
    reason: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    checked_in_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    patient: Mapped[Patient] = relationship("Patient", lazy="selectin")


class Encounter(Base, TenantScoped, TimestampMixin):
    __tablename__ = "encounters"
    __extra_table_args__ = (
        Index("ix_encounters_tenant_patient", "tenant_id", "patient_id"),
        Index("ix_encounters_tenant_date", "tenant_id", "date"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    queue_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("queue_entries.id", ondelete="SET NULL"), nullable=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    date: Mapped[date_t] = mapped_column(Date, nullable=False)
    chief_complaint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    diagnosis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    follow_up_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clinical_ext: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[EncounterStatus] = mapped_column(
        _enum(EncounterStatus, "encounter_status"), nullable=False, default=EncounterStatus.DRAFT
    )
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    patient: Mapped[Patient] = relationship("Patient", lazy="selectin")


class Prescription(Base, TenantScoped, TimestampMixin):
    __tablename__ = "prescriptions"
    __extra_table_args__ = (Index("ix_prescriptions_tenant_status", "tenant_id", "status"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[DispenseStatus] = mapped_column(
        _enum(DispenseStatus, "dispense_status"), nullable=False, default=DispenseStatus.PENDING
    )

    items: Mapped[list[PrescriptionItem]] = relationship(
        "PrescriptionItem",
        back_populates="prescription",
        cascade="all, delete-orphan",
        order_by="PrescriptionItem.position",
        lazy="selectin",
    )
    patient: Mapped[Patient] = relationship("Patient", lazy="selectin")
    #: Read for the name only. The staff list hides support accounts, so a screen
    #: that looks the prescriber up there finds nobody for a visit they recorded.
    doctor: Mapped[User] = relationship("User", lazy="selectin", foreign_keys=[doctor_id])


class PrescriptionItem(Base, TenantScoped, TimestampMixin):
    __tablename__ = "prescription_items"
    __extra_table_args__ = (
        CheckConstraint("qty > 0", name="ck_rx_item_qty"),
        CheckConstraint("dispensed_qty >= 0 and dispensed_qty <= qty", name="ck_rx_item_dispensed"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    prescription_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("prescriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("inventory_items.id", ondelete="SET NULL"), nullable=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    medicine: Mapped[str] = mapped_column(String(200), nullable=False)
    strength: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    dosage: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    frequency: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    duration: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    dispensed_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[DispenseStatus] = mapped_column(
        _enum(DispenseStatus, "dispense_status"), nullable=False, default=DispenseStatus.PENDING
    )

    prescription: Mapped[Prescription] = relationship("Prescription", back_populates="items")


class LabOrder(Base, TenantScoped, TimestampMixin):
    """§3.10. Results exist from the moment a technician types them, but they
    are only released into the patient record once verified."""

    __tablename__ = "lab_orders"
    __extra_table_args__ = (Index("ix_lab_orders_tenant_status", "tenant_id", "status"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    encounter_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordered_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    test_code: Mapped[str] = mapped_column(String(20), nullable=False)
    test_name: Mapped[str] = mapped_column(String(160), nullable=False)
    panel: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    status: Mapped[LabOrderStatus] = mapped_column(
        _enum(LabOrderStatus, "lab_order_status"), nullable=False, default=LabOrderStatus.ORDERED
    )
    ordered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resulted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resulted_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    results: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    patient: Mapped[Patient] = relationship("Patient", lazy="selectin", foreign_keys=[patient_id])

    __mapper_args__ = {"confirm_deleted_rows": False}


class LabAttachment(Base, TenantScoped, TimestampMixin):
    """A file that belongs to a lab order — a scan, a machine printout, a
    signed report.

    The bytes live in one of two places (`app/services/storage.py`): in `data`,
    where row-level security covers them exactly as it covers the order they
    hang off, or in S3 under `storage_key`, reached only through this row. One
    of the two is always set — the check constraint says so. `data` is
    deferred, so listing an order's attachments never drags files across the
    wire.
    """

    __tablename__ = "lab_attachments"
    __extra_table_args__ = (
        Index("ix_lab_attachments_tenant_order", "tenant_id", "lab_order_id"),
        CheckConstraint("size_bytes > 0", name="ck_lab_attachment_size"),
        CheckConstraint(
            "data IS NOT NULL OR storage_key IS NOT NULL", name="ck_lab_attachment_stored"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    lab_order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("lab_orders.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    data: Mapped[bytes | None] = deferred(mapped_column(LargeBinary, nullable=True))
    #: the object's key in the bucket when the file is held in S3; NULL when in `data`
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

