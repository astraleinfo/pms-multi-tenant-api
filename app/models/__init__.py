"""Import every model so `Base.metadata` is complete for Alembic and the guard."""

from app.models.clinical import (  # noqa: F401
    Appointment,
    Encounter,
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
    "Appointment", "AuditEvent", "AuthToken", "ClinicalType", "Encounter", "Feature", "FieldDefinition",
    "InventoryBatch", "InventoryItem", "InventoryTransaction", "Invoice", "InvoiceLineItem",
    "LabOrder", "LabTest", "LoginAttempt", "MedicineSuggestion", "Message", "MessagingConfig",
    "Patient", "Payment",
    "PlatformUser", "Prescription", "PrescriptionItem", "QueueEntry", "RefreshToken", "Tenant",
    "TenantConfig", "TenantFeature", "User",
]
