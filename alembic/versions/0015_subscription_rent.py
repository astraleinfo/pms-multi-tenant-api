"""subscription rent: monthly bills, reminders, and the hold

Each clinic is billed monthly for the paid modules it has switched on. The
bills are tenant-owned (the clinic reads its own), so the table gets the same
row-level security as every other tenant table. The tenant row gains the hold
— set when a bill is past its grace period, cleared when it is settled — and
an exemption for clinics that are never billed.

Revision ID: 0015_subscription_rent
Revises: 0014_lab_attachment_storage_key
Create Date: 2026-09-23 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0015_subscription_rent'
down_revision = '0014_lab_attachment_storage_key'
branch_labels = None
depends_on = None

CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
APP_ROLE = "pms_app"
TABLE = "subscription_invoices"
#: read by `test_every_tenant_owned_table_has_a_policy`
OWNED_TABLES = ["subscription_invoices"]

status = postgresql.ENUM("open", "paid", "void", name="subscription_invoice_status", create_type=False)


def upgrade() -> None:
    op.add_column('tenants', sa.Column('billing_hold_since', sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        'tenants',
        sa.Column('billing_exempt', sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    bind = op.get_bind()
    status.create(bind, checkfirst=True)

    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.String(60), nullable=False, unique=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("lines", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("issued_on", sa.Date(), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("status", status, nullable=False, server_default="open"),
        sa.Column("reminder_stage", sa.String(20), nullable=False, server_default="none"),
        sa.Column("reminders_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_reference", sa.String(120), nullable=False, server_default=""),
        sa.Column("settled_by", sa.String(160), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "period_start", name="uq_subscription_invoice_period"),
        sa.CheckConstraint("amount >= 0", name="ck_subscription_invoice_amount"),
        sa.CheckConstraint("due_on >= issued_on", name="ck_subscription_invoice_due"),
    )
    op.create_index("ix_subscription_invoices_tenant_id", TABLE, ["tenant_id"])
    op.create_index("ix_subscription_invoices_tenant_id_id", TABLE, ["tenant_id", "id"])
    op.create_index("ix_subscription_invoices_status_due", TABLE, ["status", "due_on"])

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
    op.drop_table(TABLE)
    status.drop(op.get_bind(), checkfirst=True)
    op.drop_column('tenants', 'billing_exempt')
    op.drop_column('tenants', 'billing_hold_since')
