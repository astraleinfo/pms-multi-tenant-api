"""§3.13 — the patient's story, composed from the tables that already hold it.

A read composition, not a copy: nothing here has its own store, so history can
never fall out of step with the rows it describes. Unverified lab results are
summarised but never revealed.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clinical import Appointment, Encounter, LabOrder, Patient, Prescription, QueueEntry
from app.models.enums import LabOrderStatus
from app.models.identity import User
from app.models.ops import Invoice
from app.schemas.clinical import HistoryEventOut
from app.schemas.common import clock, iso_date, stamp
from app.services.billing import totals_for


def _minutes_between(start, end) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() // 60))


async def patient_history(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    patient: Patient,
    *,
    tz: str,
    currency_symbol: str = "₹",
) -> list[HistoryEventOut]:
    names = {
        u.id: u.name
        for u in (await session.execute(select(User).where(User.tenant_id == tenant_id))).scalars()
    }

    def who(user_id: uuid.UUID | None) -> str:
        return names.get(user_id, "—") if user_id else "—"

    events: list[HistoryEventOut] = [
        HistoryEventOut(
            id=f"reg-{patient.id}",
            kind="registration",
            at=iso_date(patient.registered_at) or "",
            title="Registered at this clinic",
            detail=f"Medical record {patient.mrn} created.",
            tone="neutral",
        )
    ]

    appointments = (
        await session.execute(
            select(Appointment).where(
                Appointment.tenant_id == tenant_id, Appointment.patient_id == patient.id
            )
        )
    ).scalars()
    tone_for = {"no_show": "critical", "cancelled": "neutral"}
    for a in appointments:
        events.append(
            HistoryEventOut(
                id=f"apt-{a.id}",
                kind="appointment",
                at=iso_date(a.date) or "",
                title=f"Appointment — {a.reason}",
                detail=f"{a.time} with {who(a.doctor_id)} · booked {a.created_via.replace('_', ' ')}",
                meta=a.status,
                href="/appointments",
                tone=tone_for.get(a.status, "info"),
            )
        )

    queue_entries = (
        await session.execute(
            select(QueueEntry).where(
                QueueEntry.tenant_id == tenant_id, QueueEntry.patient_id == patient.id
            )
        )
    ).scalars()
    for q in queue_entries:
        waited = _minutes_between(q.checked_in_at, q.called_at)
        checked_in = clock(q.checked_in_at, tz)
        events.append(
            HistoryEventOut(
                id=f"q-{q.id}",
                kind="queue",
                at=iso_date(q.queue_date) or "",
                title=f"Token {q.token_no:02d} issued",
                detail=(
                    f"Checked in {checked_in}, called after {waited} min."
                    if waited is not None
                    else f"Checked in {checked_in}."
                ),
                meta=q.status,
                href="/queue",
                tone="accent",
            )
        )

    encounters = (
        await session.execute(
            select(Encounter).where(Encounter.tenant_id == tenant_id, Encounter.patient_id == patient.id)
        )
    ).scalars()
    for e in encounters:
        events.append(
            HistoryEventOut(
                id=f"enc-{e.id}",
                kind="encounter",
                at=iso_date(e.date) or "",
                title=e.diagnosis or e.chief_complaint or "Consultation",
                detail=e.notes,
                meta=f"Seen by {who(e.doctor_id)}",
                href=f"/consultations/{e.id}",
                tone="accent",
            )
        )

    prescriptions = (
        await session.execute(
            select(Prescription).where(
                Prescription.tenant_id == tenant_id, Prescription.patient_id == patient.id
            )
        )
    ).scalars()
    for rx in prescriptions:
        issued = (stamp(rx.issued_at, tz) or "")[:10]
        count = len(rx.items)
        events.append(
            HistoryEventOut(
                id=f"rx-{rx.id}",
                kind="prescription",
                at=issued,
                title=f"Prescription — {count} item{'' if count == 1 else 's'}",
                detail=", ".join(f"{i.medicine} {i.strength}".strip() for i in rx.items),
                meta=rx.status,
                href="/prescriptions",
                tone="accent",
            )
        )
        for item in rx.items:
            if item.dispensed_qty <= 0:
                continue
            events.append(
                HistoryEventOut(
                    id=f"disp-{item.id}",
                    kind="dispense",
                    at=issued,
                    title=f"Dispensed {item.medicine}",
                    detail=f"{item.dispensed_qty} of {item.qty} handed over.",
                    href="/pharmacy",
                    tone="ok",
                )
            )

    orders = (
        await session.execute(
            select(LabOrder).where(LabOrder.tenant_id == tenant_id, LabOrder.patient_id == patient.id)
        )
    ).scalars()
    for order in orders:
        verified = order.status == LabOrderStatus.VERIFIED
        events.append(
            HistoryEventOut(
                id=f"lab-{order.id}",
                kind="lab",
                at=(stamp(order.ordered_at, tz) or "")[:10],
                title=f"Lab — {order.test_name}",
                detail=(
                    " · ".join(
                        f"{r.get('analyte')}: {r.get('value')}{(' ' + r['unit']) if r.get('unit') else ''}"
                        for r in (order.results or [])
                    )
                    if verified
                    else "Results are held until a technician verifies them."
                ),
                meta=order.status,
                href="/lab",
                tone="ok" if verified else "warn",
            )
        )

    invoices = (
        await session.execute(
            select(Invoice).where(Invoice.tenant_id == tenant_id, Invoice.patient_id == patient.id)
        )
    ).scalars()
    for invoice in invoices:
        figures = totals_for(invoice)
        count = len(invoice.line_items)
        events.append(
            HistoryEventOut(
                id=f"inv-{invoice.id}",
                kind="invoice",
                at=iso_date(invoice.issued_at) or "",
                title=f"Invoice {invoice.number}",
                detail=(
                    f"{count} line item{'' if count == 1 else 's'} · "
                    f"{currency_symbol}{figures['total']:.2f}"
                ),
                meta=invoice.status,
                href=f"/billing/{invoice.id}",
                tone="warn",
            )
        )
        for payment in invoice.payments:
            events.append(
                HistoryEventOut(
                    id=f"pay-{payment.id}",
                    kind="payment",
                    at=(stamp(payment.at, tz) or "")[:10],
                    title=f"Payment received — {currency_symbol}{float(payment.amount):.2f}",
                    detail=(
                        f"{payment.method.upper()}"
                        f"{' · ' + payment.reference if payment.reference else ''}"
                        f" against {invoice.number}"
                    ),
                    href=f"/billing/{invoice.id}",
                    tone="ok",
                )
            )

    events.sort(key=lambda e: e.at, reverse=True)
    return events


def today_for(tz: str) -> date:
    from datetime import datetime

    from app.schemas.common import zone

    return datetime.now(zone(tz)).date()
