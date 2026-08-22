"""§3.9 — dispensing.

The pharmacy reads `Prescription` directly; nothing is copied into a pharmacy
table. Dispensing updates the prescription item and writes into the one
inventory ledger, in a single transaction, so stock and dispensed quantity can
never disagree.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.clinical import Prescription, PrescriptionItem
from app.models.enums import DispenseStatus, InventoryRefType, InventoryTxType
from app.schemas.clinical import DispenseIn
from app.services import encounters as encounter_service
from app.services import inventory as inventory_service


def _item_status(item: PrescriptionItem) -> DispenseStatus:
    if item.dispensed_qty >= item.qty:
        return DispenseStatus.DISPENSED
    if item.dispensed_qty > 0:
        return DispenseStatus.PARTIALLY_DISPENSED
    return DispenseStatus.PENDING


def _prescription_status(items: list[PrescriptionItem]) -> DispenseStatus:
    if items and all(i.status == DispenseStatus.DISPENSED for i in items):
        return DispenseStatus.DISPENSED
    if any(i.dispensed_qty > 0 for i in items):
        return DispenseStatus.PARTIALLY_DISPENSED
    return DispenseStatus.PENDING


async def dispense(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    prescription_id: uuid.UUID,
    payload: DispenseIn,
    *,
    actor_id: uuid.UUID,
) -> Prescription:
    prescription = await encounter_service.get_prescription(session, tenant_id, prescription_id)
    if prescription.status == DispenseStatus.DISPENSED:
        raise Conflict("Everything on that prescription has already been handed over.")

    by_id = {item.id: item for item in prescription.items}

    for line in payload.lines:
        try:
            line_item_id = uuid.UUID(line.item_id)
        except ValueError as exc:
            raise NotFound("No such prescription item.") from exc

        item = by_id.get(line_item_id)
        if item is None:
            raise NotFound("No such prescription item.")

        remaining = item.qty - item.dispensed_qty
        if line.qty > remaining:
            raise Conflict(
                f"{item.medicine} has only {remaining} left to dispense.",
                details={"item_id": str(item.id), "remaining": remaining},
            )

        if item.item_id is not None:
            # Drawing from stock is what makes this a pharmacy rather than a note.
            await inventory_service.record_movement(
                session,
                tenant_id,
                item_id=item.item_id,
                tx_type=InventoryTxType.DISPENSE,
                qty=line.qty,
                batch_no=line.batch_no,
                actor_id=actor_id,
                ref_type=InventoryRefType.PRESCRIPTION,
                ref_id=prescription.id,
            )

        item.dispensed_qty += line.qty
        item.status = _item_status(item)

    prescription.status = _prescription_status(list(prescription.items))
    await session.flush()
    return prescription


async def pending_prescriptions(
    session: AsyncSession, tenant_id: uuid.UUID, *, status: DispenseStatus | None = None
) -> list[Prescription]:
    stmt = select(Prescription).where(Prescription.tenant_id == tenant_id)
    if status is not None:
        stmt = stmt.where(Prescription.status == status)
    else:
        stmt = stmt.where(Prescription.status != DispenseStatus.DISPENSED)
    return list((await session.execute(stmt.order_by(Prescription.issued_at.desc()))).scalars())
