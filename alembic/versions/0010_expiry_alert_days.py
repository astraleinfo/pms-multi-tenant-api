"""expiry alert threshold

A tenant-configurable "how many days before expiry do we start warning"
setting, replacing the hardcoded 45/120-day literals in the dashboard and
inventory screens.

Revision ID: 0010_expiry_alert_days
Revises: 0009_inventory_item_detail
Create Date: 2026-09-08 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = '0010_expiry_alert_days'
down_revision = '0009_inventory_item_detail'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenant_configs', sa.Column('expiry_alert_days', sa.Integer(), server_default='90', nullable=False))
    op.create_check_constraint('ck_config_expiry_alert', 'tenant_configs', 'expiry_alert_days between 1 and 730')


def downgrade() -> None:
    op.drop_constraint('ck_config_expiry_alert', 'tenant_configs', type_='check')
    op.drop_column('tenant_configs', 'expiry_alert_days')
