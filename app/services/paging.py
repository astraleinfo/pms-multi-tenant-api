"""One page of a query, and how many rows it matched.

The count runs over the same statement, under the same tenant filter and the
same row-level policy as the page itself, so a total can never describe rows
the caller may not read.

`limit=0` means *no page at all* — the whole collection. The screens that still
need every row (the appointment day grid, the rail's badge counts, a patient's
own history) pass it and get exactly what they got before paging existed; the
list screens pass a real limit and hold ten rows instead of ten thousand.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

#: What a caller passes when it genuinely wants the collection, not a page.
ALL = 0


async def page(
    session: AsyncSession, stmt: Select, *, limit: int = ALL, offset: int = 0
) -> tuple[list[Any], int]:
    """`(rows, total)` — the rows this page holds, and how many matched."""
    counted = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await session.execute(counted)).scalar() or 0)

    if limit:
        stmt = stmt.limit(limit).offset(offset)
    rows = list((await session.execute(stmt)).scalars())
    return rows, total


async def counts_by(session: AsyncSession, stmt: Select, column_name: str) -> dict[str, int]:
    """How many rows fall into each value of one column, for the tab counters.

    Built from the same filtered statement as the page, minus the filter on that
    column itself — which is what makes "Pending 12" true while the Dispensed tab
    is the one on screen.
    """
    sub = stmt.order_by(None).subquery()
    column = sub.c[column_name]
    rows = await session.execute(select(column, func.count()).select_from(sub).group_by(column))
    return {str(value): int(n) for value, n in rows}
