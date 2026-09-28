"""lab attachments can live in S3

A lab report's bytes were always in `lab_attachments.data`. With
`STORAGE_BACKEND=s3` they go to a bucket instead and the row keeps the key, so
`data` becomes nullable and `storage_key` is added beside it. A row must still
hold its file one way or the other, and the check constraint says so.

Existing rows keep their bytes where they are: reads follow the row, not the
setting, so nothing needs to be copied for a deployment to switch over.

Revision ID: 0014_lab_attachment_storage_key
Revises: 0013_platform_roles
Create Date: 2026-09-23 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = '0014_lab_attachment_storage_key'
down_revision = '0013_platform_roles'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('lab_attachments', sa.Column('storage_key', sa.String(length=512), nullable=True))
    op.alter_column('lab_attachments', 'data', existing_type=sa.LargeBinary(), nullable=True)
    op.create_check_constraint(
        'ck_lab_attachment_stored',
        'lab_attachments',
        'data IS NOT NULL OR storage_key IS NOT NULL',
    )


def downgrade() -> None:
    # A row whose file is in S3 has nothing to put back into `data`, so this
    # fails on `SET NOT NULL` while any exist — and the whole downgrade rolls
    # back with it. Copy those files back into `data` first. (A pre-check
    # SELECT would not help: FORCE ROW LEVEL SECURITY hides every row from the
    # migration role, whereas the ALTER checks them all.)
    op.drop_constraint('ck_lab_attachment_stored', 'lab_attachments', type_='check')
    op.alter_column('lab_attachments', 'data', existing_type=sa.LargeBinary(), nullable=False)
    op.drop_column('lab_attachments', 'storage_key')
