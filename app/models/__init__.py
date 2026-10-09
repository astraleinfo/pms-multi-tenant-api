"""Import every model so `Base.metadata` is complete for Alembic and the guard."""

from app.models.clinical import (  # noqa: F401
    Appointment,
    Encounter,
    FollowUpCall,
    LabAttachment,
    LabOrder,
    Patient,
    PatientNote,
    Prescription,
    PrescriptionItem,
    QueueEntry,
)
from app.models.consent import ConsentForm, ConsentFormVersion, PatientConsent  # noqa: F401
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
    FieldDefinition,
    LabTest,
    MedicineSuggestion,
    Tenant,
    TenantConfig,
)

__all__ = [
    "Appointment", "AuditEvent", "AuthToken", "ClinicalType",
    "ConsentForm", "ConsentFormVersion", "Encounter", "FieldDefinition", "FollowUpCall",
    "InventoryBatch", "InventoryItem", "InventoryTransaction", "Invoice", "InvoiceLineItem",
    "LabAttachment", "LabOrder", "LabTest", "LoginAttempt", "MedicineSuggestion", "Message",
    "MessagingConfig", "Patient", "PatientConsent", "PatientNote", "Payment",
    "PlatformUser", "Prescription", "PrescriptionItem", "QueueEntry", "RefreshToken",
    "ServicePrice", "SubscriptionInvoice", "SupportRequest", "Tenant",
    "TenantConfig", "User",
]
