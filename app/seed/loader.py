"""Load the demo dataset.

The rows are the ones the web app's mock database ships with, so the API can be
pointed at the existing front end and everything lines up: the same clinics, the
same patients, the same tokens. The short ids (`p1`, `u3`) become UUIDs through
a deterministic mapping, so re-seeding is stable and nothing in the data leaks a
row count.

Passwords in the source file are the demo strings the web app documents; they
are hashed with Argon2 on the way in and the plaintext is never stored.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from datetime import time as time_t
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.session import set_tenant
from app.models.billing import Plan, PlanFeature, TaxRate
from app.models.clinical import (
    Appointment,
    Encounter,
    LabOrder,
    Patient,
    Prescription,
    PrescriptionItem,
    QueueEntry,
)
from app.models.identity import PlatformUser, User
from app.models.ops import (
    InventoryBatch,
    InventoryItem,
    InventoryTransaction,
    Invoice,
    InvoiceLineItem,
    Payment,
    ServicePrice,
)
from app.models.tenancy import (
    ClinicalType,
    Feature,
    FieldDefinition,
    LabTest,
    MedicineSuggestion,
    Tenant,
    TenantConfig,
    TenantFeature,
)
from app.schemas.common import zone
from app.seed.history import seed_history

DATA_FILE = Path(__file__).with_name("seed_data.json")
NAMESPACE = uuid.UUID("5f0f4d3a-6a6f-4c1e-9d2f-0f3c7b1a9e10")

TZ = "Europe/London"

# The web app pins "today" so its server and client render identically. Here the
# whole dataset is shifted so that pinned day lands on the real one: the demo
# clinic has a queue this morning whenever you happen to run it, and every date
# keeps its distance from every other. Dates of birth are left alone, for the
# obvious reason.
PINNED_TODAY = date(2026, 8, 19)


def _offset() -> timedelta:
    return date.today() - PINNED_TODAY


def rid(kind: str, short_id: str) -> uuid.UUID:
    """`patient` + `p1` → a stable UUID. Same input, same id, every time."""
    return uuid.uuid5(NAMESPACE, f"{kind}:{short_id}")


def load_data() -> dict[str, Any]:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def _dt(value: str | None, *, day: str | None = None) -> datetime | None:
    """`2026-08-19 09:12`, or `09:12` with the day supplied, in clinic time."""
    if not value or not value[0].isdigit():
        return None
    tz = zone(TZ)
    if len(value) == 5 and ":" in value:
        base = _d(day) or date.today()
        hour, minute = (int(p) for p in value.split(":"))
        return datetime.combine(base, time_t(hour, minute), tzinfo=tz)
    return (datetime.fromisoformat(value) + _offset()).replace(tzinfo=tz)


def _d(value: str | None) -> date | None:
    """A date from the dataset, moved onto the calendar the demo is run on."""
    if not value or not value[0].isdigit():
        return None
    return date.fromisoformat(value) + _offset()


def _birthdate(value: str | None) -> date | None:
    """Never shifted — a date of birth is a fact, not a demo prop."""
    if not value or not value[0].isdigit():
        return None
    return date.fromisoformat(value)


async def wipe(session: AsyncSession) -> None:
    """Clear everything the seeder owns.

    TRUNCATE, not DELETE: row-level security is FORCEd on the tenant tables, so
    even the owning role sees no rows without a tenant bound — and truncating
    is the one operation that is about the table rather than its rows. It needs
    ownership, which is why the seeder runs as the migration role.
    """
    tables = ", ".join(
        [
            "webhook_events", "subscription_refunds", "subscription_payments",
            "subscription_invoice_items", "subscription_invoices", "billing_cycles",
            "subscriptions", "billing_accounts", "plan_features", "plans", "tax_rates",
            "audit_events", "login_attempts", "refresh_tokens", "payments", "invoice_line_items",
            "invoices", "service_prices", "inventory_transactions", "inventory_batches",
            "prescription_items", "prescriptions", "lab_attachments", "lab_orders", "encounters",
            "queue_entries", "appointments",
            "patients", "inventory_items", "tenant_configs", "tenant_features",
            "field_definitions", "users", "tenants", "platform_users", "medicine_suggestions",
            "lab_tests", "features", "clinical_types",
        ]
    )
    await session.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))


#: The base plans this deployment sells, and the tax it charges. Seeded rather
#: than migrated, because they are data a deployment owns: a different market
#: writes different rows and changes no code. Prices are in the deployment's
#: billing currency, and add-on prices are *not* here — they fall back to
#: `features.price_monthly`, so a plan needs a `plan_features` row only where it
#: bundles or discounts a module.
BILLING_PLANS = [
    {
        "code": "starter",
        "name": "Starter",
        "description": "One clinician, the core record, and nothing to switch on.",
        "amount": 2999,
        "sort_order": 10,
        "trial_days": 14,
        "included": [],
    },
    {
        "code": "practice",
        "name": "Practice",
        "description": "A working practice: the core record with billing included.",
        "amount": 4999,
        "sort_order": 20,
        "trial_days": 14,
        "included": ["billing"],
    },
    {
        "code": "clinic",
        "name": "Clinic",
        "description": "Everything a multi-room clinic runs on, with lab and billing bundled.",
        "amount": 8999,
        "sort_order": 30,
        "trial_days": 0,
        "included": ["billing", "lab"],
    },
]

#: 18% GST, as two nine-percent components — which is what an intra-state
#: Indian supply is actually charged, and what proves the tax engine is not
#: hard-coded to one line. A deployment billing from elsewhere replaces these
#: rows and touches nothing else.
BILLING_TAX_RATES = [
    {"code": "CGST", "name": "CGST", "percent": 9, "scope": "intra_state", "country": "IN"},
    {"code": "SGST", "name": "SGST", "percent": 9, "scope": "intra_state", "country": "IN"},
    {"code": "IGST", "name": "IGST", "percent": 18, "scope": "inter_state", "country": "IN"},
]


def seed_billing_catalogue(session: AsyncSession) -> None:
    """The plans a clinic can choose from, and the tax they are charged.

    Deliberately in the seeder rather than in a migration: a migration changes
    the shape of the database and must mean the same thing everywhere, while
    what a deployment charges is its own business.
    """
    for row in BILLING_PLANS:
        plan = Plan(
            id=rid("plan", row["code"]),
            code=row["code"],
            name=row["name"],
            description=row["description"],
            amount=row["amount"],
            trial_days=row["trial_days"],
            sort_order=row["sort_order"],
        )
        session.add(plan)
        for feature_key in row["included"]:
            session.add(
                PlanFeature(
                    id=rid("plan_feature", f"{row['code']}:{feature_key}"),
                    plan_id=plan.id,
                    feature_key=feature_key,
                    included=True,
                )
            )

    for row in BILLING_TAX_RATES:
        session.add(
            TaxRate(
                id=rid("tax_rate", row["code"]),
                code=row["code"],
                name=row["name"],
                percent=row["percent"],
                scope=row["scope"],
                country=row["country"],
                # Backdated well before any invoice this deployment can raise,
                # so a demo invoice is never taxed at nothing by accident.
                effective_from=date(2020, 1, 1),
            )
        )


async def seed_platform_tables(session: AsyncSession, data: dict) -> None:
    """Everything the platform owns: catalogues, clinics, console staff, and
    the shared field definitions."""
    for row in data["CAT_CLINICAL_TYPES"]:
        session.add(
            ClinicalType(
                key=row["key"],
                label=row["label"],
                exam_label=row["examLabel"],
                blurb=row["blurb"],
                complaint_example=row["complaintExample"],
            )
        )

    for row in data["CAT_FEATURES"]:
        session.add(
            Feature(
                key=row["key"],
                name=row["name"],
                is_core=row["is_core"],
                summary=row["summary"],
                price_monthly=row["price_monthly"],
                unlocks=row["unlocks"],
            )
        )

    seed_billing_catalogue(session)

    for row in data["CAT_LAB_CATALOG"]:
        session.add(
            LabTest(
                code=row["code"],
                name=row["name"],
                panel=row["panel"],
                clinical_types=row.get("clinical_types"),
            )
        )

    for row in data["CAT_MEDICINE_SUGGESTIONS"]:
        session.add(
            MedicineSuggestion(
                medicine=row["medicine"],
                strength=row["strength"],
                dosage=row["dosage"],
                frequency=row["frequency"],
                duration=row["duration"],
                clinical_types=row.get("clinical_types"),
            )
        )

    for row in data["PLATFORM_USERS"]:
        session.add(
            PlatformUser(
                id=rid("platform_user", row["id"]),
                name=row["name"],
                email=row["email"].lower(),
                password_hash=hash_password(row["password"]),
                initials=row["initials"],
                title=row["title"],
                status="active",
            )
        )

    for row in data["TENANTS"]:
        session.add(
            Tenant(
                id=rid("tenant", row["id"]),
                name=row["name"],
                slug=row["slug"],
                clinical_type=row["clinical_type"],
                status=row["status"],
                city=row["city"],
                plan=row["plan"],
                since=_d(row["since"]),
                timezone=TZ,
            )
        )

    for row in data["CAT_FIELD_DEFINITIONS"]:
        if row["scope"] == "tenant":
            continue  # written with the tenant bound, below
        session.add(
            FieldDefinition(
                id=rid("field", row["id"]),
                scope=row["scope"],
                clinical_type=row.get("clinical_type"),
                tenant_id=None,
                entity=row["entity"],
                field_key=row["field_key"],
                label=row["label"],
                type=row["type"],
                required=row["required"],
                order=row["order"],
                options=row.get("options"),
                help=row.get("help"),
                span=row.get("span"),
                group=row.get("group"),
            )
        )

    await session.flush()


def _by_tenant(rows: list[dict], tenant_short_id: str) -> list[dict]:
    return [r for r in rows if r.get("tenant_id") == tenant_short_id]



# --- the tariff each demo clinic starts with ---------------------------------
#
# §3.12: what a clinic charges is a decision it makes, so these are per-tenant
# rows rather than numbers in the billing code. Scans differ from each other on
# purpose — a cone beam CT is not a bitewing.

CONSULTATION_PRICE = {"dental": 500, "ophthalmology": 600, "dermatology": 550}

LAB_PRICES = {
    "FBC": 350, "HBA1C": 600, "CRP": 450, "CULT": 700,
    "OPG": 900, "PA": 250, "BW": 350, "CBCT": 3500,
    "OCT": 1800, "VF": 1200, "BIO": 1500, "CORNEA": 1600,
    "PATCH": 2400, "MYC": 650, "BIOP": 2800, "DERM": 900,
}

PROCEDURE_PRICES = {
    "dental": [
        ("CROWN-ZR", "Zirconia crown", 12000),
        ("RCT-MOL", "Root canal treatment — molar", 7500),
        ("SCALE", "Scaling and polishing", 1500),
        ("EXTRACT", "Extraction — simple", 2000),
    ],
    "ophthalmology": [
        ("PHACO", "Phacoemulsification with IOL", 38000),
        ("YAG", "YAG capsulotomy", 6000),
        ("REFRACT", "Refraction and dispensing", 800),
    ],
    "dermatology": [
        ("CRYO", "Cryotherapy — per lesion", 1200),
        ("EXCISE", "Excision with histology", 6500),
        ("PEEL", "Chemical peel", 3500),
    ],
}


def _seed_service_prices(session: AsyncSession, data: dict, tenant_id, clinical_type: str) -> None:
    """One row per billable thing that is not stock: the consultation, this
    discipline's procedures, and every test it can order."""
    session.add(
        ServicePrice(
            tenant_id=tenant_id,
            kind="consultation",
            code="CONSULTATION",
            name="Consultation",
            price=CONSULTATION_PRICE.get(clinical_type, 500),
            tax_pct=0,
            active=True,
        )
    )

    for code, name, price in PROCEDURE_PRICES.get(clinical_type, []):
        session.add(
            ServicePrice(
                tenant_id=tenant_id, kind="procedure", code=code, name=name,
                price=price, tax_pct=0, active=True,
            )
        )

    for row in data["CAT_LAB_CATALOG"]:
        offered = not row.get("clinical_types") or clinical_type in row["clinical_types"]
        if not offered:
            continue
        session.add(
            ServicePrice(
                tenant_id=tenant_id,
                kind="lab",
                code=row["code"],
                name=row["name"],
                price=LAB_PRICES.get(row["code"], 500),
                tax_pct=0,
                active=True,
                note=row["panel"],
            )
        )


async def seed_tenant(session: AsyncSession, data: dict, tenant_row: dict) -> None:
    """Everything one clinic owns, written with that clinic bound to the
    session — exactly the path a request takes."""
    short = tenant_row["id"]
    tenant_id = rid("tenant", short)
    await set_tenant(session, tenant_id)

    for row in data["CAT_FIELD_DEFINITIONS"]:
        if row["scope"] != "tenant" or row["tenant_id"] != short:
            continue
        session.add(
            FieldDefinition(
                id=rid("field", row["id"]),
                scope="tenant",
                clinical_type=None,
                tenant_id=tenant_id,
                entity=row["entity"],
                field_key=row["field_key"],
                label=row["label"],
                type=row["type"],
                required=row["required"],
                order=row["order"],
                options=row.get("options"),
                help=row.get("help"),
                span=row.get("span"),
                group=row.get("group"),
            )
        )

    _seed_service_prices(session, data, tenant_id, tenant_row["clinical_type"])

    for row in _by_tenant(data["TENANT_FEATURES"], short):
        session.add(
            TenantFeature(
                tenant_id=tenant_id,
                feature_key=row["feature_key"],
                status=row["status"],
                activated_at=_d(row.get("activated_at")),
            )
        )

    config_row = next(c for c in data["TENANT_CONFIGS"] if c["tenant_id"] == short)
    patients = _by_tenant(data["PATIENTS"], short)
    invoices = _by_tenant(data["INVOICES"], short)
    session.add(
        TenantConfig(
            tenant_id=tenant_id,
            contact_phone=config_row["contact_phone"],
            contact_email=config_row["contact_email"],
            address=config_row["address"],
            mrn_prefix=config_row["mrn_prefix"],
            mrn_sequence=_highest_suffix(p["mrn"] for p in patients),
            day_start=config_row["day_start"],
            day_end=config_row["day_end"],
            slot_minutes=config_row["slot_minutes"],
            default_duration=config_row["default_duration"],
            token_prefix=config_row["token_prefix"],
            urgent_first=config_row["urgent_first"],
            invoice_prefix=config_row["invoice_prefix"],
            invoice_sequence=_highest_suffix(i["number"] for i in invoices),
            tax_pct=config_row["tax_pct"],
            payment_terms_days=config_row["payment_terms_days"],
            currency=config_row["currency"],
        )
    )

    for row in _by_tenant(data["USERS"], short):
        session.add(
            User(
                id=rid("user", row["id"]),
                tenant_id=tenant_id,
                name=row["name"],
                role=row["role"],
                email=row["email"].lower(),
                password_hash=hash_password(row["password"]) if row.get("password") else None,
                initials=row["initials"],
                speciality=row.get("speciality"),
                status=row["status"],
                failed_attempts=row["failed_attempts"],
                last_sign_in=_dt(f"{row['last_sign_in']} 08:00") if row.get("last_sign_in") else None,
            )
        )

    await session.flush()  # staff exist before anything points at them

    for row in patients:
        session.add(
            Patient(
                id=rid("patient", row["id"]),
                tenant_id=tenant_id,
                mrn=row["mrn"],
                full_name=row["full_name"],
                dob=_birthdate(row["dob"]),
                sex=row["sex"],
                phone=row["phone"],
                email=row.get("email"),
                address=row.get("address"),
                blood_group=row.get("blood_group"),
                allergies=row.get("allergies"),
                registered_at=_d(row["registered_at"]),
                last_visit=_d(row.get("last_visit")),
                clinical_ext=row.get("clinical_ext") or {},
                tags=row.get("tags") or [],
            )
        )

    await session.flush()

    for row in _by_tenant(data["APPOINTMENTS"], short):
        session.add(
            Appointment(
                id=rid("appointment", row["id"]),
                tenant_id=tenant_id,
                patient_id=rid("patient", row["patient_id"]),
                doctor_id=rid("user", row["doctor_id"]),
                date=_d(row["date"]),
                time=row["time"],
                duration_min=row["duration_min"],
                reason=row["reason"],
                status=row["status"],
                created_via=row["created_via"],
            )
        )

    await session.flush()

    today = data["CAT_TODAY"]
    for row in _by_tenant(data["QUEUE"], short):
        session.add(
            QueueEntry(
                id=rid("queue", row["id"]),
                tenant_id=tenant_id,
                patient_id=rid("patient", row["patient_id"]),
                appointment_id=(
                    rid("appointment", row["appointment_id"]) if row.get("appointment_id") else None
                ),
                doctor_id=rid("user", row["doctor_id"]),
                queue_date=_d(today),
                token_no=row["token_no"],
                status=row["status"],
                priority=row["priority"],
                reason=row["reason"],
                checked_in_at=_dt(row["checked_in_at"], day=today),
                called_at=_dt(row.get("called_at"), day=today),
                started_at=_dt(row.get("started_at"), day=today),
                completed_at=_dt(row.get("completed_at"), day=today),
            )
        )

    await session.flush()

    for row in _by_tenant(data["ENCOUNTERS"], short):
        session.add(
            Encounter(
                id=rid("encounter", row["id"]),
                tenant_id=tenant_id,
                patient_id=rid("patient", row["patient_id"]),
                queue_entry_id=rid("queue", row["queue_entry_id"]) if row.get("queue_entry_id") else None,
                doctor_id=rid("user", row["doctor_id"]),
                date=_d(row["date"]),
                chief_complaint=row["chief_complaint"],
                notes=row["notes"],
                diagnosis=row["diagnosis"],
                follow_up_days=row.get("follow_up_days"),
                clinical_ext=row.get("clinical_ext") or {},
                status=row["status"],
                signed_at=_dt(f"{row['date']} 12:00") if row["status"] == "signed" else None,
            )
        )

    await session.flush()

    for row in _by_tenant(data["INVENTORY_ITEMS"], short):
        session.add(
            InventoryItem(
                id=rid("item", row["id"]),
                tenant_id=tenant_id,
                sku=row["sku"],
                name=row["name"],
                form=row["form"],
                strength=row["strength"],
                unit=row["unit"],
                category=row["category"],
                reorder_level=row["reorder_level"],
                unit_price=row["unit_price"],
            )
        )

    await session.flush()

    item_ids = {r["id"] for r in _by_tenant(data["INVENTORY_ITEMS"], short)}
    for row in data["INVENTORY_BATCHES"]:
        if row["item_id"] not in item_ids:
            continue
        session.add(
            InventoryBatch(
                tenant_id=tenant_id,
                item_id=rid("item", row["item_id"]),
                batch_no=row["batch_no"],
                expiry=_d(row["expiry"]),
                qty=row["qty"],
            )
        )

    await session.flush()

    for row in _by_tenant(data["PRESCRIPTIONS"], short):
        prescription = Prescription(
            id=rid("prescription", row["id"]),
            tenant_id=tenant_id,
            encounter_id=rid("encounter", row["encounter_id"]),
            patient_id=rid("patient", row["patient_id"]),
            doctor_id=rid("user", row["doctor_id"]),
            issued_at=_dt(row["created_at"]),
            status=row["status"],
        )
        session.add(prescription)
        for position, item in enumerate(row["items"]):
            session.add(
                PrescriptionItem(
                    id=rid("prescription_item", item["id"]),
                    tenant_id=tenant_id,
                    prescription_id=prescription.id,
                    item_id=rid("item", item["item_id"]) if item.get("item_id") else None,
                    position=position,
                    medicine=item["medicine"],
                    strength=item["strength"],
                    dosage=item["dosage"],
                    frequency=item["frequency"],
                    duration=item["duration"],
                    instructions=item.get("instructions"),
                    qty=item["qty"],
                    dispensed_qty=item["dispensed_qty"],
                    status=item["status"],
                )
            )

    for row in _by_tenant(data["LAB_ORDERS"], short):
        session.add(
            LabOrder(
                id=rid("lab_order", row["id"]),
                tenant_id=tenant_id,
                encounter_id=rid("encounter", row["encounter_id"]),
                patient_id=rid("patient", row["patient_id"]),
                ordered_by=rid("user", row["ordered_by"]),
                test_code=row["test_code"],
                test_name=row["test_name"],
                panel=row["panel"],
                status=row["status"],
                ordered_at=_dt(row["ordered_at"]),
                resulted_at=_dt(row.get("resulted_at")),
                verified_at=_dt(row.get("verified_at")),
                verified_by=rid("user", row["verified_by"]) if row.get("verified_by") else None,
                results=row.get("results") or [],
                note=row.get("note"),
            )
        )

    for row in _by_tenant(data["INVENTORY_TX"], short):
        is_prescription = row.get("ref_type") == "prescription"
        session.add(
            InventoryTransaction(
                id=rid("inventory_tx", row["id"]),
                tenant_id=tenant_id,
                item_id=rid("item", row["item_id"]),
                type=row["type"],
                qty=row["qty"],
                batch_no=row["batch_no"],
                at=_dt(row["at"]),
                ref_type=row.get("ref_type"),
                ref_id=rid("prescription", row["ref_id"]) if is_prescription and row.get("ref_id") else None,
                note=_tx_note(row),
                actor_id=rid("user", row["actor_id"]) if row.get("actor_id") else None,
            )
        )

    for row in invoices:
        invoice = Invoice(
            id=rid("invoice", row["id"]),
            tenant_id=tenant_id,
            patient_id=rid("patient", row["patient_id"]),
            encounter_id=rid("encounter", row["encounter_id"]) if row.get("encounter_id") else None,
            number=row["number"],
            status=row["status"],
            issued_at=_d(row["issued_at"]),
            due_at=_d(row["due_at"]),
        )
        session.add(invoice)
        for position, line in enumerate(row["line_items"]):
            session.add(
                InvoiceLineItem(
                    id=rid("invoice_line", line["id"]),
                    tenant_id=tenant_id,
                    invoice_id=invoice.id,
                    position=position,
                    description=line["description"],
                    source=line["source"],
                    qty=line["qty"],
                    unit_price=line["unit_price"],
                    tax_pct=line["tax_pct"],
                )
            )
        for payment in row["payments"]:
            session.add(
                Payment(
                    id=rid("payment", payment["id"]),
                    tenant_id=tenant_id,
                    invoice_id=invoice.id,
                    amount=payment["amount"],
                    method=payment["method"],
                    at=_dt(payment["at"]),
                    reference=payment.get("reference"),
                )
            )

    await session.flush()

    # Then the clinic's past, so the reports have real rows to read rather than
    # a client-side guess at what a trend looks like.
    config = (
        await session.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_id))
    ).scalar_one()
    tenant = await session.get(Tenant, tenant_id)
    written_patients = list(
        (await session.execute(select(Patient).where(Patient.tenant_id == tenant_id))).scalars()
    )
    clinicians = list(
        (
            await session.execute(
                select(User).where(User.tenant_id == tenant_id, User.role == "doctor")
            )
        ).scalars()
    )
    await seed_history(
        session,
        tenant,
        config,
        patients=written_patients,
        doctors=clinicians,
        today=date.today(),
        invoice_prefix_from=config.invoice_sequence,
    )
    await session.flush()


def _tx_note(row: dict) -> str | None:
    """Keep the human-readable reference from the demo data when it is not a
    row id we can point at."""
    note = row.get("note")
    ref = row.get("ref_id")
    if ref and row.get("ref_type") != "prescription":
        return f"{note} · {ref}" if note else str(ref)
    return note


def _highest_suffix(values) -> int:
    """`NG-1042` → 1042. The next MRN or invoice number carries on from here."""
    best = 0
    for value in values:
        tail = str(value).rsplit("-", 1)[-1]
        if tail.isdigit():
            best = max(best, int(tail))
    return best


async def seed_all(session: AsyncSession, *, reset: bool = True) -> dict[str, int]:
    data = load_data()

    if reset:
        await set_tenant(session, None)
        await wipe(session)

    await set_tenant(session, None)
    await seed_platform_tables(session, data)

    for tenant_row in data["TENANTS"]:
        await seed_tenant(session, data, tenant_row)

    await set_tenant(session, None)
    counts = {
        "tenants": len(data["TENANTS"]),
        "users": len(data["USERS"]),
        "patients": len(data["PATIENTS"]),
        "appointments": len(data["APPOINTMENTS"]),
        "encounters": len(data["ENCOUNTERS"]),
        "invoices": len(data["INVOICES"]),
    }
    return counts


async def tenant_uuid_for_slug(session: AsyncSession, slug: str) -> uuid.UUID | None:
    row = await session.execute(select(Tenant.id).where(Tenant.slug == slug))
    return row.scalar_one_or_none()
