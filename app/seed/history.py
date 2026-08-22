"""Back-history for the demo clinics.

The reports read the transactional tables and nothing else, so a demo with only
today's rows shows honest but empty charts. Rather than have the client invent
a trend line, this writes real history: appointments that were kept, missed or
cancelled, the queue tokens they produced with real waiting and consulting
times, the encounters that came out of them, and the invoices some of them
raised.

It is deterministic — seeded from the clinic's slug — so re-seeding produces
the same past twice, and every panel agrees with every other because they are
all reading the same rows.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from datetime import time as time_t

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clinical import Appointment, Encounter, Patient, QueueEntry
from app.models.enums import (
    AppointmentChannel,
    AppointmentStatus,
    EncounterStatus,
    InvoiceStatus,
    LineItemSource,
    PaymentMethod,
    QueuePriority,
    QueueStatus,
)
from app.models.identity import User
from app.models.ops import Invoice, InvoiceLineItem, Payment
from app.models.tenancy import Tenant, TenantConfig
from app.schemas.common import zone

# Days of history to write. Long enough for a 30-day window to be full.
WINDOW_DAYS = 45

DIAGNOSES: dict[str, list[tuple[str, str]]] = {
    "dental": [
        ("Cold sensitivity, upper left", "Dental caries — occlusal"),
        ("Bleeding gums", "Chronic periodontitis"),
        ("Toothache, worse at night", "Pulpitis — irreversible"),
        ("Routine check-up", "Sound dentition — recall"),
        ("Crown feels loose", "Crown re-cementation"),
        ("Wisdom tooth pain", "Pericoronitis — lower right"),
    ],
    "ophthalmology": [
        ("Blurred distance vision", "Myopia — progression"),
        ("Gritty, tired eyes", "Dry eye disease"),
        ("Glare when driving at night", "Cataract — early nuclear"),
        ("Routine pressure check", "Ocular hypertension — monitored"),
        ("Crusted lids each morning", "Blepharitis"),
        ("Diabetic screening", "No retinopathy detected"),
    ],
    "dermatology": [
        ("Itchy flare across both elbows", "Atopic dermatitis — moderate flare"),
        ("Scaly patches on the scalp", "Plaque psoriasis"),
        ("Persistent spots along the jaw", "Acne vulgaris — inflammatory"),
        ("Changing mole on the back", "Melanocytic naevus — benign"),
        ("Rash after a new detergent", "Contact dermatitis"),
        ("Rough patch on the temple", "Actinic keratosis"),
    ],
}

PROCEDURES = {
    "dental": [
        "Scale and polish", "Composite filling", "Crown cementation", "Extraction", "Fluoride varnish",
    ],
    "ophthalmology": ["Refraction", "Tonometry", "Slit lamp", "Fundus photography"],
    "dermatology": ["Cryotherapy", "Dermoscopy", "Punch biopsy", "Phototherapy session"],
}

LESION_SITES = ["Elbows", "Scalp", "Face", "Trunk", "Hands", "Lower legs"]
TOOTH_STATES = ["filling", "crown", "caries", "watch"]

BILLABLE_LINES = {
    "dental": [("Consultation", 500.0), ("Scale and polish", 1500.0), ("Composite filling", 2500.0)],
    "ophthalmology": [("Consultation", 700.0), ("Full refraction", 400.0), ("Retinal imaging", 1200.0)],
    "dermatology": [("Consultation", 700.0), ("Cryotherapy", 1500.0), ("Dermoscopy review", 600.0)],
}


def _local(day: date, hour: int, minute: int, tz: str) -> datetime:
    return datetime.combine(day, time_t(hour, minute), tzinfo=zone(tz))


async def seed_history(
    session: AsyncSession,
    tenant: Tenant,
    config: TenantConfig,
    *,
    patients: list[Patient],
    doctors: list[User],
    today: date,
    invoice_prefix_from: int,
) -> int:
    """Write the clinic's past. Returns the last invoice number used."""
    if not patients or not doctors:
        return invoice_prefix_from

    rng = random.Random(f"astrale:{tenant.slug}")  # noqa: S311 - demo data, not security
    kind = tenant.clinical_type
    complaints = DIAGNOSES.get(kind, DIAGNOSES["dental"])
    invoice_no = invoice_prefix_from
    written = 0

    for offset in range(WINDOW_DAYS, 0, -1):
        day = today - timedelta(days=offset)
        weekday = day.weekday()
        if weekday == 6:  # closed on Sunday
            continue

        visits = rng.randint(1, 3) if weekday == 5 else rng.randint(3, 7)
        token = 0

        for slot in range(visits):
            patient = rng.choice(patients)
            doctor = rng.choice(doctors)
            complaint, diagnosis = rng.choice(complaints)
            hour = 9 + (slot * 70) // 60
            minute = (slot * 70) % 60
            if hour > 17:
                continue

            roll = rng.random()
            status = (
                AppointmentStatus.NO_SHOW if roll > 0.93
                else AppointmentStatus.CANCELLED if roll > 0.88
                else AppointmentStatus.COMPLETED
            )

            appointment = Appointment(
                tenant_id=tenant.id,
                patient_id=patient.id,
                doctor_id=doctor.id,
                date=day,
                time=f"{hour:02d}:{minute:02d}",
                duration_min=config.default_duration,
                reason=complaint,
                status=status,
                created_via=rng.choice(list(AppointmentChannel)),
            )
            session.add(appointment)

            if status != AppointmentStatus.COMPLETED:
                continue

            token += 1
            waited = rng.randint(2, 34)
            consulted = rng.randint(9, 38)
            checked_in = _local(day, hour, minute, tenant.timezone) - timedelta(minutes=rng.randint(0, 8))
            called = checked_in + timedelta(minutes=waited)
            started = called + timedelta(minutes=rng.randint(0, 3))

            session.add(
                QueueEntry(
                    tenant_id=tenant.id,
                    patient_id=patient.id,
                    appointment_id=appointment.id,
                    doctor_id=doctor.id,
                    queue_date=day,
                    token_no=token,
                    status=QueueStatus.COMPLETED,
                    priority=QueuePriority.URGENT if rng.random() > 0.85 else QueuePriority.ROUTINE,
                    reason=complaint,
                    checked_in_at=checked_in,
                    called_at=called,
                    started_at=started,
                    completed_at=started + timedelta(minutes=consulted),
                )
            )

            encounter = Encounter(
                tenant_id=tenant.id,
                patient_id=patient.id,
                doctor_id=doctor.id,
                date=day,
                chief_complaint=complaint,
                notes="Examined, findings discussed, plan agreed.",
                diagnosis=diagnosis,
                follow_up_days=rng.choice([None, 30, 90, 180]),
                clinical_ext=_exam(kind, rng),
                status=EncounterStatus.SIGNED,
                signed_at=started + timedelta(minutes=consulted),
            )
            session.add(encounter)
            written += 1

            if rng.random() > 0.45:
                invoice_no += 1
                invoice = Invoice(
                    tenant_id=tenant.id,
                    patient_id=patient.id,
                    encounter_id=encounter.id,
                    number=f"{config.invoice_prefix}-{invoice_no}",
                    status=InvoiceStatus.ISSUED,
                    issued_at=day,
                    due_at=day + timedelta(days=config.payment_terms_days),
                )
                session.add(invoice)
                await session.flush()

                total = 0.0
                for position, (description, price) in enumerate(rng.sample(BILLABLE_LINES[kind], k=2)):
                    total += price
                    session.add(
                        InvoiceLineItem(
                            tenant_id=tenant.id,
                            invoice_id=invoice.id,
                            position=position,
                            description=description,
                            source=LineItemSource.CONSULTATION if position == 0 else LineItemSource.PROCEDURE,
                            qty=1,
                            unit_price=price,
                            tax_pct=config.tax_pct,
                        )
                    )

                paid_roll = rng.random()
                if paid_roll > 0.25:
                    amount = total if paid_roll > 0.4 else round(total / 2, 2)
                    session.add(
                        Payment(
                            tenant_id=tenant.id,
                            invoice_id=invoice.id,
                            amount=amount,
                            method=rng.choice(list(PaymentMethod)),
                            at=_local(day, 17, 30, tenant.timezone),
                        )
                    )
                    invoice.status = (
                        InvoiceStatus.PAID if amount >= total else InvoiceStatus.PARTIALLY_PAID
                    )

        await session.flush()

    config.invoice_sequence = max(config.invoice_sequence, invoice_no)
    return invoice_no


def _exam(kind: str, rng: random.Random) -> dict:
    """A plausible `clinical_ext` for the discipline — the payload the clinical
    analytics panel is counted from."""
    common = {"bp": f"{rng.randint(108, 138)}/{rng.randint(66, 88)}", "pulse": rng.randint(58, 92)}

    if kind == "dental":
        return {
            **common,
            "pain_scale": rng.randint(0, 8),
            "oral_hygiene": rng.choice(["Good", "Fair", "Poor"]),
            "perio_bleeding": rng.random() > 0.6,
            "tooth_chart": {
                str(rng.randint(11, 48)): rng.choice(TOOTH_STATES) for _ in range(rng.randint(1, 3))
            },
            "procedures": rng.sample(PROCEDURES["dental"], k=rng.randint(1, 2)),
        }

    if kind == "ophthalmology":
        return {
            **common,
            "iop_od": rng.randint(11, 24),
            "iop_os": rng.randint(11, 24),
            "fundus": "No haemorrhage or exudate.",
            "refraction": {
                "od_sph": round(rng.uniform(-4.5, 1.5), 2),
                "os_sph": round(rng.uniform(-4.5, 1.5), 2),
            },
            "va": {"od": rng.choice(["6/6", "6/9", "6/12"]), "os": rng.choice(["6/6", "6/9", "6/12"])},
            "procedures": rng.sample(PROCEDURES["ophthalmology"], k=rng.randint(1, 2)),
        }

    return {
        **common,
        "bsa": rng.randint(1, 22),
        "itch_score": rng.randint(0, 9),
        "phototherapy": rng.random() > 0.7,
        "lesion_sites": rng.sample(LESION_SITES, k=rng.randint(1, 3)),
        "procedures": rng.sample(PROCEDURES["dermatology"], k=1),
    }
