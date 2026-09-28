"""a clinic can set its own logo

Four columns on `tenant_configs`, mirroring how `lab_attachments` splits a
file between the row and S3: `logo_data` holds the bytes, or
`logo_storage_key` names the object, never both. Unlike an attachment, a
clinic has at most one logo, so there is no check constraint forcing either
to be set — `logo_filename` null means no logo at all.

Revision ID: 0020_tenant_config_logo
Revises: 0019_merge_heads
Create Date: 2026-09-26 00:05:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = '0020_tenant_config_logo'
down_revision = '0019_merge_heads'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenant_configs', sa.Column('logo_filename', sa.String(length=200), nullable=True))
    op.add_column('tenant_configs', sa.Column('logo_content_type', sa.String(length=120), nullable=True))
    op.add_column('tenant_configs', sa.Column('logo_data', sa.LargeBinary(), nullable=True))
    op.add_column('tenant_configs', sa.Column('logo_storage_key', sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column('tenant_configs', 'logo_storage_key')
    op.drop_column('tenant_configs', 'logo_data')
    op.drop_column('tenant_configs', 'logo_content_type')
    op.drop_column('tenant_configs', 'logo_filename')
