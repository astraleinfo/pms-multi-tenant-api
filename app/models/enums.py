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


class SubscriptionInvoiceStatus(StrEnum):
    """A clinic's monthly bill from the platform — the "app rent"."""

    OPEN = "open"
    PAID = "paid"
    VOID = "void"


class RentReminder(StrEnum):
    """How far along the reminder ladder a bill has got. Each rung is emailed
    once, in this order; `STAGE_ORDER` is what "further along" means."""

    NONE = "none"
    ISSUED = "issued"
    DUE_SOON = "due_soon"
    DUE = "due"
    OVERDUE = "overdue"
    FINAL = "final"
    BLOCKED = "blocked"


RENT_STAGE_ORDER: dict[str, int] = {stage.value: i for i, stage in enumerate(RentReminder)}


class TenantFeatureStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    TRIAL = "trial"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    INVITED = "invited"
    LOCKED = "locked"


class PlatformRole(StrEnum):
    """What a platform account may do in the console.

    Clinic roles live in `app.core.permissions`; this is the other side of the
    seam. `super_admin` is the whole console, including the parts that look at
    the platform's own machinery — background jobs, the workers running them,
    the broker behind both. `support` is the desk: clinics, their requests, and
    nothing underneath them.

    Existing accounts were all of the first kind, so the column defaults to it;
    narrowing somebody to `support` is a deliberate act.
    """

    SUPER_ADMIN = "super_admin"
    SUPPORT = "support"


class JobStatus(StrEnum):
    """The buckets the console counts background jobs in.

    Celery has more states than this and Flower reports every one of them;
    these are the ones an operator actually asks about. `pending` folds
    PENDING and RECEIVED together — from the console's point of view both mean
    "queued, not started" — and `revoked` is kept separate from `failed`
    because a job somebody cancelled is not a job that went wrong.
    """

    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRIED = "retried"
    REVOKED = "revoked"


#: Celery's task states, mapped onto the six above. A state not listed here is
#: reported as `pending`, which is what an unrecognised state means in practice:
#: the broker has it and nothing has finished it.
CELERY_STATE_STATUS: dict[str, JobStatus] = {
    "PENDING": JobStatus.PENDING,
    "RECEIVED": JobStatus.PENDING,
    "STARTED": JobStatus.ACTIVE,
    "SUCCESS": JobStatus.COMPLETED,
    "FAILURE": JobStatus.FAILED,
    "REJECTED": JobStatus.FAILED,
    "RETRY": JobStatus.RETRIED,
    "REVOKED": JobStatus.REVOKED,
    "IGNORED": JobStatus.COMPLETED,
}


class AuthTokenPurpose(StrEnum):
    """Why a one-time link exists.

    An invitation is the first password an account ever has; a reset replaces
    one that already exists. They are told apart because the wording, the
    lifetime, and what a spent one leaves behind all differ.
    """

    INVITE = "invite"
    RESET = "reset"


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


class PricingMode(StrEnum):
    """How a stock item's selling price is arrived at (§3.11).

    The three ways a pharmacy actually quotes a price: type the figure, add a
    margin to what the box cost, or sell at the printed MRP (optionally less a
    discount). Whichever is chosen, `unit_price` still holds the answer — the
    mode records *why* it is that number, so a later cost or MRP change can be
    re-applied without anyone re-deriving it by hand.
    """

    PRICE = "price"
    PERCENT = "percent"
    MRP = "mrp"


class ServiceKind(StrEnum):
    """What a tenant-set price is for. Lab prices are keyed by test code, so a
    scan can carry its own amount (§3.10)."""

    CONSULTATION = "consultation"
    PROCEDURE = "procedure"
    LAB = "lab"
    OTHER = "other"


class BillingDepartment(StrEnum):
    """Which counter an invoice belongs to.

    Lab and pharmacy bill separately from the clinic: one visit can produce
    three invoices, each owned by the desk that raised it.
    """

    CLINIC = "clinic"
    LAB = "lab"
    PHARMACY = "pharmacy"


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


#: Which counter a line belongs to. A department is never guessed from a
#: description — it follows the source the line was created with.
DEPARTMENT_OF_SOURCE: dict[LineItemSource, BillingDepartment] = {
    LineItemSource.CONSULTATION: BillingDepartment.CLINIC,
    LineItemSource.PROCEDURE: BillingDepartment.CLINIC,
    LineItemSource.MANUAL: BillingDepartment.CLINIC,
    LineItemSource.LAB: BillingDepartment.LAB,
    LineItemSource.PHARMACY: BillingDepartment.PHARMACY,
}


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


class MessageChannel(StrEnum):
    SMS = "sms"
    WHATSAPP = "whatsapp"


class MessageTemplate(StrEnum):
    BOOKING_CONFIRMATION = "booking_confirmation"
    APPOINTMENT_RESCHEDULED = "appointment_rescheduled"
    APPOINTMENT_CANCELLED = "appointment_cancelled"
    APPOINTMENT_REMINDER = "appointment_reminder"
    FOLLOW_UP = "follow_up"
    CUSTOM = "custom"


class MessageStatus(StrEnum):
    """`queued` is the only state the worker picks up. `failed` is terminal
    until someone retries it; `cancelled` is what a scrapped appointment does
    to a reminder that had not gone out yet."""

    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MessageProvider(StrEnum):
    """Adding one is a new adapter plus a member here — nothing else changes."""

    CONSOLE = "console"
    TWILIO = "twilio"


class SupportRequestKind(StrEnum):
    """What a clinic is asking the support desk for. `activation` and
    `walkthrough` are about one module; `general` is anything else."""

    ACTIVATION = "activation"
    WALKTHROUGH = "walkthrough"
    GENERAL = "general"


class SupportRequestStatus(StrEnum):
    """Whether the desk has been told. `queued` is the only state the worker
    picks up; `failed` is parked after the last attempt, and the request itself
    still stands — the row is the ask, the email is only how the desk hears."""

    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
