"""platform account roles

Platform staff were one kind of account: everybody in the console could do
everything in it. The jobs section reads the platform's own machinery — the
workers, the broker, the arguments every job was called with — so it is the
first thing in the console that not every member of staff should see, and a
role is what tells the two apart.

Every account that existed before this column was a super admin, so that is
the server default and the backfill. Narrowing somebody to `support` is a
deliberate act afterwards.

Revision ID: 0013_platform_roles
Revises: 0012_support_requests
Create Date: 2026-09-22 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = '0013_platform_roles'
down_revision = '0012_support_requests'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'platform_users',
        sa.Column('role', sa.String(length=40), nullable=False, server_default='super_admin'),
    )


def downgrade() -> None:
    op.drop_column('platform_users', 'role')
