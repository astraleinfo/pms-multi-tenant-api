"""RBAC — step 4 of the middleware stack.

Permissions are named after what they let someone do, and each role gets an
explicit set. Route handlers ask for a permission, never for a role, so adding
a role is a change in one table rather than a sweep through the routers.
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
            Permission.USER_READ,
            Permission.TENANT_CONFIG_READ,
            Permission.ANALYTICS_READ,
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
