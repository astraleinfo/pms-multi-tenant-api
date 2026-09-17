"""RBAC — step 4 of the middleware stack.

Permissions are named after what they let someone do, and each role gets an
explicit set. Route handlers ask for a permission, never for a role, so adding
a role is a change in one table rather than a sweep through the routers.

Money is the one place the table looks wider than it reads: every clinical role
can bill and settle, because in this product the counter takes the money. The
pharmacist asks for it as the medicine is handed over, the lab tech as the
report is collected, the clinician for the consultation. Which counter is being
settled is chosen on the screen, and the ledger keeps the three apart.

The clinic's *own* subscription is the exception, and the narrowest thing in
this table: `SUBSCRIPTION_READ` and `SUBSCRIPTION_MANAGE` belong to the tenant
administrator alone. Everyone else can take a patient's money; only the person
who runs the practice can change what the practice itself pays for.
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    TENANT_ADMIN = "tenant_admin"
    RECEPTIONIST = "receptionist"
    DOCTOR = "doctor"
    PHARMACIST = "pharmacist"
    LAB_TECH = "lab_tech"


class Permission(str, Enum):
    # practice administration
    TENANT_CONFIG_READ = "tenant:config:read"
    TENANT_CONFIG_WRITE = "tenant:config:write"
    TENANT_FEATURE_MANAGE = "tenant:feature:manage"
    TENANT_FIELD_MANAGE = "tenant:field:manage"
    USER_READ = "user:read"
    USER_MANAGE = "user:manage"

    # clinical core
    PATIENT_READ = "patient:read"
    PATIENT_WRITE = "patient:write"
    APPOINTMENT_READ = "appointment:read"
    APPOINTMENT_WRITE = "appointment:write"
    QUEUE_READ = "queue:read"
    QUEUE_WRITE = "queue:write"
    ENCOUNTER_READ = "encounter:read"
    ENCOUNTER_WRITE = "encounter:write"
    PRESCRIPTION_READ = "prescription:read"
    PRESCRIPTION_WRITE = "prescription:write"

    # purchasable modules
    PHARMACY_READ = "pharmacy:read"
    PHARMACY_DISPENSE = "pharmacy:dispense"
    LAB_ORDER = "lab:order"
    LAB_READ = "lab:read"
    LAB_RESULT = "lab:result"
    LAB_VERIFY = "lab:verify"
    INVENTORY_READ = "inventory:read"
    INVENTORY_WRITE = "inventory:write"
    BILLING_READ = "billing:read"
    BILLING_WRITE = "billing:write"

    ANALYTICS_READ = "analytics:read"

    # What the clinic pays the platform. Separate from `BILLING_*`, which is
    # what the clinic charges its patients — the two ledgers are different
    # money and different people, and a receptionist who settles a patient's
    # bill has no business changing the clinic's own subscription.
    SUBSCRIPTION_READ = "subscription:read"
    SUBSCRIPTION_MANAGE = "subscription:manage"

    # Patient messaging. Reading history and sending are separate so a role can
    # see what went out without being able to send anything new.
    MESSAGE_READ = "message:read"
    MESSAGE_SEND = "message:send"


_CLINICAL_READ = {
    Permission.PATIENT_READ,
    Permission.APPOINTMENT_READ,
    Permission.QUEUE_READ,
    Permission.ENCOUNTER_READ,
    Permission.PRESCRIPTION_READ,
}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.TENANT_ADMIN: frozenset(Permission),  # every permission inside their own clinic
    Role.RECEPTIONIST: frozenset(
        {
            Permission.PATIENT_READ,
            Permission.PATIENT_WRITE,
            Permission.APPOINTMENT_READ,
            Permission.APPOINTMENT_WRITE,
            Permission.QUEUE_READ,
            Permission.QUEUE_WRITE,
            Permission.ENCOUNTER_READ,
            Permission.PRESCRIPTION_READ,
            Permission.BILLING_READ,
            Permission.BILLING_WRITE,
            Permission.USER_READ,
            Permission.TENANT_CONFIG_READ,
            Permission.ANALYTICS_READ,
            Permission.MESSAGE_READ,
            Permission.MESSAGE_SEND,
        }
    ),
    Role.DOCTOR: frozenset(
        _CLINICAL_READ
        | {
            Permission.PATIENT_WRITE,
            Permission.APPOINTMENT_WRITE,
            Permission.QUEUE_WRITE,
            Permission.ENCOUNTER_WRITE,
            Permission.PRESCRIPTION_WRITE,
            Permission.LAB_ORDER,
            Permission.LAB_READ,
            Permission.PHARMACY_READ,
            Permission.INVENTORY_READ,
            Permission.BILLING_READ,
            Permission.BILLING_WRITE,
            Permission.USER_READ,
            Permission.TENANT_CONFIG_READ,
            Permission.ANALYTICS_READ,
            Permission.MESSAGE_READ,
            Permission.MESSAGE_SEND,
        }
    ),
    Role.PHARMACIST: frozenset(
        {
            Permission.PATIENT_READ,
            Permission.PRESCRIPTION_READ,
            Permission.PHARMACY_READ,
            Permission.PHARMACY_DISPENSE,
            Permission.INVENTORY_READ,
            Permission.INVENTORY_WRITE,
            Permission.BILLING_READ,
            Permission.BILLING_WRITE,
            Permission.QUEUE_READ,
            Permission.USER_READ,
            Permission.TENANT_CONFIG_READ,
            Permission.ANALYTICS_READ,
        }
    ),
    Role.LAB_TECH: frozenset(
        {
            Permission.PATIENT_READ,
            Permission.LAB_READ,
            Permission.LAB_RESULT,
            Permission.LAB_VERIFY,
            Permission.BILLING_READ,
            Permission.BILLING_WRITE,
            Permission.QUEUE_READ,
            Permission.INVENTORY_READ,
            Permission.USER_READ,
            Permission.TENANT_CONFIG_READ,
            Permission.ANALYTICS_READ,
        }
    ),
}


def role_has(role: str, permission: Permission) -> bool:
    try:
        return permission in ROLE_PERMISSIONS[Role(role)]
    except ValueError:
        return False


def permissions_for(role: str) -> list[str]:
    try:
        return sorted(p.value for p in ROLE_PERMISSIONS[Role(role)])
    except ValueError:
        return []
