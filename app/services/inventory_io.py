"""§3.11 — stock lines in and out of a spreadsheet.

A clinic moving from another system has a price list and a shelf count, not
the time to type either in. Three routes, on the same rules as the console's
onboarding import (`app/services/imports.py`):

*   **The sample sheet.** Every field a stock line has, with the fixed choices
    as dropdowns, three worked examples (one per pricing mode) and a second
    sheet that says what each column is.
*   **The import.** Checked row by row and reported before anything is
    written, then written all at once or not at all. A row is matched on its
    SKU: a code the clinic already has updates that line, a new one opens it.
    On an update a blank cell leaves the field as it is — a sheet that only
    carries new prices must not wipe every manufacturer.
*   **The export.** The same columns, filled in from the clinic's own lines,
    plus what is on hand and a sheet of batches. It goes straight back in as
    an import, so "export, edit the prices, import" is a bulk edit.

The import runs in the Celery worker, not the request — see
`app/services/import_jobs.py`, which stores the upload, runs `process` below
and deletes the file either way.

Stock arrives the only way it ever does: as a purchase on the ledger, into a
named batch with an expiry. The sheet's quantity is stock *received*, never a
level to set, which is why re-importing an export adds nothing — its quantity
column is empty.
"""

from __future__ import annotations

import io
import uuid
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    InventoryCategory,
    InventoryRefType,
    InventoryTxType,
    PricingMode,
)
from app.models.ops import InventoryBatch, InventoryItem
from app.models.tenancy import Tenant
from app.schemas.common import zone
from app.schemas.ops import InventoryItemIn, InventoryItemPatch
from app.services import audit, import_jobs
from app.services import inventory as inventory_service
from app.services.imports import Column, RowResult, _blank, _date, _text, build_template

KIND = "inventory"

_CATEGORIES = tuple(c.value for c in InventoryCategory)
_MODES = tuple(m.value for m in PricingMode)

COLUMNS: tuple[Column, ...] = (
    Column("sku", "SKU", True,
           "Your own code for the line, unique in the clinic. A code you already have updates that "
           "line; a new one adds it. Stored in capitals.", "AMOX-500"),
    Column("name", "Item name", True, "May be left blank when updating a line you already have.",
           "Amoxicillin"),
    Column("category", "Category", True,
           "One of: " + ", ".join(_CATEGORIES) + ". May be left blank when updating.",
           "medicine", _CATEGORIES),
    Column("form", "Form", False, "Tablet, capsule, syrup, box…", "Capsule"),
    Column("strength", "Strength", False, "", "500 mg"),
    Column("unit", "Unit", False, "What stock is counted in. Blank means 'unit'.", "capsule"),
    Column("reorder_level", "Reorder level", False,
           "A whole number. The line shows as low at or below it.", 100),
    Column("pricing_mode", "Pricing mode", False,
           "How the selling price is worked out. price: the selling price as typed. "
           "percent: cost price plus margin %. mrp: MRP less margin % as a discount. Blank means price.",
           "percent", _MODES),
    Column("unit_price", "Selling price", False,
           "What one unit bills at. Used when the pricing mode is price; worked out for you otherwise.", ""),
    Column("cost_price", "Cost price", False, "What one unit costs to buy. Needed for percent.", 4.5),
    Column("mrp", "MRP", False, "The printed maximum retail price. Needed for mrp.", ""),
    Column("margin_pct", "Margin %", False,
           "Markup over cost for percent; discount off MRP for mrp. From -100 to 1000.", 25),
    Column("manufacturer", "Manufacturer", False, "", "Cipla"),
    Column("hsn_code", "HSN code", False, "The tax classification code.", "3004"),
    Column("description", "Description", False, "", "Broad-spectrum antibiotic"),
    Column("qty", "Quantity received", False,
           "Stock to add now, as a purchase. A whole number. Leave blank to only add or update the line.",
           200),
    Column("batch_no", "Batch number", False, "Needed with a quantity received.", "AMX2409"),
    Column("expiry", "Expiry date", False,
           "Needed with a quantity received. YYYY-MM-DD, or a date cell. DD/MM/YYYY also works.",
           "2027-06-30"),
)

#: the fields of a stock line itself, as opposed to the stock arriving on it
_TEXT_FIELDS = ("name", "form", "strength", "unit", "manufacturer", "hsn_code", "description")
_MONEY_FIELDS = ("unit_price", "cost_price", "mrp", "margin_pct")
_LABEL = {c.key: c.label for c in COLUMNS}

#: one row per pricing mode, so the sheet shows every way of filling it in
EXAMPLES: list[dict[str, Any]] = [
    {c.key: c.example for c in COLUMNS},
    {
        "sku": "PARA-650", "name": "Paracetamol", "category": "medicine", "form": "Tablet",
        "strength": "650 mg", "unit": "tablet", "reorder_level": 200, "pricing_mode": "mrp",
        "mrp": 2, "margin_pct": 10, "manufacturer": "Micro Labs", "hsn_code": "3004",
        "qty": 500, "batch_no": "PCM2411", "expiry": "2027-03-31",
    },
    {
        "sku": "GLV-NIT-M", "name": "Nitrile gloves (M)", "category": "consumable", "form": "Box of 100",
        "unit": "box", "reorder_level": 5, "pricing_mode": "price", "unit_price": 450,
        "cost_price": 380, "manufacturer": "Kimberly-Clark", "hsn_code": "4015",
        "qty": 20, "batch_no": "GLV-0925", "expiry": "2028-09-30",
    },
]

NOTES = (
    "A row is matched on its SKU: one you already have is updated, a new one is added.",
    "When updating, a blank cell leaves that field as it is.",
    "Quantity received is stock arriving, recorded as a purchase — not the level to set.",
    "An export from the Inventory page is in this same layout, so it can be edited and imported back.",
)


def template(*, clinic: str) -> bytes:
    return build_template(KIND, COLUMNS, clinic=clinic, examples=EXAMPLES, notes=NOTES)


# --- checking rows ----------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if _blank(value):
        return None
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, (int, float)):
        return float(value)
    return float(_text(value).replace(",", "").removesuffix("%").strip())


def _whole(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    if not number.is_integer():
        raise ValueError("not a whole number")
    return int(number)


async def check_rows(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    rows: list[tuple[int, dict[str, Any]]],
    *,
    today: date,
) -> list[RowResult]:
    """Each row turned into what the item form and the movement form would
    have sent, and checked the way they are checked. Nothing is written."""
    existing = {item.sku: item for item in await inventory_service.list_items(session, tenant_id)}
    batches = {
        (b.item_id, b.batch_no): b for b in await inventory_service.list_batches(session, tenant_id)
    }
    # The same product under two codes is the duplicate a SKU check cannot
    # see: a new code for an item already on the shelf would open a second
    # line, and stock would split between them.
    stocked_as = {_identity(i.name, i.form, i.strength): i.sku for i in existing.values()}
    seen: dict[str, int] = {}
    seen_as: dict[tuple[str, str, str], tuple[int, str]] = {}
    results = []

    for number, values in rows:
        sku = _text(values.get("sku")).upper()
        current = existing.get(sku)
        name = _text(values.get("name")) or (current.name if current else "")
        result = RowResult(row=number, label=" — ".join(p for p in (sku, name) if p) or "(no SKU)")
        errors = result.errors

        duplicate_of: str | None = None
        if not sku:
            errors.append("SKU is required.")
        elif sku in seen:
            duplicate_of = f"row {seen[sku]}"
            errors.append(f"Duplicate: {sku} is also on row {seen[sku]}.")
        seen.setdefault(sku, number)

        fields: dict[str, Any] = {"sku": sku} if sku else {}
        for key in _TEXT_FIELDS:
            text = _text(values.get(key))
            if text:
                fields[key] = text

        if name and sku and duplicate_of is None:
            ident = _identity(
                name,
                fields.get("form", current.form if current else ""),
                fields.get("strength", current.strength if current else ""),
            )
            other = stocked_as.get(ident)
            if other == sku:
                pass  # the line itself, being updated — never a duplicate of its own copies
            elif other:
                duplicate_of = other
                errors.append(
                    f"Duplicate: the same item is already in stock as {other}. Use that SKU to update it."
                )
            elif ident in seen_as and seen_as[ident][1] != sku:
                at, code = seen_as[ident]
                duplicate_of = f"row {at}"
                errors.append(f"Duplicate: the same item is on row {at} as {code}.")
            seen_as.setdefault(ident, (number, sku))

        category = _text(values.get("category")).lower()
        if category:
            if category in _CATEGORIES:
                fields["category"] = category
            else:
                errors.append(f"Category must be one of {', '.join(_CATEGORIES)} — '{category}' is not.")
        elif current is None:
            errors.append("Category is required for a new item.")

        mode = _text(values.get("pricing_mode")).lower()
        if mode:
            if mode in _MODES:
                fields["pricing_mode"] = mode
            else:
                errors.append(f"Pricing mode must be one of {', '.join(_MODES)} — '{mode}' is not.")

        for key in _MONEY_FIELDS:
            try:
                value = _number(values.get(key))
            except ValueError:
                errors.append(f"{_LABEL[key]} '{_text(values.get(key))}' is not a number.")
                continue
            if value is not None:
                fields[key] = value
        try:
            level = _whole(values.get("reorder_level"))
        except ValueError:
            errors.append(f"Reorder level '{_text(values.get('reorder_level'))}' is not a whole number.")
        else:
            if level is not None:
                fields["reorder_level"] = level

        # The same bounds as the item form: a new line through the create
        # model, an existing one through the edit model.
        if current is None and not name:
            errors.append("Item name is required for a new item.")
        model = InventoryItemIn if current is None else InventoryItemPatch
        if sku and (current is not None or (name and "category" in fields)):
            try:
                model(**fields)
            except ValidationError as exc:
                for problem in exc.errors():
                    key = str(problem["loc"][0]) if problem["loc"] else ""
                    errors.append(f"{_LABEL.get(key, key)}: {problem['msg'].removeprefix('Value error, ')}")

        # A price worked out from a figure the row leaves at nothing is a
        # price of nothing. Say so, rather than import a line that bills 0.00.
        effective_mode = fields.get("pricing_mode") or (current.pricing_mode.value if current else "price")
        if effective_mode == "percent" and not fields.get("cost_price", current.cost_price if current else 0):
            errors.append("Pricing mode percent needs a cost price.")
        if effective_mode == "mrp" and not fields.get("mrp", current.mrp if current else 0):
            errors.append("Pricing mode mrp needs an MRP.")

        stock = _check_stock(values, errors, today=today, current=current, batches=batches)

        result.data = {
            "item_id": current.id if current else None,
            "fields": fields,
            "stock": stock,
            "duplicate_of": duplicate_of,
        }
        results.append(result)
    return results


def _identity(name: str, form: str, strength: str) -> tuple[str, str, str]:
    """What makes two lines the same product, whatever they are coded: the
    name, form and strength, ignoring case and spacing."""
    return tuple(" ".join(str(part).lower().split()) for part in (name, form, strength))  # type: ignore[return-value]


def _check_stock(
    values: dict[str, Any],
    errors: list[str],
    *,
    today: date,
    current: InventoryItem | None,
    batches: dict[tuple[uuid.UUID, str], InventoryBatch],
) -> dict[str, Any] | None:
    """The stock a row brings with it, if any: a quantity, into a batch, with
    an expiry. All three or none."""
    try:
        qty = _whole(values.get("qty"))
    except ValueError:
        errors.append(f"Quantity received '{_text(values.get('qty'))}' is not a whole number.")
        return None
    batch_no = _text(values.get("batch_no"))
    try:
        expiry = _date(values.get("expiry"))
    except ValueError:
        errors.append(f"Expiry date '{_text(values.get('expiry'))}' is not a date.")
        return None

    if not qty:
        if qty is not None:
            errors.append("Quantity received must be more than 0 — leave it blank to add no stock.")
        elif batch_no or expiry:
            errors.append("A batch number or expiry is given but no quantity received.")
        return None
    if qty < 0:
        errors.append("Quantity received cannot be negative. Record a write-off as a stock movement.")
        return None
    if qty > 1_000_000:
        errors.append("Quantity received is more than 1,000,000.")
        return None
    if not batch_no:
        errors.append("Batch number is needed with a quantity received.")
    elif len(batch_no) > 60:
        errors.append("Batch number is longer than 60 characters.")
    if expiry is None:
        errors.append("Expiry date is needed with a quantity received.")
    elif expiry < today:
        errors.append(f"Expiry date {expiry.isoformat()} has already passed.")

    # Receiving more of a batch already on the shelf is normal; receiving it
    # under a different expiry means one of the two dates is wrong.
    shelf = batches.get((current.id, batch_no)) if current is not None else None
    if shelf is not None and expiry is not None and shelf.expiry != expiry:
        errors.append(
            f"Batch {batch_no} is already on the shelf with expiry {shelf.expiry.isoformat()}, "
            f"not {expiry.isoformat()}."
        )
    return {"qty": qty, "batch_no": batch_no, "expiry": expiry}


# --- writing ------------------------------------------------------------------------------


async def commit_rows(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    results: list[RowResult],
    *,
    actor_id: uuid.UUID | None,
    source: str,
) -> dict[str, int]:
    """Write every row. The caller has checked that every row passed; the
    request's one transaction makes it all or nothing."""
    created = updated = stocked = 0
    for r in results:
        # Through the same input models the item form uses, so the service
        # is handed exactly what the form would have handed it.
        fields = r.data["fields"]
        if r.data["item_id"] is None:
            payload = InventoryItemIn(**fields).model_dump()
            item = await inventory_service.create_item(session, tenant_id, **payload)
            created += 1
        else:
            changes = InventoryItemPatch(**{k: v for k, v in fields.items() if k != "sku"})
            item = await inventory_service.update_item(
                session, tenant_id, r.data["item_id"], changes.model_dump(exclude_unset=True)
            )
            updated += 1

        stock = r.data["stock"]
        if stock:
            await inventory_service.record_movement(
                session,
                tenant_id,
                item_id=item.id,
                tx_type=InventoryTxType.PURCHASE,
                qty=stock["qty"],
                batch_no=stock["batch_no"],
                expiry=stock["expiry"],
                actor_id=actor_id,
                ref_type=InventoryRefType.MANUAL,
                note=f"Imported from {source}"[:500],
            )
            stocked += 1
    return {"created": created, "updated": updated, "stocked": stocked}


# --- the export ---------------------------------------------------------------------------


def export(
    items: list[InventoryItem],
    levels: dict[uuid.UUID, int],
    batches: list[InventoryBatch],
    *,
    today: date,
    alert_days: int,
) -> bytes:
    """The clinic's stock lines in the import's own layout, with what is on
    hand after them, and a second sheet of the batches on the shelf."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    book = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="2F6BF3")
    info_fill = PatternFill("solid", fgColor="4B5563")

    sheet = book.active
    sheet.title = "Items"
    info = ("On hand", "Low stock", "Stock value at cost")
    headers = [f"{c.label}{' *' if c.required else ''}" for c in COLUMNS] + list(info)
    sheet.append(headers)
    for i, cell in enumerate(sheet[1]):
        cell.font = head_font
        # the columns after the import's own are read-only information: an
        # import ignores a header it does not know
        cell.fill = info_fill if i >= len(COLUMNS) else head_fill

    for item in items:
        on_hand = levels.get(item.id, 0)
        row = {
            "sku": item.sku, "name": item.name, "category": item.category.value, "form": item.form,
            "strength": item.strength, "unit": item.unit, "reorder_level": item.reorder_level,
            "pricing_mode": item.pricing_mode.value, "unit_price": float(item.unit_price),
            "cost_price": float(item.cost_price), "mrp": float(item.mrp),
            "margin_pct": float(item.margin_pct), "manufacturer": item.manufacturer,
            "hsn_code": item.hsn_code, "description": item.description,
        }
        sheet.append(
            [row.get(c.key) for c in COLUMNS]
            + [on_hand, "yes" if on_hand <= item.reorder_level else "", float(item.cost_price) * on_hand]
        )

    for i, header in enumerate(headers, start=1):
        letter = sheet.cell(row=1, column=i).column_letter
        sheet.column_dimensions[letter].width = min(max(len(header) + 4, 14), 40)
    sheet.freeze_panes = "B2"

    shelf = book.create_sheet("Batches")
    shelf.append(["SKU", "Item name", "Batch number", "Expiry date", "On hand", "Status"])
    for cell in shelf[1]:
        cell.font = head_font
        cell.fill = head_fill
    by_id = {item.id: item for item in items}
    for batch in sorted(batches, key=lambda b: b.expiry):
        item = by_id.get(batch.item_id)
        if item is None or batch.qty <= 0:
            continue
        days = (batch.expiry - today).days
        status = "expired" if days < 0 else "expiring soon" if days <= alert_days else ""
        shelf.append([item.sku, item.name, batch.batch_no, batch.expiry, batch.qty, status])
        shelf.cell(row=shelf.max_row, column=4).number_format = "yyyy-mm-dd"
    for letter, width in zip("ABCDEF", (16, 32, 18, 14, 10, 16), strict=True):
        shelf.column_dimensions[letter].width = width
    shelf.freeze_panes = "A2"

    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


# --- the import, as the worker runs it ------------------------------------------------------


def _row(r: RowResult) -> dict[str, Any]:
    return {
        "row": r.row, "label": r.label, "ok": r.ok, "errors": r.errors,
        "action": "update" if r.data["item_id"] else "create",
        "qty": (r.data["stock"] or {}).get("qty", 0),
        "duplicate_of": r.data["duplicate_of"],
    }


async def process(
    session: AsyncSession,
    tenant: Tenant,
    rows: list[tuple[int, dict[str, Any]]],
    *,
    filename: str,
    actor: import_jobs.Actor,
) -> dict[str, Any]:
    """Check every row and write them all, or raise `Rejected` with the rows
    that need fixing. Runs in the worker, in one transaction."""
    today = datetime.now(zone(tenant.timezone)).date()
    results = await check_rows(session, tenant.id, rows, today=today)
    invalid = [r for r in results if not r.ok]
    report: dict[str, Any] = {
        "total": len(results), "valid": len(results) - len(invalid), "invalid": len(invalid),
        "duplicates": sum(1 for r in invalid if r.data["duplicate_of"]),
    }
    if invalid:
        raise import_jobs.Rejected({**report, "rows": [_row(r) for r in invalid]})

    counts = await commit_rows(session, tenant.id, results, actor_id=actor.id, source=filename)
    await audit.record(
        session, action="inventory.import", tenant_id=tenant.id,
        actor_kind=actor.kind, actor_id=actor.id, actor_label=actor.label,
        entity="import", entity_id=filename, meta={"rows": len(results), **counts},
    )
    return {**report, **counts, "rows": []}
