"""Declarative base and the mixins that make tenancy structural.

Anything that inherits `TenantScoped` gets a non-nullable, indexed `tenant_id`,
is picked up by the ORM isolation guard, and gets a row-level security policy
generated for it in the migration. Forgetting the mixin is therefore not a
silent hole: the table simply is not part of the tenant world.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from uuid_utils.compat import uuid7


class Base(DeclarativeBase):
    pass


def uuid_pk() -> Mapped[uuid.UUID]:
    """UUIDv7: time-ordered, so a new row lands at the end of the primary-key
    index instead of a random page of it — what keeps inserts and the index
    size in check once a table runs to millions of rows. Rows made before the
    switch keep their v4 ids; the two mix freely."""
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TenantScoped:
    """Marker + column. Every tenant-owned row carries the tenant it belongs to."""

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(
            PGUUID(as_uuid=True),
            ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )

    @declared_attr.directive
    def __table_args__(cls) -> tuple:  # noqa: N805
        extra = getattr(cls, "__extra_table_args__", ())
        return (*extra, Index(f"ix_{cls.__tablename__}_tenant_id_id", "tenant_id", "id"))


def tenant_scoped_tables() -> list[str]:
    return sorted(
        m.class_.__tablename__
        for m in Base.registry.mappers
        if issubclass(m.class_, TenantScoped)
    )
