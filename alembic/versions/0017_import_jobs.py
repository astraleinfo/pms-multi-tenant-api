"""import jobs: spreadsheet imports, run by the worker

The upload is stored on the row (or in the bucket) until the worker has
finished with it, and cleared when it has — pass or fail. Tenant-owned, so it
gets the same forced row-level security as every other clinical table.

Revision ID: 0017_import_jobs
Revises: 0016_appointment_types
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0017_import_jobs"
down_revision = "0016_appointment_types"
branch_labels = None
depends_on = None

CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
APP_ROLE = "pms_app"
TABLE = "import_jobs"
#: read by `test_every_tenant_owned_table_has_a_policy`
OWNED_TABLES = ["import_jobs"]

status = postgresql.ENUM("queued", "running", "done", "failed", name="import_job_status", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    status.create(bind, checkfirst=True)

    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("status", status, nullable=False, server_default="queued"),
        sa.Column("filename", sa.String(255), nullable=False, server_default=""),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("data", sa.LargeBinary(), nullable=True),
        sa.Column("storage_key", sa.String(300), nullable=True),
        sa.Column("requested_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by_label", sa.String(160), nullable=False, server_default=""),
        sa.Column("actor_kind", sa.String(20), nullable=False, server_default="user"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_import_jobs_tenant_id", TABLE, ["tenant_id"])
    op.create_index("ix_import_jobs_tenant_id_id", TABLE, ["tenant_id", "id"])
    op.create_index("ix_import_jobs_status_created", TABLE, ["status", "created_at"])
    op.create_index("ix_import_jobs_tenant_kind_created", TABLE, ["tenant_id", "kind", "created_at"])

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
    status.drop(bind=op.get_bind(), checkfirst=True)
