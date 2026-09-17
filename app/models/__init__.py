"""Import every model so `Base.metadata` is complete for Alembic and the guard."""

from app.models.billing import (  # noqa: F401
    BillingAccount,
    BillingCycle,
    Plan,
    PlanFeature,
    Subscription,
    SubscriptionInvoice,
    SubscriptionInvoiceItem,
    SubscriptionPayment,
    SubscriptionRefund,
    TaxRate,
    WebhookEvent,
)
from app.models.clinical import (  # noqa: F401
    Appointment,
    Encounter,
    LabAttachment,
    LabOrder,
    Patient,
    Prescription,
    PrescriptionItem,
    QueueEntry,
)
from app.models.identity import (  # noqa: F401
    AuthToken,
    LoginAttempt,
    PlatformUser,
    RefreshToken,
    User,
)
from app.models.messaging import Message, MessagingConfig  # noqa: F401
from app.models.ops import (  # noqa: F401
    InventoryBatch,
    InventoryItem,
    InventoryTransaction,
    Invoice,
    InvoiceLineItem,
    Payment,
    ServicePrice,
)
from app.models.tenancy import (  # noqa: F401
    AuditEvent,
    ClinicalType,
    Feature,
    FieldDefinition,
    LabTest,
    MedicineSuggestion,
    Tenant,
    TenantConfig,
    TenantFeature,
)

__all__ = [
    "Appointment", "AuditEvent", "AuthToken", "BillingAccount", "BillingCycle", "ClinicalType",
    "Encounter", "Feature", "FieldDefinition",
    "InventoryBatch", "InventoryItem", "InventoryTransaction", "Invoice", "InvoiceLineItem",
    "LabAttachment", "LabOrder", "LabTest", "LoginAttempt", "MedicineSuggestion", "Message",
    "MessagingConfig", "Patient", "Payment", "Plan", "PlanFeature",
    "PlatformUser", "Prescription", "PrescriptionItem", "QueueEntry", "RefreshToken",
    "ServicePrice", "Subscription", "SubscriptionInvoice", "SubscriptionInvoiceItem",
    "SubscriptionPayment", "SubscriptionRefund", "TaxRate", "Tenant",
    "TenantConfig", "TenantFeature", "User", "WebhookEvent",
]
