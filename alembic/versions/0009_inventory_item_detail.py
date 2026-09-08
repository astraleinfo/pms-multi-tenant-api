"""inventory item detail fields

Adds manufacturer, description and HSN/tax code to inventory items, so the
detail drawer can show more than sku/form/strength/unit/category/pricing.

Revision ID: 0009_inventory_item_detail
Revises: 0008_charges
Create Date: 2026-09-08 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = '0009_inventory_item_detail'
down_revision = '0008_charges'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('inventory_items', sa.Column('manufacturer', sa.String(length=160), server_default='', nullable=False))
    op.add_column('inventory_items', sa.Column('description', sa.Text(), server_default='', nullable=False))
    op.add_column('inventory_items', sa.Column('hsn_code', sa.String(length=20), server_default='', nullable=False))


def downgrade() -> None:
    op.drop_column('inventory_items', 'hsn_code')
    op.drop_column('inventory_items', 'description')
    op.drop_column('inventory_items', 'manufacturer')
