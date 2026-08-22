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
from app.models.identity import LoginAttempt, PlatformUser, RefreshToken, User  # noqa: F401
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
    "Appointment", "AuditEvent", "ClinicalType", "Encounter", "Feature", "FieldDefinition",
    "InventoryBatch", "InventoryItem", "InventoryTransaction", "Invoice", "InvoiceLineItem",
    "LabOrder", "LabTest", "LoginAttempt", "MedicineSuggestion", "Patient", "Payment",
    "PlatformUser", "Prescription", "PrescriptionItem", "QueueEntry", "RefreshToken", "Tenant",
    "TenantConfig", "TenantFeature", "User",
]
