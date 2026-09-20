"""§3.12 — the tariff a practice admin keeps.

What a consultation, a procedure or a particular scan costs is a decision the
clinic makes, not a number in this code. Every row here belongs to one tenant,
so two practices on the same deployment can charge differently for the same
test, and one scan can cost something different from the next.

Billing reads through `price_for`, which answers with the tariff row or with
nothing — never with a guess. A line with no tariff behind it arrives at the
composer priced at zero and saying so, which is the honest answer to "what do
you charge for this?" when nobody has said yet.
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.core.permissions import Role
from app.models.enums import ServiceKind
from app.models.identity import User
from app.models.ops import ServicePrice
from app.models.tenancy import LabTest
from app.services import tenants as tenant_service

CENTS = Decimal("0.01")

#: The code a consultation is billed under. One per clinic, so it needs a
#: stable key rather than a name someone might retype.
CONSULTATION_CODE = "CONSULTATION"


def money(value: Decimal | float | int) -> Decimal:
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


def normalise_code(code: str) -> str:
    """Codes are matched, not read: upper case, no surrounding space."""
    return code.strip().upper()


async def list_prices(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    kind: ServiceKind | None = None,
    active_only: bool = False,
) -> list[ServicePrice]:
    stmt = select(ServicePrice).where(ServicePrice.tenant_id == tenant_id)
    if kind is not None:
        stmt = stmt.where(ServicePrice.kind == kind)
    if active_only:
        stmt = stmt.where(ServicePrice.active.is_(True))
    return list(
        (await session.execute(stmt.order_by(ServicePrice.kind, ServicePrice.name))).scalars()
    )


async def get_price(session: AsyncSession, tenant_id: uuid.UUID, price_id: uuid.UUID) -> ServicePrice:
    row = (
        await session.execute(
            select(ServicePrice).where(
                ServicePrice.id == price_id, ServicePrice.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("No such price.")
    return row


async def price_for(
    session: AsyncSession, tenant_id: uuid.UUID, *, kind: ServiceKind, code: str
) -> ServicePrice | None:
    """The tariff for one thing, or nothing if the clinic has not set it."""
    return (
        await session.execute(
            select(ServicePrice).where(
                ServicePrice.tenant_id == tenant_id,
                ServicePrice.kind == kind,
                ServicePrice.code == normalise_code(code),
                ServicePrice.active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def create_price(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    kind: ServiceKind,
    code: str,
    name: str,
    price: float,
    tax_pct: float,
    active: bool = True,
    note: str | None = None,
) -> ServicePrice:
    row = ServicePrice(
        tenant_id=tenant_id,
        kind=kind,
        code=normalise_code(code),
        name=name.strip(),
        price=money(price),
        tax_pct=money(tax_pct),
        active=active,
        note=note,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict(
            f"There is already a {kind.value} price under the code {normalise_code(code)}.",
            details={"kind": kind.value, "code": normalise_code(code)},
        ) from exc
    return row


async def update_price(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    price_id: uuid.UUID,
    changes: dict,
) -> ServicePrice:
    row = await get_price(session, tenant_id, price_id)
    for field in ("name", "note", "active"):
        if field in changes and changes[field] is not None:
            setattr(row, field, changes[field])
    if "price" in changes and changes["price"] is not None:
        row.price = money(changes["price"])
    if "tax_pct" in changes and changes["tax_pct"] is not None:
        row.tax_pct = money(changes["tax_pct"])
    await session.flush()
    return row


async def delete_price(session: AsyncSession, tenant_id: uuid.UUID, price_id: uuid.UUID) -> None:
    row = await get_price(session, tenant_id, price_id)
    await session.delete(row)
    await session.flush()


async def import_lab_catalogue(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    clinical_type: str,
    price: float,
    tax_pct: float,
) -> list[ServicePrice]:
    """Give every test this clinic can order a row to price.

    Filling the table is otherwise the tedious part of setting prices up: the
    catalogue is shared, the prices are not. Tests that already have a price
    are left exactly as they are — this only adds what is missing.
    """
    catalogue = (
        await session.execute(select(LabTest).order_by(LabTest.panel, LabTest.name))
    ).scalars()
    offered = [t for t in catalogue if not t.clinical_types or clinical_type in t.clinical_types]

    existing = {
        row.code
        for row in await list_prices(session, tenant_id, kind=ServiceKind.LAB)
    }

    created: list[ServicePrice] = []
    for test in offered:
        code = normalise_code(test.code)
        if code in existing:
            continue
        row = ServicePrice(
            tenant_id=tenant_id,
            kind=ServiceKind.LAB,
            code=code,
            name=test.name,
            price=money(price),
            tax_pct=money(tax_pct),
            active=True,
            note=test.panel or None,
        )
        session.add(row)
        created.append(row)
        existing.add(code)

    await session.flush()
    return created


async def resolve_consultation_price(
    session: AsyncSession, tenant_id: uuid.UUID, doctor_id: uuid.UUID
) -> tuple[Decimal, Decimal, bool]:
    """What this doctor's consultation costs: their own fee if they have one,
    otherwise the clinic's default tariff. Returns (price, tax_pct, priced) —
    `priced` is false only when neither exists, the same "say so rather than
    guess" convention every other tariff lookup follows."""
    config = await tenant_service.get_config(session, tenant_id)
    doctor = (
        await session.execute(
            select(User).where(User.id == doctor_id, User.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if doctor is not None and doctor.consultation_fee is not None:
        return money(doctor.consultation_fee), money(config.tax_pct), True

    default = await price_for(session, tenant_id, kind=ServiceKind.CONSULTATION, code=CONSULTATION_CODE)
    if default is not None:
        return money(default.price), money(default.tax_pct), True
    return money(0), money(config.tax_pct), False


async def set_doctor_fee(
    session: AsyncSession, tenant_id: uuid.UUID, doctor_id: uuid.UUID, fee: float | None
) -> User:
    doctor = (
        await session.execute(
            select(User).where(
                User.id == doctor_id, User.tenant_id == tenant_id, User.role == Role.DOCTOR.value
            )
        )
    ).scalar_one_or_none()
    if doctor is None:
        raise NotFound("No such clinician.")
    doctor.consultation_fee = money(fee) if fee is not None else None
    await session.flush()
    return doctor
