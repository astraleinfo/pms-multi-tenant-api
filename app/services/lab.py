"""§3.10 — lab orders.

`ordered → pending → resulted → verified`, and nothing skips a step. Results
exist as soon as a technician types them, but they are withheld from the
patient record until someone verifies them — that release is the whole point of
the last transition.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.models.clinical import LabAttachment, LabOrder, Patient
from app.models.enums import LAB_TRANSITIONS, LabOrderStatus
from app.models.tenancy import LabTest
from app.schemas.clinical import LabOrderIn, LabResultsIn
from app.services import encounters as encounter_service
from app.services import paging


def _now() -> datetime:
    return datetime.now(timezone.utc)


LAB_SORTS = {
    "ordered_at": (LabOrder.ordered_at,),
    "test_name": (LabOrder.test_name,),
    "panel": (LabOrder.panel,),
    "status": (LabOrder.status,),
}


async def list_orders(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    status: LabOrderStatus | None = None,
    patient_id: uuid.UUID | None = None,
    encounter_id: uuid.UUID | None = None,
    unverified_only: bool = False,
    search: str | None = None,
    limit: int = paging.ALL,
    offset: int = 0,
    sort: str = "ordered_at",
    descending: bool = True,
) -> tuple[list[LabOrder], int]:
    """One page of the order book, and how many orders matched.

    `unverified_only` is the bench's own view — everything still in flight,
    which is one state machine away from any single status.
    """
    stmt = select(LabOrder).where(LabOrder.tenant_id == tenant_id)
    if status is not None:
        stmt = stmt.where(LabOrder.status == status)
    if unverified_only:
        stmt = stmt.where(LabOrder.status != LabOrderStatus.VERIFIED)
    if patient_id is not None:
        stmt = stmt.where(LabOrder.patient_id == patient_id)
    if encounter_id is not None:
        stmt = stmt.where(LabOrder.encounter_id == encounter_id)
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.join(LabOrder.patient).where(
            or_(
                Patient.full_name.ilike(like),
                Patient.mrn.ilike(like),
                LabOrder.test_name.ilike(like),
                LabOrder.test_code.ilike(like),
                LabOrder.panel.ilike(like),
            )
        )

    columns = LAB_SORTS.get(sort, LAB_SORTS["ordered_at"])
    order = [c.desc().nulls_last() if descending else c.asc().nulls_last() for c in columns]
    stmt = stmt.order_by(*order, LabOrder.id)

    return await paging.page(session, stmt, limit=limit, offset=offset)


async def get_order(session: AsyncSession, tenant_id: uuid.UUID, order_id: uuid.UUID) -> LabOrder:
    row = (
        await session.execute(
            select(LabOrder).where(LabOrder.id == order_id, LabOrder.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("No such lab order.")
    return row


async def create_orders(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    encounter_id: uuid.UUID,
    payload: LabOrderIn,
    *,
    ordered_by: uuid.UUID,
) -> list[LabOrder]:
    encounter = await encounter_service.get_encounter(session, tenant_id, encounter_id)
    created: list[LabOrder] = []

    for test in payload.tests:
        catalogue = await session.get(LabTest, test.code.upper())
        order = LabOrder(
            tenant_id=tenant_id,
            encounter_id=encounter.id,
            patient_id=encounter.patient_id,
            ordered_by=ordered_by,
            test_code=test.code.upper(),
            test_name=test.name or (catalogue.name if catalogue else test.code),
            panel=test.panel or (catalogue.panel if catalogue else ""),
            status=LabOrderStatus.ORDERED,
            ordered_at=_now(),
            results=[],
            note=test.note,
        )
        session.add(order)
        created.append(order)

    await session.flush()
    for order in created:
        await session.refresh(order, ["patient"])
    return created


def _transition(order: LabOrder, target: LabOrderStatus) -> None:
    current = LabOrderStatus(order.status)
    if target not in LAB_TRANSITIONS[current]:
        raise Conflict(f"A lab order cannot go from {current.value} to {target.value}.")
    order.status = target


async def start_order(session: AsyncSession, tenant_id: uuid.UUID, order_id: uuid.UUID) -> LabOrder:
    order = await get_order(session, tenant_id, order_id)
    _transition(order, LabOrderStatus.PENDING)
    await session.flush()
    return order


async def save_results(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    order_id: uuid.UUID,
    payload: LabResultsIn,
    *,
    actor_id: uuid.UUID,
) -> LabOrder:
    order = await get_order(session, tenant_id, order_id)
    if order.status == LabOrderStatus.ORDERED:
        _transition(order, LabOrderStatus.PENDING)
    _transition(order, LabOrderStatus.RESULTED)
    order.results = [line.model_dump() for line in payload.results]
    order.resulted_at = _now()
    order.resulted_by = actor_id
    await session.flush()
    return order


async def verify_order(
    session: AsyncSession, tenant_id: uuid.UUID, order_id: uuid.UUID, *, actor_id: uuid.UUID
) -> LabOrder:
    """Verification is what releases the result into the patient record.

    `resulted_by` and `verified_by` are both recorded, so a four-eyes rule can
    be turned on later without a migration — see the note in the README.
    """
    order = await get_order(session, tenant_id, order_id)
    if not order.results:
        raise Conflict("There is nothing to verify yet.")
    _transition(order, LabOrderStatus.VERIFIED)
    order.verified_at = _now()
    order.verified_by = actor_id
    await session.flush()
    return order


async def results_for_encounter(
    session: AsyncSession, tenant_id: uuid.UUID, encounter_id: uuid.UUID, *, verified_only: bool = True
) -> list[LabOrder]:
    """The patient-record view: verified only, unless a clinician asks to see
    what is still in flight."""
    orders, _ = await list_orders(session, tenant_id, encounter_id=encounter_id)
    if verified_only:
        return [o for o in orders if o.status == LabOrderStatus.VERIFIED]
    return orders


# --- attachments -------------------------------------------------------------
#
# A lab order collects paper: a scanned request form, a machine printout, the
# radiologist's signed report. These are the same clinical record as the typed
# analytes, so they live in the same tenant-owned table space and are reached
# through the order — never by a bare file id.

#: 15 MB. Big enough for a scanned report or a DICOM-derived JPEG, small enough
#: that one upload cannot fill a page of the ledger's storage.
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024

#: What a lab actually files. Anything else is refused by name rather than
#: stored and hoped about — an executable in a patient record is a mistake
#: whatever it is called.
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
    "text/csv",
    "text/plain",
}


async def list_attachments(
    session: AsyncSession, tenant_id: uuid.UUID, order_id: uuid.UUID
) -> list[LabAttachment]:
    """Everything filed against one order — names and sizes, not the bytes."""
    await get_order(session, tenant_id, order_id)
    return list(
        (
            await session.execute(
                select(LabAttachment)
                .where(
                    LabAttachment.tenant_id == tenant_id,
                    LabAttachment.lab_order_id == order_id,
                )
                .order_by(LabAttachment.uploaded_at.desc())
            )
        ).scalars()
    )


async def get_attachment(
    session: AsyncSession, tenant_id: uuid.UUID, order_id: uuid.UUID, attachment_id: uuid.UUID
) -> LabAttachment:
    """One file, bytes included — the only read that loads `data`."""
    row = (
        await session.execute(
            select(LabAttachment)
            .options(undefer(LabAttachment.data))
            .where(
                LabAttachment.id == attachment_id,
                LabAttachment.tenant_id == tenant_id,
                LabAttachment.lab_order_id == order_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("No such attachment.")
    return row


async def add_attachment(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    order_id: uuid.UUID,
    *,
    filename: str,
    content_type: str,
    data: bytes,
    note: str | None = None,
    actor_id: uuid.UUID,
) -> LabAttachment:
    """File a report against an order.

    A verified order is closed to new paper: verification is what released the
    result into the patient record, and adding to it afterwards would change
    what a clinician already read without anyone signing for it.
    """
    order = await get_order(session, tenant_id, order_id)
    if order.status == LabOrderStatus.VERIFIED:
        raise Conflict("That order is verified — its report can no longer be changed.")

    if not data:
        raise ValidationFailed("That file is empty.")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValidationFailed(
            f"Files are limited to {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.",
            details={"size_bytes": len(data), "limit_bytes": MAX_ATTACHMENT_BYTES},
        )

    kind = (content_type or "").split(";")[0].strip().lower() or "application/octet-stream"
    if kind not in ALLOWED_CONTENT_TYPES:
        raise ValidationFailed(
            "A lab report has to be a PDF, an image, or a plain-text or CSV file.",
            details={"content_type": kind},
        )

    attachment = LabAttachment(
        tenant_id=tenant_id,
        lab_order_id=order.id,
        filename=_safe_filename(filename),
        content_type=kind,
        size_bytes=len(data),
        note=note,
        uploaded_at=_now(),
        uploaded_by=actor_id,
        data=data,
    )
    session.add(attachment)
    await session.flush()
    return attachment


async def delete_attachment(
    session: AsyncSession, tenant_id: uuid.UUID, order_id: uuid.UUID, attachment_id: uuid.UUID
) -> None:
    order = await get_order(session, tenant_id, order_id)
    if order.status == LabOrderStatus.VERIFIED:
        raise Conflict("That order is verified — its report can no longer be changed.")
    attachment = await get_attachment(session, tenant_id, order_id, attachment_id)
    await session.delete(attachment)
    await session.flush()


def _safe_filename(name: str) -> str:
    """The name is a label, never a path.

    Nothing here writes to a disk, but the name is echoed back to a browser in
    a download header — so the parts that could steer a save dialogue, or split
    that header in two, are taken out at the door.
    """
    bare = (name or "").replace("\\", "/").split("/")[-1].strip()
    forbidden = {'"', "\r", "\n"}
    cleaned = "".join(ch for ch in bare if ch.isprintable() and ch not in forbidden)
    return (cleaned or "report")[:200]
