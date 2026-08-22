"""Engine, session factory, and the tenant context a session runs under.

The whole request is one transaction. `app.tenant_id` is set with `set_config
(..., is_local => true)` inside it, so PostgreSQL's row-level security policies
can see it, and it disappears when the transaction ends — a pooled connection
can never carry one clinic's identity into another clinic's request.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

TENANT_GUC = "app.tenant_id"

engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    pool_recycle=1800,
)

session_factory = async_sessionmaker(
    engine, expire_on_commit=False, autoflush=False, class_=AsyncSession
)


async def set_tenant(session: AsyncSession, tenant_id: uuid.UUID | None) -> None:
    """Bind this transaction to one tenant (or to none at all).

    The value is bound as a parameter — it is never interpolated into SQL — and
    it is transaction-local, so it cannot outlive the request.
    """
    value = str(tenant_id) if tenant_id else ""
    await session.execute(
        text("SELECT set_config(:guc, :value, true)"), {"guc": TENANT_GUC, "value": value}
    )
    session.info["tenant_id"] = tenant_id


async def current_tenant_guc(session: AsyncSession) -> str | None:
    row = await session.execute(text("SELECT current_setting(:guc, true)"), {"guc": TENANT_GUC})
    value = row.scalar()
    return value or None


@asynccontextmanager
async def tenant_session(tenant_id: uuid.UUID | None) -> AsyncIterator[AsyncSession]:
    """A standalone unit of work — for background jobs, the seeder, and tests."""
    async with session_factory() as session:
        async with session.begin():
            await set_tenant(session, tenant_id)
            yield session


async def dispose_engine() -> None:
    await engine.dispose()
