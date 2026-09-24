"""§3.11 — one ledger for every stock movement.

Stock on hand is `sum(qty)` over the ledger for that item in that tenant, never
a column someone can drift. Batch rows are a convenience for expiry and picking;
they are kept in step with the ledger inside the same transaction.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models.enums import InventoryCategory, InventoryRefType, InventoryTxType, PricingMode
from app.models.ops import InventoryBatch, InventoryItem, InventoryTransaction
from app.schemas.common import local_today

CENTS = Decimal("0.01")

# Which movements add stock and which take it away. `adjustment` is signed by
# the caller, because a stock-take can go either way.
_POSITIVE = {InventoryTxType.PURCHASE, InventoryTxType.RETURN}
_NEGATIVE = {InventoryTxType.DISPENSE, InventoryTxType.EXPIRY, InventoryTxType.TRANSFER}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _money(value: Decimal | float | int | None) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENTS, rounding=ROUND_HALF_UP)


def sale_price(
    mode: PricingMode,
    *,
    unit_price: Decimal | float | None,
    cost_price: Decimal | float | None,
    mrp: Decimal | float | None,
    margin_pct: Decimal | float | None,
) -> Decimal:
    """What one unit bills at, given how the clinic chose to price it.

    Three ways of saying the same kind of thing, and the caller picks one:

    *   `price`   — the figure typed is the figure charged.
    *   `percent` — cost plus a margin. 12.00 at 25% is 15.00.
    *   `mrp`     — the printed price, less `margin_pct` as a discount. 100.00
        at 10% is 90.00, and at 0% it is the MRP itself.

    Whichever is used, the answer lands in `unit_price`, so everything that
    bills for stock — the pharmacy, the composer, the ledger's valuation —
    keeps reading one column.
    """
    if mode == PricingMode.PERCENT:
        cost = _money(cost_price)
        return _money(cost + cost * _money(margin_pct) / Decimal(100))
    if mode == PricingMode.MRP:
        printed = _money(mrp)
        discounted = printed - printed * _money(margin_pct) / Decimal(100)
        return _money(max(discounted, Decimal(0)))
    return _money(unit_price)


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


async def create_item(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    sku: str,
    name: str,
    form: str = "",
    strength: str = "",
    unit: str = "unit",
    category: InventoryCategory,
    reorder_level: int = 0,
    unit_price: float = 0,
    cost_price: float = 0,
    mrp: float = 0,
    margin_pct: float = 0,
    pricing_mode: PricingMode = PricingMode.PRICE,
    manufacturer: str = "",
    description: str = "",
    hsn_code: str = "",
) -> InventoryItem:
    """Open a new stock line.

    The line exists before any of it does: an item with no ledger rows is at
    zero on hand, which is exactly what a shelf that has been labelled but not
    filled looks like. Stock arrives afterwards, as a purchase.
    """
    item = InventoryItem(
        tenant_id=tenant_id,
        sku=sku.strip().upper(),
        name=name.strip(),
        form=form.strip(),
        strength=strength.strip(),
        unit=unit.strip() or "unit",
        category=category,
        reorder_level=reorder_level,
        cost_price=_money(cost_price),
        mrp=_money(mrp),
        margin_pct=_money(margin_pct),
        pricing_mode=pricing_mode,
        manufacturer=manufacturer.strip(),
        description=description.strip(),
        hsn_code=hsn_code.strip(),
    )
    item.unit_price = sale_price(
        pricing_mode,
        unit_price=unit_price,
        cost_price=item.cost_price,
        mrp=item.mrp,
        margin_pct=item.margin_pct,
    )
    session.add(item)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict(
            f"There is already a stock line with the code {item.sku}.",
            details={"sku": item.sku},
        ) from exc
    return item


async def update_item(
    session: AsyncSession, tenant_id: uuid.UUID, item_id: uuid.UUID, changes: dict
) -> InventoryItem:
    """Edit a stock line, re-deriving the selling price from whatever the
    pricing mode now says it should be."""
    item = await get_item(session, tenant_id, item_id)

    for field in ("name", "form", "strength", "manufacturer", "description", "hsn_code"):
        if changes.get(field) is not None:
            setattr(item, field, str(changes[field]).strip())
    if changes.get("unit") is not None:
        # A stock line is always counted in something. Blanking the box falls
        # back rather than leaving "12 " on every screen that quotes a level.
        item.unit = str(changes["unit"]).strip() or "unit"
    if changes.get("sku") is not None:
        item.sku = str(changes["sku"]).strip().upper()
    if changes.get("category") is not None:
        item.category = changes["category"]
    if changes.get("reorder_level") is not None:
        item.reorder_level = int(changes["reorder_level"])
    for field in ("cost_price", "mrp", "margin_pct"):
        if changes.get(field) is not None:
            setattr(item, field, _money(changes[field]))
    if changes.get("pricing_mode") is not None:
        item.pricing_mode = changes["pricing_mode"]

    # An explicit null is not a price of zero: it means "leave this alone".
    typed = changes.get("unit_price")
    item.unit_price = sale_price(
        PricingMode(item.pricing_mode),
        unit_price=item.unit_price if typed is None else typed,
        cost_price=item.cost_price,
        mrp=item.mrp,
        margin_pct=item.margin_pct,
    )

    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict(
            f"There is already a stock line with the code {item.sku}.",
            details={"sku": item.sku},
        ) from exc
    return item


def apply_purchase_pricing(
    item: InventoryItem,
    *,
    cost_price: float | None,
    mrp: float | None,
    margin_pct: float | None,
    unit_price: float | None,
    pricing_mode: PricingMode | None,
) -> None:
    """Re-price a line from what a delivery actually cost.

    A purchase is the moment the numbers on the supplier's invoice are in front
    of somebody, so it is the moment worth capturing them. Nothing is touched
    unless it was sent — a movement recorded with no pricing on it leaves the
    line priced exactly as it was.
    """
    sent = (cost_price, mrp, margin_pct, unit_price, pricing_mode)
    if all(value is None for value in sent):
        return

    if cost_price is not None:
        item.cost_price = _money(cost_price)
    if mrp is not None:
        item.mrp = _money(mrp)
    if margin_pct is not None:
        item.margin_pct = _money(margin_pct)
    if pricing_mode is not None:
        item.pricing_mode = pricing_mode

    item.unit_price = sale_price(
        PricingMode(item.pricing_mode),
        unit_price=item.unit_price if unit_price is None else unit_price,
        cost_price=item.cost_price,
        mrp=item.mrp,
        margin_pct=item.margin_pct,
    )


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
    cost_price: float | None = None,
    mrp: float | None = None,
    margin_pct: float | None = None,
    unit_price: float | None = None,
    pricing_mode: PricingMode | None = None,
) -> InventoryTransaction:
    """Write one ledger row and move the batch with it.

    Refuses to take more out than the batch holds — a dispense cannot invent
    stock, and the check happens under a row lock so two tills cannot both take
    the last box.

    A movement may also carry what the delivery cost and what it should sell
    for; see `apply_purchase_pricing`. Sending none of it leaves the line's
    price alone, so the pharmacy dispensing against a prescription never
    disturbs what the item is worth.
    """
    item = await get_item(session, tenant_id, item_id)
    apply_purchase_pricing(
        item,
        cost_price=cost_price,
        mrp=mrp,
        margin_pct=margin_pct,
        unit_price=unit_price,
        pricing_mode=pricing_mode,
    )

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
            expiry=expiry or (local_today() + timedelta(days=365 * 2)),
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
