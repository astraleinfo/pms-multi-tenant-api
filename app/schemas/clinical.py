"""Patients, appointments, queue, encounters, prescriptions and lab orders."""

from __future__ import annotations

from datetime import date as date_t
from typing import Any, Literal

from pydantic import Field, field_validator

from app.models.enums import (
    AppointmentChannel,
    AppointmentStatus,
    DispenseStatus,
    EncounterStatus,
    LabOrderStatus,
    QueuePriority,
    QueueStatus,
    ResultFlag,
    Sex,
)
from app.schemas.common import ApiModel, InputModel, clock, iso_date, stamp

# --- patients ---------------------------------------------------------------


class PatientOut(ApiModel):
    id: str
    tenant_id: str
    mrn: str
    full_name: str
    dob: str
    sex: Sex
    phone: str
    email: str | None = None
    address: str | None = None
    blood_group: str | None = None
    allergies: str | None = None
    registered_at: str
    last_visit: str | None
    clinical_ext: dict[str, Any]
    tags: list[str]

    @classmethod
    def of(cls, p) -> PatientOut:
        return cls(
            id=str(p.id), tenant_id=str(p.tenant_id), mrn=p.mrn, full_name=p.full_name,
            dob=iso_date(p.dob) or "", sex=p.sex, phone=p.phone, email=p.email, address=p.address,
            blood_group=p.blood_group, allergies=p.allergies,
            registered_at=iso_date(p.registered_at) or "", last_visit=iso_date(p.last_visit),
            clinical_ext=dict(p.clinical_ext or {}), tags=list(p.tags or []),
        )


class PatientPageOut(ApiModel):
    """A page of the register, and how many rows the query matched in total.

    The count is what makes a pager honest: the client shows "1–25 of 3,481"
    without ever holding 3,481 rows.
    """

    items: list[PatientOut]
    total: int
    limit: int
    offset: int


class PatientIn(InputModel):
    """Common fields are typed; everything else is validated against the
    tenant's effective schema (§3.3) and stored in `clinical_ext`."""

    full_name: str = Field(min_length=1, max_length=200)
    dob: date_t
    sex: Sex = Sex.OTHER
    phone: str = Field(default="", max_length=40)
    email: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=500)
    blood_group: str | None = Field(default=None, max_length=8)
    allergies: str | None = Field(default=None, max_length=500)
    clinical_ext: dict[str, Any] = Field(default_factory=dict)

    @field_validator("dob")
    @classmethod
    def _sane_dob(cls, v: date_t) -> date_t:
        if v > date_t.today():
            raise ValueError("A date of birth cannot be in the future.")
        if v.year < 1890:
            raise ValueError("That date of birth looks wrong.")
        return v


class PatientPatch(InputModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    dob: date_t | None = None
    sex: Sex | None = None
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=500)
    blood_group: str | None = Field(default=None, max_length=8)
    allergies: str | None = Field(default=None, max_length=500)
    clinical_ext: dict[str, Any] | None = None
    tags: list[str] | None = Field(default=None, max_length=20)


# --- appointments -----------------------------------------------------------

_HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class AppointmentOut(ApiModel):
    id: str
    tenant_id: str
    patient_id: str
    #: Denormalised for display. A list of appointments should not require the
    #: client to hold every patient in the clinic just to render a name.
    patient_name: str
    patient_mrn: str
    doctor_id: str
    date: str
    time: str
    duration_min: int
    reason: str
    status: AppointmentStatus
    created_via: AppointmentChannel

    @classmethod
    def of(cls, a) -> AppointmentOut:
        return cls(
            id=str(a.id), tenant_id=str(a.tenant_id), patient_id=str(a.patient_id),
            patient_name=a.patient.full_name, patient_mrn=a.patient.mrn,
            doctor_id=str(a.doctor_id), date=iso_date(a.date) or "", time=a.time,
            duration_min=a.duration_min, reason=a.reason, status=a.status, created_via=a.created_via,
        )


class AppointmentIn(InputModel):
    patient_id: str
    doctor_id: str
    date: date_t
    time: str = Field(pattern=_HHMM)
    duration_min: int = Field(default=30, ge=5, le=480)
    reason: str = Field(default="", max_length=240)
    created_via: AppointmentChannel = AppointmentChannel.FRONT_DESK


class AppointmentStatusIn(InputModel):
    status: AppointmentStatus


class AppointmentPageOut(ApiModel):
    """One page of the diary, and how many bookings the query matched.

    `limit=0` asks for the whole collection instead — what the day and week
    grids need, since a grid is not a page.
    """

    items: list[AppointmentOut]
    total: int
    limit: int
    offset: int


class AvailabilityQuery(InputModel):
    doctor_id: str
    date: date_t
    time: str = Field(pattern=_HHMM)
    duration_min: int = Field(default=30, ge=5, le=480)
    ignore_id: str | None = None


class AvailabilityOut(ApiModel):
    available: bool
    conflicts: list[AppointmentOut]


# --- queue ------------------------------------------------------------------


class QueueEntryOut(ApiModel):
    id: str
    tenant_id: str
    patient_id: str
    patient_name: str
    patient_mrn: str
    appointment_id: str | None
    doctor_id: str
    token_no: int
    status: QueueStatus
    checked_in_at: str
    called_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    priority: QueuePriority
    reason: str
    queue_date: str

    @classmethod
    def of(cls, q, tz: str) -> QueueEntryOut:
        return cls(
            id=str(q.id), tenant_id=str(q.tenant_id), patient_id=str(q.patient_id),
            patient_name=q.patient.full_name, patient_mrn=q.patient.mrn,
            appointment_id=str(q.appointment_id) if q.appointment_id else None,
            doctor_id=str(q.doctor_id), token_no=q.token_no, status=q.status,
            checked_in_at=clock(q.checked_in_at, tz) or "", called_at=clock(q.called_at, tz),
            started_at=clock(q.started_at, tz), completed_at=clock(q.completed_at, tz),
            priority=q.priority, reason=q.reason, queue_date=iso_date(q.queue_date) or "",
        )


class WalkInIn(InputModel):
    patient_id: str
    doctor_id: str
    reason: str = Field(default="", max_length=240)
    priority: QueuePriority = QueuePriority.ROUTINE


class QueueStatusIn(InputModel):
    status: QueueStatus


class DisplayRowOut(ApiModel):
    """The waiting-room screen. A first name and an initial, never more."""

    token_no: int
    status: QueueStatus
    display_name: str
    doctor: str
    priority: QueuePriority


class DisplayOut(ApiModel):
    tenant: str
    now_serving: list[DisplayRowOut]
    waiting: list[DisplayRowOut]


# --- encounters -------------------------------------------------------------


class EncounterOut(ApiModel):
    id: str
    tenant_id: str
    patient_id: str
    patient_name: str
    patient_mrn: str
    queue_entry_id: str | None
    doctor_id: str
    date: str
    chief_complaint: str
    notes: str
    diagnosis: str
    follow_up_days: int | None
    clinical_ext: dict[str, Any]
    status: EncounterStatus

    @classmethod
    def of(cls, e) -> EncounterOut:
        return cls(
            id=str(e.id), tenant_id=str(e.tenant_id), patient_id=str(e.patient_id),
            patient_name=e.patient.full_name, patient_mrn=e.patient.mrn,
            queue_entry_id=str(e.queue_entry_id) if e.queue_entry_id else None,
            doctor_id=str(e.doctor_id), date=iso_date(e.date) or "",
            chief_complaint=e.chief_complaint, notes=e.notes, diagnosis=e.diagnosis,
            follow_up_days=e.follow_up_days, clinical_ext=dict(e.clinical_ext or {}), status=e.status,
        )


class EncounterPageOut(ApiModel):
    """One page of the case book, and how many encounters matched."""

    items: list[EncounterOut]
    total: int
    limit: int
    offset: int


class EncounterIn(InputModel):
    patient_id: str
    queue_entry_id: str | None = None
    doctor_id: str | None = None  # defaults to the signed-in clinician
    date: date_t | None = None
    chief_complaint: str = Field(default="", max_length=1000)
    notes: str = Field(default="", max_length=20_000)
    diagnosis: str = Field(default="", max_length=1000)
    follow_up_days: int | None = Field(default=None, ge=0, le=3650)
    clinical_ext: dict[str, Any] = Field(default_factory=dict)
    sign: bool = False


class EncounterPatch(InputModel):
    chief_complaint: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=20_000)
    diagnosis: str | None = Field(default=None, max_length=1000)
    follow_up_days: int | None = Field(default=None, ge=0, le=3650)
    clinical_ext: dict[str, Any] | None = None
    sign: bool | None = None


# --- prescriptions ----------------------------------------------------------


class PrescriptionItemOut(ApiModel):
    id: str
    prescription_id: str
    item_id: str | None
    medicine: str
    strength: str
    dosage: str
    frequency: str
    duration: str
    instructions: str | None = None
    qty: int
    dispensed_qty: int
    status: DispenseStatus

    @classmethod
    def of(cls, i) -> PrescriptionItemOut:
        return cls(
            id=str(i.id), prescription_id=str(i.prescription_id),
            item_id=str(i.item_id) if i.item_id else None, medicine=i.medicine, strength=i.strength,
            dosage=i.dosage, frequency=i.frequency, duration=i.duration, instructions=i.instructions,
            qty=i.qty, dispensed_qty=i.dispensed_qty, status=i.status,
        )


class PrescriptionOut(ApiModel):
    id: str
    tenant_id: str
    encounter_id: str
    patient_id: str
    patient_name: str
    patient_mrn: str
    doctor_id: str
    created_at: str
    status: DispenseStatus
    items: list[PrescriptionItemOut]

    @classmethod
    def of(cls, rx, tz: str) -> PrescriptionOut:
        return cls(
            id=str(rx.id), tenant_id=str(rx.tenant_id), encounter_id=str(rx.encounter_id),
            patient_id=str(rx.patient_id), patient_name=rx.patient.full_name,
            patient_mrn=rx.patient.mrn, doctor_id=str(rx.doctor_id),
            created_at=stamp(rx.issued_at, tz) or "", status=rx.status,
            items=[PrescriptionItemOut.of(i) for i in rx.items],
        )


class PrescriptionPageOut(ApiModel):
    """One page of prescriptions, the match count, and the tab counts.

    `counts` is keyed by dispense status and is taken before the status filter,
    so a tab can say how many are pending while another tab is on screen.
    """

    items: list[PrescriptionOut]
    total: int
    limit: int
    offset: int
    counts: dict[str, int]


class PrescriptionItemIn(InputModel):
    medicine: str = Field(min_length=1, max_length=200)
    strength: str = Field(default="", max_length=60)
    dosage: str = Field(default="", max_length=120)
    frequency: str = Field(default="", max_length=120)
    duration: str = Field(default="", max_length=80)
    instructions: str | None = Field(default=None, max_length=1000)
    qty: int = Field(default=1, ge=1, le=10_000)
    item_id: str | None = None


class PrescriptionIn(InputModel):
    items: list[PrescriptionItemIn] = Field(min_length=1, max_length=50)


class DispenseLineIn(InputModel):
    item_id: str  # the prescription item, not the stock item
    qty: int = Field(ge=1, le=10_000)
    batch_no: str = Field(min_length=1, max_length=60)


class DispenseIn(InputModel):
    lines: list[DispenseLineIn] = Field(min_length=1, max_length=50)


# --- lab --------------------------------------------------------------------


class LabResultLineIn(InputModel):
    analyte: str = Field(min_length=1, max_length=120)
    value: str = Field(default="", max_length=120)
    unit: str = Field(default="", max_length=40)
    reference: str = Field(default="", max_length=120)
    flag: ResultFlag = ResultFlag.NORMAL


class LabOrderOut(ApiModel):
    id: str
    tenant_id: str
    encounter_id: str
    patient_id: str
    patient_name: str
    patient_mrn: str
    ordered_by: str
    test_code: str
    test_name: str
    panel: str
    status: LabOrderStatus
    ordered_at: str
    resulted_at: str | None = None
    verified_at: str | None = None
    verified_by: str | None = None
    results: list[dict[str, Any]]
    note: str | None = None

    @classmethod
    def of(cls, order, tz: str, *, redact_unverified: bool = False) -> LabOrderOut:
        results = list(order.results or [])
        if redact_unverified and order.status != LabOrderStatus.VERIFIED:
            results = []
        return cls(
            id=str(order.id), tenant_id=str(order.tenant_id), encounter_id=str(order.encounter_id),
            patient_id=str(order.patient_id), patient_name=order.patient.full_name,
            patient_mrn=order.patient.mrn, ordered_by=str(order.ordered_by),
            test_code=order.test_code, test_name=order.test_name, panel=order.panel,
            status=order.status, ordered_at=stamp(order.ordered_at, tz) or "",
            resulted_at=stamp(order.resulted_at, tz), verified_at=stamp(order.verified_at, tz),
            verified_by=str(order.verified_by) if order.verified_by else None,
            results=results, note=order.note,
        )


class LabOrderPageOut(ApiModel):
    """One page of the order book, and how many orders matched."""

    items: list[LabOrderOut]
    total: int
    limit: int
    offset: int


class LabTestIn(InputModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(default="", max_length=160)
    panel: str = Field(default="", max_length=80)
    note: str | None = Field(default=None, max_length=1000)


class LabOrderIn(InputModel):
    tests: list[LabTestIn] = Field(min_length=1, max_length=30)


class LabResultsIn(InputModel):
    results: list[LabResultLineIn] = Field(min_length=1, max_length=100)


class LabVerifyIn(InputModel):
    status: Literal["verified"] = "verified"


# --- history ----------------------------------------------------------------


class HistoryEventOut(ApiModel):
    id: str
    kind: str
    at: str
    title: str
    detail: str
    meta: str | None = None
    href: str | None = None
    tone: str | None = None
