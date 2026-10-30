"""prescription lines carry a unit price, and may be prescribed only

- `prescription_items.unit_price` — what one unit bills at, as written on the
  prescription. Null when nobody priced it, and always on a prescribe-only line.
- `prescription_items.dispense` — false is "prescribe only": the patient buys
  it elsewhere, so the line is never dispensed, never moves stock and never
  becomes a charge. A check holds that to no price and nothing dispensed.
- `dispense_status` gains `prescribe_only`, for such a line and for a
  prescription with no other kind, so it never waits at the pharmacy counter.
- `tenant_configs.prescribe_only_default` — a clinic that does not sell
  medicine starts every new line as prescribe only.

Every existing line is dispensed here, as it always was, and takes its stock
line's current price where it has one (`BACKFILL_PRICES`) — what billing
would have charged for it anyway. A line typed by hand stays unpriced.

Revision ID: 0031_prescription_pricing
Revises: 0030_invoice_email
Create Date: 2026-10-30 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0031_prescription_pricing"
down_revision = "0030_invoice_email"
branch_labels = None
depends_on = None

#: Read by the test that a pre-upgrade line comes out priced: only a line
#: dispensed here, still unpriced, and tied to a stock line of its own clinic.
BACKFILL_PRICES = """
    UPDATE prescription_items AS p
       SET unit_price = i.unit_price
      FROM inventory_items AS i
     WHERE i.id = p.item_id
       AND i.tenant_id = p.tenant_id
       AND p.dispense
       AND p.unit_price IS NULL
"""


def upgrade() -> None:
    # A new enum value cannot be used in the transaction that adds it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE dispense_status ADD VALUE IF NOT EXISTS 'prescribe_only'")

    op.add_column("prescription_items", sa.Column("unit_price", sa.Numeric(12, 2), nullable=True))
    op.add_column(
        "prescription_items", sa.Column("dispense", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.add_column(
        "tenant_configs",
        sa.Column("prescribe_only_default", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # Both tables FORCE row-level security, so even their owner sees no rows
    # with no clinic bound; relaxed for a backfill meant to touch every clinic,
    # and put straight back.
    op.execute("ALTER TABLE prescription_items NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE inventory_items NO FORCE ROW LEVEL SECURITY")
    op.execute(BACKFILL_PRICES)
    op.execute("ALTER TABLE inventory_items FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE prescription_items FORCE ROW LEVEL SECURITY")

    op.create_check_constraint("ck_rx_item_unit_price", "prescription_items", "unit_price >= 0")
    op.create_check_constraint(
        "ck_rx_item_prescribe_only",
        "prescription_items",
        "dispense or (unit_price is null and dispensed_qty = 0)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_rx_item_prescribe_only", "prescription_items", type_="check")
    op.drop_constraint("ck_rx_item_unit_price", "prescription_items", type_="check")
    # PostgreSQL cannot drop an enum value, so `dispense_status` keeps
    # `prescribe_only`; its rows go back to plain `pending` — every line is
    # dispensed here again once the flag is gone.
    op.execute("ALTER TABLE prescription_items NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE prescriptions NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE prescription_items SET status = 'pending' WHERE status = 'prescribe_only'")
    op.execute("UPDATE prescriptions SET status = 'pending' WHERE status = 'prescribe_only'")
    op.execute("ALTER TABLE prescriptions FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE prescription_items FORCE ROW LEVEL SECURITY")
    op.drop_column("tenant_configs", "prescribe_only_default")
    op.drop_column("prescription_items", "dispense")
    op.drop_column("prescription_items", "unit_price")
