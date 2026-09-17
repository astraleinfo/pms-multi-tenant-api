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


# --- platform billing (subscriptions, add-ons, invoices) --------------------
#
# This is the platform billing a *tenant* for the product, which is a different
# ledger from `InvoiceStatus` above — that one is a clinic billing its patients.
# The two never share a table, an enum, or a service.


class PlanInterval(StrEnum):
    """How often a plan bills. Monthly is the only cycle the product sells
    today; the column exists so adding a yearly plan is data, not a migration
    of every invoice that already quotes one."""

    MONTHLY = "monthly"
    YEARLY = "yearly"


class SubscriptionStatus(StrEnum):
    """The lifecycle of a tenant's subscription.

    `pending` is a subscription created but not yet paid for — the gateway has
    an authorisation to collect and has not collected. `past_due` is a renewal
    whose invoice went unpaid; the clinic keeps working through the grace
    period, and suspension is a separate, deliberate act by the platform.
    """

    PENDING = "pending"
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class BillingCycleStatus(StrEnum):
    """A cycle is `open` while it is the one being lived in, `closed` once its
    invoice has been generated, and `superseded` when a plan change ended it
    early."""

    OPEN = "open"
    CLOSED = "closed"
    SUPERSEDED = "superseded"


class SubscriptionInvoiceStatus(StrEnum):
    DRAFT = "draft"
    ISSUED = "issued"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    VOID = "void"
    UNCOLLECTIBLE = "uncollectible"


class InvoiceItemKind(StrEnum):
    """What a line on a subscription invoice is.

    The order here is the order the requirement states the sum in: base plan,
    add-ons, proration, then discounts and credits, then tax. `position` on the
    row is what actually orders them; this is what each one means.
    """

    BASE_PLAN = "base_plan"
    ADDON = "addon"
    PRORATION = "proration"
    DISCOUNT = "discount"
    CREDIT = "credit"
    TAX = "tax"


class PaymentGateway(StrEnum):
    """Adding one is a new adapter package plus a member here. The billing
    engine never names a member of this enum in an arithmetic path."""

    RAZORPAY = "razorpay"
    MANUAL = "manual"


class SubscriptionPaymentStatus(StrEnum):
    """What the *gateway* says, never what a browser says.

    `created` and `authorised` are money that has not arrived. Only `captured`
    counts as paid, and only a webhook or a server-side fetch can set it.
    """

    CREATED = "created"
    AUTHORISED = "authorised"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class RefundStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"


class WebhookStatus(StrEnum):
    """Every webhook is stored before it is interpreted, so `received` is the
    state of a delivery nobody has processed yet — including one whose
    signature failed, which is stored as `rejected` and never processed."""

    RECEIVED = "received"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"
    REJECTED = "rejected"


class TaxScope(StrEnum):
    """Where a tax rate applies. `intra_state` and `inter_state` are what an
    Indian deployment splits CGST/SGST from IGST on; a deployment that does not
    care uses `any` and never sees the distinction."""

    ANY = "any"
    INTRA_STATE = "intra_state"
    INTER_STATE = "inter_state"


SUBSCRIPTION_TRANSITIONS: dict[SubscriptionStatus, set[SubscriptionStatus]] = {
    SubscriptionStatus.PENDING: {
        SubscriptionStatus.TRIALING,
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.CANCELLED,
        SubscriptionStatus.EXPIRED,
    },
    SubscriptionStatus.TRIALING: {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.PAUSED,
        SubscriptionStatus.CANCELLED,
        SubscriptionStatus.EXPIRED,
    },
    SubscriptionStatus.ACTIVE: {
        SubscriptionStatus.PAST_DUE,
        SubscriptionStatus.PAUSED,
        SubscriptionStatus.CANCELLED,
    },
    SubscriptionStatus.PAST_DUE: {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.PAUSED,
        SubscriptionStatus.CANCELLED,
        SubscriptionStatus.EXPIRED,
    },
    SubscriptionStatus.PAUSED: {SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELLED},
    SubscriptionStatus.CANCELLED: set(),
    SubscriptionStatus.EXPIRED: set(),
}

SUBSCRIPTION_INVOICE_TRANSITIONS: dict[SubscriptionInvoiceStatus, set[SubscriptionInvoiceStatus]] = {
    SubscriptionInvoiceStatus.DRAFT: {
        SubscriptionInvoiceStatus.ISSUED,
        SubscriptionInvoiceStatus.VOID,
    },
    SubscriptionInvoiceStatus.ISSUED: {
        SubscriptionInvoiceStatus.PARTIALLY_PAID,
        SubscriptionInvoiceStatus.PAID,
        SubscriptionInvoiceStatus.VOID,
        SubscriptionInvoiceStatus.UNCOLLECTIBLE,
    },
    SubscriptionInvoiceStatus.PARTIALLY_PAID: {
        SubscriptionInvoiceStatus.PAID,
        SubscriptionInvoiceStatus.VOID,
        SubscriptionInvoiceStatus.UNCOLLECTIBLE,
    },
    SubscriptionInvoiceStatus.PAID: {SubscriptionInvoiceStatus.VOID},
    SubscriptionInvoiceStatus.UNCOLLECTIBLE: {
        SubscriptionInvoiceStatus.PAID,
        SubscriptionInvoiceStatus.VOID,
    },
    SubscriptionInvoiceStatus.VOID: set(),
}
