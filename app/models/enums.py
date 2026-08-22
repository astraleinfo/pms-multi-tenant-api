"""Every enumerated value the API speaks. The strings match the web app's
TypeScript unions exactly, so the two halves cannot drift apart silently."""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.value


class TenantStatus(StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class TenantFeatureStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    TRIAL = "trial"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    INVITED = "invited"
    LOCKED = "locked"


class FieldScope(StrEnum):
    COMMON = "common"
    CLINICAL_TYPE = "clinical_type"
    TENANT = "tenant"


class FieldEntity(StrEnum):
    PATIENT = "patient"
    ENCOUNTER = "encounter"


class FieldType(StrEnum):
    TEXT = "text"
    TEXTAREA = "textarea"
    NUMBER = "number"
    DATE = "date"
    SELECT = "select"
    MULTISELECT = "multiselect"
    BOOLEAN = "boolean"
    PHONE = "phone"
    EMAIL = "email"


class Sex(StrEnum):
    FEMALE = "female"
    MALE = "male"
    OTHER = "other"


class AppointmentStatus(StrEnum):
    SCHEDULED = "scheduled"
    CHECKED_IN = "checked_in"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"
    COMPLETED = "completed"


class AppointmentChannel(StrEnum):
    FRONT_DESK = "front_desk"
    ONLINE = "online"
    PHONE = "phone"


class QueueStatus(StrEnum):
    WAITING = "waiting"
    CALLED = "called"
    IN_CONSULTATION = "in_consultation"
    COMPLETED = "completed"
    NO_SHOW = "no_show"
    CANCELLED = "cancelled"


class QueuePriority(StrEnum):
    ROUTINE = "routine"
    URGENT = "urgent"


class EncounterStatus(StrEnum):
    DRAFT = "draft"
    SIGNED = "signed"


class DispenseStatus(StrEnum):
    PENDING = "pending"
    PARTIALLY_DISPENSED = "partially_dispensed"
    DISPENSED = "dispensed"


class LabOrderStatus(StrEnum):
    ORDERED = "ordered"
    PENDING = "pending"
    RESULTED = "resulted"
    VERIFIED = "verified"


class InventoryTxType(StrEnum):
    PURCHASE = "purchase"
    DISPENSE = "dispense"
    RETURN = "return"
    ADJUSTMENT = "adjustment"
    EXPIRY = "expiry"
    TRANSFER = "transfer"


class InventoryRefType(StrEnum):
    PRESCRIPTION = "prescription"
    PURCHASE_ORDER = "purchase_order"
    MANUAL = "manual"
    LAB_ORDER = "lab_order"


class InventoryCategory(StrEnum):
    MEDICINE = "medicine"
    CONSUMABLE = "consumable"
    IMPLANT = "implant"
    LENS = "lens"
    REAGENT = "reagent"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    ISSUED = "issued"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    VOID = "void"


class LineItemSource(StrEnum):
    CONSULTATION = "consultation"
    PROCEDURE = "procedure"
    PHARMACY = "pharmacy"
    LAB = "lab"
    MANUAL = "manual"


class PaymentMethod(StrEnum):
    CASH = "cash"
    CARD = "card"
    UPI = "upi"
    INSURANCE = "insurance"


class Currency(StrEnum):
    GBP = "GBP"
    EUR = "EUR"
    USD = "USD"
    INR = "INR"


class ResultFlag(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
    LOW = "low"


class SubjectKind(StrEnum):
    USER = "user"
    PLATFORM = "platform"


# --- state machines --------------------------------------------------------
# A transition that is not listed here cannot happen, whoever asks for it.

APPOINTMENT_TRANSITIONS: dict[AppointmentStatus, set[AppointmentStatus]] = {
    AppointmentStatus.SCHEDULED: {
        AppointmentStatus.CHECKED_IN,
        AppointmentStatus.CANCELLED,
        AppointmentStatus.NO_SHOW,
    },
    AppointmentStatus.CHECKED_IN: {AppointmentStatus.COMPLETED, AppointmentStatus.CANCELLED},
    AppointmentStatus.CANCELLED: set(),
    AppointmentStatus.NO_SHOW: set(),
    AppointmentStatus.COMPLETED: set(),
}

QUEUE_TRANSITIONS: dict[QueueStatus, set[QueueStatus]] = {
    QueueStatus.WAITING: {QueueStatus.CALLED, QueueStatus.NO_SHOW, QueueStatus.CANCELLED},
    QueueStatus.CALLED: {
        QueueStatus.IN_CONSULTATION,
        QueueStatus.WAITING,
        QueueStatus.NO_SHOW,
        QueueStatus.CANCELLED,
    },
    QueueStatus.IN_CONSULTATION: {QueueStatus.COMPLETED, QueueStatus.CANCELLED},
    QueueStatus.COMPLETED: set(),
    QueueStatus.NO_SHOW: set(),
    QueueStatus.CANCELLED: set(),
}

LAB_TRANSITIONS: dict[LabOrderStatus, set[LabOrderStatus]] = {
    LabOrderStatus.ORDERED: {LabOrderStatus.PENDING},
    LabOrderStatus.PENDING: {LabOrderStatus.RESULTED},
    LabOrderStatus.RESULTED: {LabOrderStatus.VERIFIED, LabOrderStatus.PENDING},
    LabOrderStatus.VERIFIED: set(),
}

INVOICE_TRANSITIONS: dict[InvoiceStatus, set[InvoiceStatus]] = {
    InvoiceStatus.DRAFT: {InvoiceStatus.ISSUED, InvoiceStatus.VOID},
    InvoiceStatus.ISSUED: {InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID, InvoiceStatus.VOID},
    InvoiceStatus.PARTIALLY_PAID: {InvoiceStatus.PAID, InvoiceStatus.VOID},
    InvoiceStatus.PAID: {InvoiceStatus.VOID},
    InvoiceStatus.VOID: set(),
}
