"""The ORM-level half of tenant isolation (step 2 of the middleware stack).

PostgreSQL row-level security is the enforcement. This layer sits above it so a
mistake fails loudly in the application instead of quietly returning nothing:

  * a query touching a tenant-owned table on a session with no tenant bound
    raises rather than running;
  * every such query gets `tenant_id = :current` appended, whatever the caller
    wrote;
  * a flush that would write, change, or delete another tenant's row raises.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import event, orm
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from app.core.errors import TenantIsolationViolation
from app.db.base import TenantScoped

log = logging.getLogger("pms.isolation")


def _session_tenant(session: Session) -> uuid.UUID | None:
    value = session.info.get("tenant_id")
    return value if isinstance(value, uuid.UUID) else None


def _touches_tenant_scoped(state: ORMExecuteState) -> bool:
    for mapper in state.all_mappers:
        if issubclass(mapper.class_, TenantScoped):
            return True
    return False


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_filter(state: ORMExecuteState) -> None:
    if state.is_column_load or state.is_relationship_load:
        # Loading an attribute of a row that already passed the filter.
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if not _touches_tenant_scoped(state):
        return

    tenant_id = _session_tenant(state.session)
    if tenant_id is None:
        raise TenantIsolationViolation(
            "a query against tenant-owned tables ran on a session with no tenant bound"
        )

    state.statement = state.statement.options(
        with_loader_criteria(
            TenantScoped,
            lambda cls: cls.tenant_id == tenant_id,
            include_aliases=True,
            propagate_to_loaders=True,
        )
    )


@event.listens_for(Session, "before_flush")
def _guard_flush(session: Session, _flush_context: object, _instances: object) -> None:
    tenant_id = _session_tenant(session)

    def check(obj: object, what: str) -> None:
        if not isinstance(obj, TenantScoped):
            return
        if tenant_id is None:
            raise TenantIsolationViolation(f"{what} {type(obj).__name__} with no tenant bound to the session")
        own = getattr(obj, "tenant_id", None)
        if own is None:
            # Stamped server-side, always — a client-supplied tenant_id never reaches here.
            obj.tenant_id = tenant_id
            return
        if own != tenant_id:
            raise TenantIsolationViolation(
                f"{what} {type(obj).__name__} belonging to tenant {own} from a session bound to {tenant_id}"
            )

    for obj in session.new:
        check(obj, "insert of")
    for obj in session.dirty:
        if session.is_modified(obj, include_collections=False):
            check(obj, "update of")
    for obj in session.deleted:
        check(obj, "delete of")


def assert_same_tenant(session: Session | orm.Session, *rows: object) -> None:
    """Belt-and-braces for hand-written joins: every row must belong to the
    session's tenant. Used where a service loads several entities and links
    them together."""
    tenant_id = _session_tenant(session)
    for row in rows:
        if isinstance(row, TenantScoped) and row.tenant_id != tenant_id:
            raise TenantIsolationViolation(
                f"{type(row).__name__} {getattr(row, 'id', '?')} is outside the bound tenant"
            )
