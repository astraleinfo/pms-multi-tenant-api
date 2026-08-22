"""§3.14 — analytics computed off the transactional tables.

Three layers, kept apart on purpose:

* **core** — always computed, for every clinic;
* **modules** — only for features that are actually active, so a clinic that
  never bought Billing is never told what its revenue mix is;
* **clinical** — driven purely by what is inside `clinical_ext`, never by an
  entitlement.

Everything here is aggregated in PostgreSQL, under this clinic's row-level
policy. The client receives figures, not rows: a browser never sees a patient
in order to count patients, and a clinic with fifty thousand of them sends the
same handful of kilobytes as one with fifty.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import Date, Numeric, and_, case, cast, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clinical import Appointment, Encounter, LabOrder, Patient, Prescription, QueueEntry
from app.models.enums import (
    AppointmentStatus,
    DispenseStatus,
    InventoryTxType,
    InvoiceStatus,
    LabOrderStatus,
    QueueStatus,
)
from app.models.identity import User
from app.models.ops import (
    InventoryBatch,
    InventoryItem,
    InventoryTransaction,
    Invoice,
    InvoiceLineItem,
    Payment,
)

# What an invoice line is worth, tax included. Written once so every figure that
# mentions money agrees with every other one.
LINE_VALUE = cast(InvoiceLineItem.qty, Numeric) * InvoiceLineItem.unit_price * (
    1 + InvoiceLineItem.tax_pct / 100
)

BILLABLE = Invoice.status.notin_([InvoiceStatus.VOID, InvoiceStatus.DRAFT])


async def _scalar(session: AsyncSession, stmt) -> float:
    return float((await session.execute(stmt)).scalar() or 0)


async def _count(session: AsyncSession, model, *conditions) -> int:
    return int(await _scalar(session, select(func.count(model.id)).where(*conditions)))


async def dashboard(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    today: date,
    days: int = 30,
    active_features: set[str],
    clinical_type: str,
) -> dict:
    since = today - timedelta(days=days - 1)

    core = {
        **await _register(session, tenant_id, today=today, since=since),
        **await _queue(session, tenant_id, today=today, since=since),
        "visits_series": await _visits_series(session, tenant_id, today=today, days=days),
        "attendance_series": await _attendance_series(session, tenant_id, today=today, days=min(days, 14)),
        "top_diagnoses": await _top_diagnoses(session, tenant_id, since=since),
    }

    modules: dict[str, object] = {}
    if "pharmacy" in active_features:
        modules["pharmacy"] = await _pharmacy(session, tenant_id, since=since)
    if "lab" in active_features:
        modules["lab"] = await _lab(session, tenant_id)
    if "inventory" in active_features:
        modules["inventory"] = await _inventory(session, tenant_id, today=today)
    if "billing" in active_features:
        modules["billing"] = await _billing(session, tenant_id)
    if "advanced_reports" in active_features:
        modules["advanced_reports"] = {
            "age_cohort": await _age_cohort(session, tenant_id, today=today),
            "sex_split": await _sex_split(session, tenant_id),
            "clinicians": await _clinicians(session, tenant_id),
        }

    return {
        "range": {"from": since.isoformat(), "to": today.isoformat(), "days": days},
        "core": core,
        "modules": modules,
        "clinical": await _clinical(session, tenant_id, clinical_type=clinical_type, since=since),
    }


# --- core --------------------------------------------------------------------


async def _register(session: AsyncSession, tenant_id: uuid.UUID, *, today: date, since: date) -> dict:
    month_start = today.replace(day=1)
    return {
        "patients": await _count(session, Patient, Patient.tenant_id == tenant_id),
        "new_patients": await _count(
            session, Patient, Patient.tenant_id == tenant_id, Patient.registered_at >= month_start
        ),
        "appointments_today": await _count(
            session, Appointment, Appointment.tenant_id == tenant_id, Appointment.date == today
        ),
        "no_shows": await _count(
            session,
            Appointment,
            Appointment.tenant_id == tenant_id,
            Appointment.date >= since,
            Appointment.status == AppointmentStatus.NO_SHOW,
        ),
        "seen_today": await _count(
            session, Patient, Patient.tenant_id == tenant_id, Patient.last_visit == today
        ),
        "never_visited": await _count(
            session, Patient, Patient.tenant_id == tenant_id, Patient.last_visit.is_(None)
        ),
        "encounters": await _count(
            session, Encounter, Encounter.tenant_id == tenant_id, Encounter.date >= since
        ),
        "prescriptions": await _count(
            session,
            Prescription,
            Prescription.tenant_id == tenant_id,
            cast(Prescription.issued_at, Date) >= since,
        ),
        "revenue": round(
            await _scalar(
                session,
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.tenant_id == tenant_id, cast(Payment.at, Date) >= since
                ),
            ),
            2,
        ),
    }


async def _queue(session: AsyncSession, tenant_id: uuid.UUID, *, today: date, since: date) -> dict:
    todays = dict(
        (
            await session.execute(
                select(QueueEntry.status, func.count(QueueEntry.id))
                .where(QueueEntry.tenant_id == tenant_id, QueueEntry.queue_date == today)
                .group_by(QueueEntry.status)
            )
        ).all()
    )

    waited = func.extract("epoch", QueueEntry.called_at - QueueEntry.checked_in_at) / 60
    consulted = func.extract("epoch", QueueEntry.completed_at - QueueEntry.started_at) / 60

    timings = (
        await session.execute(
            select(func.avg(waited), func.max(waited), func.count()).where(
                QueueEntry.tenant_id == tenant_id,
                QueueEntry.queue_date >= since,
                QueueEntry.called_at.isnot(None),
            )
        )
    ).one()
    consult_avg = await _scalar(
        session,
        select(func.avg(consulted)).where(
            QueueEntry.tenant_id == tenant_id,
            QueueEntry.queue_date >= since,
            QueueEntry.started_at.isnot(None),
            QueueEntry.completed_at.isnot(None),
        ),
    )

    return {
        "waiting_now": int(todays.get(QueueStatus.WAITING, 0)),
        "in_consultation": int(todays.get(QueueStatus.IN_CONSULTATION, 0)),
        "completed_today": int(todays.get(QueueStatus.COMPLETED, 0)),
        "avg_wait_minutes": round(float(timings[0] or 0), 1),
        "max_wait_minutes": round(float(timings[1] or 0), 1),
        "avg_consultation_minutes": round(consult_avg, 1),
        "wait_samples": int(timings[2] or 0),
        "queue_funnel": [
            {"label": label, "value": int(todays.get(status, 0))}
            for label, status in (
                ("Waiting", QueueStatus.WAITING),
                ("Called", QueueStatus.CALLED),
                ("In consultation", QueueStatus.IN_CONSULTATION),
                ("Completed", QueueStatus.COMPLETED),
                ("No show", QueueStatus.NO_SHOW),
            )
        ],
    }


async def _visits_series(
    session: AsyncSession, tenant_id: uuid.UUID, *, today: date, days: int
) -> list[dict]:
    """Encounters recorded per day. Days with none are days with none."""
    since = today - timedelta(days=days - 1)
    rows = dict(
        (
            await session.execute(
                select(Encounter.date, func.count(Encounter.id))
                .where(Encounter.tenant_id == tenant_id, Encounter.date >= since)
                .group_by(Encounter.date)
            )
        ).all()
    )
    return [
        {
            "date": (since + timedelta(days=i)).isoformat(),
            "value": int(rows.get(since + timedelta(days=i), 0)),
        }
        for i in range(days)
    ]


async def _attendance_series(
    session: AsyncSession, tenant_id: uuid.UUID, *, today: date, days: int
) -> list[dict]:
    since = today - timedelta(days=days - 1)
    kept = func.count().filter(
        Appointment.status.in_([AppointmentStatus.COMPLETED, AppointmentStatus.CHECKED_IN])
    )
    missed = func.count().filter(Appointment.status == AppointmentStatus.NO_SHOW)
    cancelled = func.count().filter(Appointment.status == AppointmentStatus.CANCELLED)

    rows = {
        row[0]: {"kept": int(row[1]), "missed": int(row[2]), "cancelled": int(row[3])}
        for row in (
            await session.execute(
                select(Appointment.date, kept, missed, cancelled)
                .where(Appointment.tenant_id == tenant_id, Appointment.date >= since)
                .group_by(Appointment.date)
            )
        ).all()
    }
    empty = {"kept": 0, "missed": 0, "cancelled": 0}
    return [
        {"date": (since + timedelta(days=i)).isoformat(), **rows.get(since + timedelta(days=i), empty)}
        for i in range(days)
    ]


async def _top_diagnoses(
    session: AsyncSession, tenant_id: uuid.UUID, *, since: date, limit: int = 6
) -> list[dict]:
    label = func.nullif(func.trim(Encounter.diagnosis), "")
    rows = await session.execute(
        select(func.coalesce(label, Encounter.chief_complaint).label("label"), func.count())
        .where(Encounter.tenant_id == tenant_id, Encounter.date >= since)
        .group_by("label")
        .order_by(func.count().desc())
        .limit(limit)
    )
    return [{"label": name, "value": int(count)} for name, count in rows.all() if name]


# --- modules -----------------------------------------------------------------


async def _pharmacy(session: AsyncSession, tenant_id: uuid.UUID, *, since: date) -> dict:
    dispensed = InventoryTransaction.type == InventoryTxType.DISPENSE
    units = await _scalar(
        session,
        select(func.coalesce(func.sum(func.abs(InventoryTransaction.qty)), 0)).where(
            InventoryTransaction.tenant_id == tenant_id,
            dispensed,
            cast(InventoryTransaction.at, Date) >= since,
        ),
    )
    value = await _scalar(
        session,
        select(
            func.coalesce(func.sum(func.abs(InventoryTransaction.qty) * InventoryItem.unit_price), 0)
        )
        .select_from(InventoryTransaction)
        .join(InventoryItem, InventoryItem.id == InventoryTransaction.item_id)
        .where(
            InventoryTransaction.tenant_id == tenant_id,
            dispensed,
            cast(InventoryTransaction.at, Date) >= since,
        ),
    )
    by_status = dict(
        (
            await session.execute(
                select(Prescription.status, func.count(Prescription.id))
                .where(Prescription.tenant_id == tenant_id)
                .group_by(Prescription.status)
            )
        ).all()
    )
    waiting = sum(
        count for status, count in by_status.items() if status != DispenseStatus.DISPENSED
    )
    return {
        "dispensed_units": int(units),
        "dispensed_value": round(value, 2),
        "prescriptions_waiting": int(waiting),
        "fully_dispensed": int(by_status.get(DispenseStatus.DISPENSED, 0)),
    }


async def _lab(session: AsyncSession, tenant_id: uuid.UUID) -> dict:
    by_status = dict(
        (
            await session.execute(
                select(LabOrder.status, func.count(LabOrder.id))
                .where(LabOrder.tenant_id == tenant_id)
                .group_by(LabOrder.status)
            )
        ).all()
    )
    # Containment rather than a path expression: `@>` is the operator a GIN
    # index on `results` would use, and a flag is either high or low.
    flagged = or_(
        LabOrder.results.contains([{"flag": "high"}]),
        LabOrder.results.contains([{"flag": "low"}]),
    )
    abnormal = await _scalar(
        session,
        select(func.count(LabOrder.id)).where(LabOrder.tenant_id == tenant_id, flagged),
    )
    return {
        "total": int(sum(by_status.values())),
        "ordered": int(by_status.get(LabOrderStatus.ORDERED, 0)),
        "pending": int(by_status.get(LabOrderStatus.PENDING, 0)),
        "resulted": int(by_status.get(LabOrderStatus.RESULTED, 0)),
        "verified": int(by_status.get(LabOrderStatus.VERIFIED, 0)),
        "abnormal": int(abnormal),
    }


async def _inventory(session: AsyncSession, tenant_id: uuid.UUID, *, today: date) -> dict:
    """Stock on hand is the sum of the ledger — never a stored counter (§3.11)."""
    levels = (
        await session.execute(
            select(
                InventoryItem.id,
                InventoryItem.name,
                InventoryItem.reorder_level,
                func.coalesce(func.sum(InventoryTransaction.qty), 0),
            )
            .outerjoin(
                InventoryTransaction,
                and_(
                    InventoryTransaction.item_id == InventoryItem.id,
                    InventoryTransaction.tenant_id == tenant_id,
                ),
            )
            .where(InventoryItem.tenant_id == tenant_id)
            .group_by(InventoryItem.id, InventoryItem.name, InventoryItem.reorder_level)
            .order_by(InventoryItem.name)
        )
    ).all()

    expiring = (
        await session.execute(
            select(InventoryBatch.batch_no, InventoryBatch.expiry, InventoryBatch.qty, InventoryItem.name)
            .join(InventoryItem, InventoryItem.id == InventoryBatch.item_id)
            .where(
                InventoryBatch.tenant_id == tenant_id,
                InventoryBatch.qty > 0,
                InventoryBatch.expiry <= today + timedelta(days=90),
            )
            .order_by(InventoryBatch.expiry)
            .limit(10)
        )
    ).all()

    return {
        "items": len(levels),
        "below_reorder": [
            {"item_id": str(item_id), "name": name, "on_hand": int(level), "reorder_level": reorder}
            for item_id, name, reorder, level in levels
            if int(level) <= reorder
        ],
        "expiring_soon": [
            {
                "batch_no": batch,
                "expiry": expiry.isoformat(),
                "qty": qty,
                "item": name,
                "days_left": (expiry - today).days,
            }
            for batch, expiry, qty, name in expiring
        ],
    }


async def _billing(session: AsyncSession, tenant_id: uuid.UUID) -> dict:
    billed = await _scalar(
        session,
        select(func.coalesce(func.sum(LINE_VALUE), 0))
        .select_from(InvoiceLineItem)
        .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
        .where(InvoiceLineItem.tenant_id == tenant_id, BILLABLE),
    )
    paid = await _scalar(
        session,
        select(func.coalesce(func.sum(Payment.amount), 0))
        .select_from(Payment)
        .join(Invoice, Invoice.id == Payment.invoice_id)
        .where(Payment.tenant_id == tenant_id, Invoice.status != InvoiceStatus.VOID),
    )
    counts = dict(
        (
            await session.execute(
                select(Invoice.status, func.count(Invoice.id))
                .where(Invoice.tenant_id == tenant_id)
                .group_by(Invoice.status)
            )
        ).all()
    )
    mix = (
        await session.execute(
            select(InvoiceLineItem.source, func.coalesce(func.sum(LINE_VALUE), 0))
            .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
            .where(InvoiceLineItem.tenant_id == tenant_id, BILLABLE)
            .group_by(InvoiceLineItem.source)
            .order_by(func.coalesce(func.sum(LINE_VALUE), 0).desc())
        )
    ).all()

    live = sum(count for status, count in counts.items() if status != InvoiceStatus.VOID)
    return {
        "billed": round(billed, 2),
        "paid": round(paid, 2),
        "outstanding": round(max(0.0, billed - paid), 2),
        "count": int(live),
        "unpaid": int(
            counts.get(InvoiceStatus.ISSUED, 0) + counts.get(InvoiceStatus.PARTIALLY_PAID, 0)
        ),
        "drafts": int(counts.get(InvoiceStatus.DRAFT, 0)),
        "revenue_mix": [
            {"label": str(source), "value": round(float(total or 0), 2)} for source, total in mix
        ],
    }


async def _age_cohort(session: AsyncSession, tenant_id: uuid.UUID, *, today: date) -> list[dict]:
    age = func.date_part("year", func.age(Patient.dob))
    band = case(
        (age < 18, "0–17"),
        (age < 35, "18–34"),
        (age < 55, "35–54"),
        (age < 75, "55–74"),
        else_="75+",
    ).label("band")
    rows = dict(
        (
            await session.execute(
                select(band, func.count()).where(Patient.tenant_id == tenant_id).group_by(band)
            )
        ).all()
    )
    bands = ("0–17", "18–34", "35–54", "55–74", "75+")
    return [{"label": label, "value": int(rows.get(label, 0))} for label in bands]


async def _sex_split(session: AsyncSession, tenant_id: uuid.UUID) -> list[dict]:
    rows = await session.execute(
        select(Patient.sex, func.count()).where(Patient.tenant_id == tenant_id).group_by(Patient.sex)
    )
    return [{"label": str(sex).capitalize(), "value": int(count)} for sex, count in rows.all()]


async def _clinicians(session: AsyncSession, tenant_id: uuid.UUID) -> list[dict]:
    """Volume and revenue per clinician, joined through the encounter an invoice
    was raised against."""
    encounters = (
        select(Encounter.doctor_id, func.count(Encounter.id).label("seen"))
        .where(Encounter.tenant_id == tenant_id)
        .group_by(Encounter.doctor_id)
        .subquery()
    )
    prescriptions = (
        select(Prescription.doctor_id, func.count(Prescription.id).label("written"))
        .where(Prescription.tenant_id == tenant_id)
        .group_by(Prescription.doctor_id)
        .subquery()
    )
    revenue = (
        select(Encounter.doctor_id, func.coalesce(func.sum(LINE_VALUE), 0).label("revenue"))
        .select_from(InvoiceLineItem)
        .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
        .join(Encounter, Encounter.id == Invoice.encounter_id)
        .where(InvoiceLineItem.tenant_id == tenant_id, Invoice.status != InvoiceStatus.VOID)
        .group_by(Encounter.doctor_id)
        .subquery()
    )

    rows = await session.execute(
        select(
            User.id,
            User.name,
            User.initials,
            User.speciality,
            func.coalesce(encounters.c.seen, 0),
            func.coalesce(prescriptions.c.written, 0),
            func.coalesce(revenue.c.revenue, 0),
        )
        .outerjoin(encounters, encounters.c.doctor_id == User.id)
        .outerjoin(prescriptions, prescriptions.c.doctor_id == User.id)
        .outerjoin(revenue, revenue.c.doctor_id == User.id)
        .where(User.tenant_id == tenant_id, User.role == "doctor")
        .order_by(func.coalesce(encounters.c.seen, 0).desc(), User.name)
    )
    return [
        {
            "user_id": str(user_id),
            "name": name,
            "initials": initials,
            "speciality": speciality,
            "encounters": int(seen),
            "prescriptions": int(written),
            "revenue": round(float(money or 0), 2),
        }
        for user_id, name, initials, speciality, seen, written, money in rows.all()
    ]


# --- clinical ----------------------------------------------------------------


def _json_array(field):
    """Expand a JSONB array of strings into rows, treating a missing key as empty.

    `procedures` and `lesion_sites` are lists inside `clinical_ext`; counting
    them means unnesting them in the database rather than shipping every
    encounter to the client to be counted there.
    """
    empty = literal_column("'[]'::jsonb")
    return func.jsonb_array_elements_text(func.coalesce(field, empty))


async def _clinical(
    session: AsyncSession, tenant_id: uuid.UUID, *, clinical_type: str, since: date
) -> dict:
    """Driven by `clinical_ext`, never by a feature flag.

    The breakdown is counted in PostgreSQL straight out of the JSONB, so the
    shape of a discipline's exam payload is the only thing that decides what
    comes back.
    """
    scope = (Encounter.tenant_id == tenant_id, Encounter.date >= since)

    async def average(key: str) -> float:
        return round(
            await _scalar(
                session,
                select(func.avg(cast(Encounter.clinical_ext[key].astext, Numeric))).where(
                    *scope, Encounter.clinical_ext.has_key(key)  # noqa: W601 - JSONB operator
                ),
            ),
            1,
        )

    async def counted(where) -> int:
        return int(await _scalar(session, select(func.count(Encounter.id)).where(*scope, where)))

    async def tally(expression) -> list[dict]:
        rows = await session.execute(
            select(expression.label("label"), func.count())
            .select_from(Encounter)
            .where(*scope)
            .group_by("label")
            .order_by(func.count().desc())
            .limit(8)
        )
        return [{"label": str(label), "value": int(count)} for label, count in rows.all() if label]

    if clinical_type == "dental":
        procedures = await tally(_json_array(Encounter.clinical_ext["procedures"]))
        return {
            "type": "dental",
            "avg_pain_score": await average("pain_scale"),
            "bleeding_on_probing": await counted(Encounter.clinical_ext["perio_bleeding"].astext == "true"),
            "breakdown": procedures,
        }

    if clinical_type == "ophthalmology":
        raised = await counted(
            func.greatest(
                func.coalesce(cast(Encounter.clinical_ext["iop_od"].astext, Numeric), 0),
                func.coalesce(cast(Encounter.clinical_ext["iop_os"].astext, Numeric), 0),
            )
            >= 21
        )
        recorded = await counted(Encounter.clinical_ext.has_key("refraction"))  # noqa: W601
        acuity = await counted(Encounter.clinical_ext.has_key("va"))  # noqa: W601
        return {
            "type": "ophthalmology",
            "avg_iop_right": await average("iop_od"),
            "avg_iop_left": await average("iop_os"),
            "breakdown": [
                {"label": "Raised intraocular pressure", "value": raised},
                {"label": "Refraction recorded", "value": recorded},
                {"label": "Acuity recorded", "value": acuity},
            ],
        }

    if clinical_type == "dermatology":
        sites = await tally(_json_array(Encounter.clinical_ext["lesion_sites"]))
        extensive = await counted(
            func.coalesce(cast(Encounter.clinical_ext["bsa"].astext, Numeric), 0) >= 10
        )
        return {
            "type": "dermatology",
            "avg_body_surface_area": await average("bsa"),
            "on_phototherapy": await counted(Encounter.clinical_ext["phototherapy"].astext == "true"),
            "breakdown": [*sites, {"label": "Extensive involvement (≥10% BSA)", "value": extensive}],
        }

    return {
        "type": clinical_type,
        "breakdown": [],
        "encounters": await counted(Encounter.id.isnot(None)),
    }
