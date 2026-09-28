"""support requests

A clinic asking the platform for a module, or a walkthrough of one. The row is
the ask; the Celery worker emails the support desk from it. Tenant-owned, so it
gets the same forced row-level security as every other clinical table.

Revision ID: 0012_support_requests
Revises: 0011_doctor_consultation_fee
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0012_support_requests"
down_revision = "0011_doctor_consultation_fee"
branch_labels = None
depends_on = None

CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
APP_ROLE = "pms_app"
TABLE = "support_requests"
#: read by `test_every_tenant_owned_table_has_a_policy`
OWNED_TABLES = ["support_requests"]

kind = postgresql.ENUM(
    "activation", "walkthrough", "general", name="support_request_kind", create_type=False
)
status = postgresql.ENUM("queued", "sent", "failed", name="support_request_status", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    kind.create(bind, checkfirst=True)
    status.create(bind, checkfirst=True)

    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", kind, nullable=False),
        sa.Column("feature_key", sa.String(40),
                  sa.ForeignKey("features.key", ondelete="SET NULL"), nullable=True),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("requested_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by_label", sa.String(160), nullable=False, server_default=""),
        sa.Column("reply_to", sa.String(254), nullable=False, server_default=""),
        sa.Column("status", status, nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_support_requests_tenant_id", TABLE, ["tenant_id"])
    op.create_index("ix_support_requests_tenant_id_id", TABLE, ["tenant_id", "id"])
    op.create_index("ix_support_requests_due", TABLE, ["status", "next_attempt_at"])
    op.create_index("ix_support_requests_tenant_created", TABLE, ["tenant_id", "created_at"])

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {TABLE}
            USING (tenant_id = {CURRENT})
            WITH CHECK (tenant_id = {CURRENT})
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.execute(f"ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY")
    op.drop_table(TABLE)
    bind = op.get_bind()
    status.drop(bind, checkfirst=True)
    kind.drop(bind, checkfirst=True)
