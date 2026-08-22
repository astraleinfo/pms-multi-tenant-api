"""§3.11 — one ledger for every stock movement.

Stock on hand is `sum(qty)` over the ledger for that item in that tenant, never
a column someone can drift. Batch rows are a convenience for expiry and picking;
they are kept in step with the ledger inside the same transaction.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.enums import InventoryRefType, InventoryTxType
from app.models.ops import InventoryBatch, InventoryItem, InventoryTransaction

# Which movements add stock and which take it away. `adjustment` is signed by
# the caller, because a stock-take can go either way.
_POSITIVE = {InventoryTxType.PURCHASE, InventoryTxType.RETURN}
_NEGATIVE = {InventoryTxType.DISPENSE, InventoryTxType.EXPIRY, InventoryTxType.TRANSFER}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def list_items(session: AsyncSession, tenant_id: uuid.UUID) -> list[InventoryItem]:
    return list(
        (
            await session.execute(
                select(InventoryItem).where(InventoryItem.tenant_id == tenant_id).order_by(InventoryItem.name)
            )
        ).scalars()
    )


async def get_item(session: AsyncSession, tenant_id: uuid.UUID, item_id: uuid.UUID) -> InventoryItem:
    row = (
        await session.execute(
            select(InventoryItem).where(
                InventoryItem.id == item_id, InventoryItem.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("No such stock item.")
    return row


async def list_batches(session: AsyncSession, tenant_id: uuid.UUID) -> list[InventoryBatch]:
    return list(
        (
            await session.execute(
                select(InventoryBatch)
                .where(InventoryBatch.tenant_id == tenant_id)
                .order_by(InventoryBatch.expiry)
            )
        ).scalars()
    )


async def list_transactions(
    session: AsyncSession, tenant_id: uuid.UUID, *, item_id: uuid.UUID | None = None, limit: int = 500
) -> list[InventoryTransaction]:
    stmt = select(InventoryTransaction).where(InventoryTransaction.tenant_id == tenant_id)
    if item_id is not None:
        stmt = stmt.where(InventoryTransaction.item_id == item_id)
    return list((await session.execute(stmt.order_by(InventoryTransaction.at.desc()).limit(limit))).scalars())


async def stock_level(session: AsyncSession, tenant_id: uuid.UUID, item_id: uuid.UUID) -> int:
    total = (
        await session.execute(
            select(func.coalesce(func.sum(InventoryTransaction.qty), 0)).where(
                InventoryTransaction.tenant_id == tenant_id, InventoryTransaction.item_id == item_id
            )
        )
    ).scalar()
    return int(total or 0)


async def stock_levels(session: AsyncSession, tenant_id: uuid.UUID) -> dict[uuid.UUID, int]:
    rows = await session.execute(
        select(InventoryTransaction.item_id, func.coalesce(func.sum(InventoryTransaction.qty), 0))
        .where(InventoryTransaction.tenant_id == tenant_id)
        .group_by(InventoryTransaction.item_id)
    )
    return {item_id: int(total or 0) for item_id, total in rows.all()}


async def near_expiry(
    session: AsyncSession, tenant_id: uuid.UUID, *, today: date, within_days: int = 90
) -> list[InventoryBatch]:
    cutoff = today + timedelta(days=within_days)
    return list(
        (
            await session.execute(
                select(InventoryBatch)
                .where(
                    InventoryBatch.tenant_id == tenant_id,
                    InventoryBatch.expiry <= cutoff,
                    InventoryBatch.qty > 0,
                )
                .order_by(InventoryBatch.expiry)
            )
        ).scalars()
    )


async def record_movement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    item_id: uuid.UUID,
    tx_type: InventoryTxType,
    qty: int,
    batch_no: str,
    actor_id: uuid.UUID | None,
    ref_type: InventoryRefType | None = InventoryRefType.MANUAL,
    ref_id: uuid.UUID | None = None,
    note: str | None = None,
    expiry: date | None = None,
    allow_negative: bool = False,
) -> InventoryTransaction:
    """Write one ledger row and move the batch with it.

    Refuses to take more out than the batch holds — a dispense cannot invent
    stock, and the check happens under a row lock so two tills cannot both take
    the last box.
    """
    item = await get_item(session, tenant_id, item_id)

    if tx_type in _POSITIVE:
        signed = abs(qty)
    elif tx_type in _NEGATIVE:
        signed = -abs(qty)
    else:  # adjustment — the caller's sign is meaningful
        signed = qty
    if signed == 0:
        raise Conflict("A stock movement of zero changes nothing.")

    batch = (
        await session.execute(
            select(InventoryBatch)
            .where(
                InventoryBatch.tenant_id == tenant_id,
                InventoryBatch.item_id == item.id,
                InventoryBatch.batch_no == batch_no,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    if batch is None:
        if signed < 0 and not allow_negative:
            raise Conflict(f"Batch {batch_no} of {item.name} does not exist.")
        batch = InventoryBatch(
            tenant_id=tenant_id,
            item_id=item.id,
            batch_no=batch_no,
            expiry=expiry or (date.today() + timedelta(days=365 * 2)),
            qty=0,
        )
        session.add(batch)
        await session.flush()

    if signed < 0 and batch.qty + signed < 0 and not allow_negative:
        raise Conflict(
            f"Only {batch.qty} of batch {batch_no} left.",
            details={"item_id": str(item.id), "batch_no": batch_no, "available": batch.qty},
        )

    batch.qty = max(0, batch.qty + signed)
    if expiry is not None:
        batch.expiry = expiry

    transaction = InventoryTransaction(
        tenant_id=tenant_id,
        item_id=item.id,
        type=tx_type,
        qty=signed,
        batch_no=batch_no,
        at=_now(),
        ref_type=ref_type,
        ref_id=ref_id,
        note=note,
        actor_id=actor_id,
    )
    session.add(transaction)
    await session.flush()
    return transaction
