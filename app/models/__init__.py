"""Import every model so `Base.metadata` is complete for Alembic and the guard."""

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
from app.models.imports import ImportJob  # noqa: F401
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
from app.models.rent import SubscriptionInvoice  # noqa: F401
from app.models.support import SupportRequest  # noqa: F401
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
    "Appointment", "AuditEvent", "AuthToken", "ClinicalType", "Encounter", "Feature", "FieldDefinition",
    "InventoryBatch", "InventoryItem", "InventoryTransaction", "Invoice", "InvoiceLineItem",
    "LabAttachment", "LabOrder", "LabTest", "LoginAttempt", "MedicineSuggestion", "Message",
    "MessagingConfig", "Patient", "Payment",
    "PlatformUser", "Prescription", "PrescriptionItem", "QueueEntry", "RefreshToken",
    "ServicePrice", "SubscriptionInvoice", "SupportRequest", "Tenant",
    "TenantConfig", "TenantFeature", "User",
]
