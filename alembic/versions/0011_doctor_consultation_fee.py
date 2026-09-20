"""per-doctor consultation fee

A doctor's own consultation price, overriding the clinic-wide default when
set. NULL means "use the clinic default."

Revision ID: 0011_doctor_consultation_fee
Revises: 0010_expiry_alert_days
Create Date: 2026-09-08 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = '0011_doctor_consultation_fee'
down_revision = '0010_expiry_alert_days'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('consultation_fee', sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'consultation_fee')
