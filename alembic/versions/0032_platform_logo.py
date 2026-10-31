"""the platform can set its own logo

Four columns on `platform_settings`, the same split as a clinic's logo
(`0020_tenant_config_logo`): `logo_data` holds the bytes, or
`logo_storage_key` names the S3 object, never both. `logo_filename` null means
none has been uploaded, and the built-in logo is used.

Revision ID: 0032_platform_logo
Revises: 0031_prescription_pricing
Create Date: 2026-10-31 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0032_platform_logo"
down_revision = "0031_prescription_pricing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("platform_settings", sa.Column("logo_filename", sa.String(length=200), nullable=True))
    op.add_column("platform_settings", sa.Column("logo_content_type", sa.String(length=120), nullable=True))
    op.add_column("platform_settings", sa.Column("logo_data", sa.LargeBinary(), nullable=True))
    op.add_column("platform_settings", sa.Column("logo_storage_key", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("platform_settings", "logo_storage_key")
    op.drop_column("platform_settings", "logo_data")
    op.drop_column("platform_settings", "logo_content_type")
    op.drop_column("platform_settings", "logo_filename")
