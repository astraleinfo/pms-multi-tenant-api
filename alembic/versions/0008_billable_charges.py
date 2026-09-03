"""an invoice line remembers what it bills for

A charge is outstanding until a line points at it. Without that, the only way to
know whether the medicine handed over this morning has been paid for is to read
descriptions and guess — and a second dispense, or a second visit to the desk,
bills it twice.

`ref_id` names the row the line bills: a prescription item for a `pharmacy`
line, a lab order for a `lab` line, an encounter for a `consultation` one.
`source` already says which of those it is, so one column is enough.

Existing lines get NULL, which is honest: they were composed by hand before this
existed and point at nothing in particular. They are ignored when working out
what is still unbilled, so a clinic upgrading mid-day will see the visit's
charges offered again — once — rather than silently swallowed.

Revision ID: 0008_charges
Revises: 0007_pricing
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008_charges"
down_revision = "0007_pricing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_line_items",
        sa.Column("ref_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # The question asked of this column is always "has anything billed this
    # row yet", within one clinic.
    op.create_index(
        "ix_invoice_line_items_tenant_ref", "invoice_line_items", ["tenant_id", "ref_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_invoice_line_items_tenant_ref", table_name="invoice_line_items")
    op.drop_column("invoice_line_items", "ref_id")
