"""The audit trail.

Every state change and every authentication decision leaves a row. Writes are
best-effort in the sense that they never mask the real error, but they are part
of the same transaction as the change they describe, so the log cannot claim
something that was rolled back.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import set_tenant
from app.models.tenancy import AuditEvent


async def record(
    session: AsyncSession,
    *,
    action: str,
    tenant_id: uuid.UUID | None = None,
    actor_kind: str = "system",
    actor_id: uuid.UUID | None = None,
    actor_label: str = "",
    entity: str = "",
    entity_id: str = "",
    outcome: str = "ok",
    ip: str = "",
    user_agent: str = "",
    meta: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            actor_label=actor_label[:160],
            action=action[:80],
            entity=entity[:60],
            entity_id=str(entity_id)[:80],
            outcome=outcome[:20],
            ip=ip[:64],
            user_agent=user_agent[:300],
            meta=meta,
        )
    )


async def record_for_tenant(session: AsyncSession, tenant_id: uuid.UUID, **fields: Any) -> None:
    """Write a console action that names the clinic it touched.

    A platform session has no tenant bound, but the audit row does name one, so
    the clinic is bound for the length of the write and released again. The row
    then satisfies exactly the same policy as any other write — the console gets
    no special path through row-level security.
    """
    await set_tenant(session, tenant_id)
    await record(session, tenant_id=tenant_id, **fields)
    await session.flush()
    await set_tenant(session, None)
