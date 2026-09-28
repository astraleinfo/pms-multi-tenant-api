"""referral credits: a clinic earns credit toward its own bill when a clinic
it refers pays its first one

`referrals` names two clinics and belongs to neither, so unlike every other
table added in this project it carries no `tenant_id` and gets no row-level
security — platform accounting, like `platform_settings`. `referral_redemptions`
is the referrer's own spend record and is tenant-owned like everything else.

Revision ID: 0021_referral_credits
Revises: 0020_tenant_config_logo
Create Date: 2026-09-28 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0021_referral_credits'
down_revision = '0020_tenant_config_logo'
branch_labels = None
depends_on = None

CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
APP_ROLE = "pms_app"
REDEMPTIONS_TABLE = "referral_redemptions"
#: read by `test_every_tenant_owned_table_has_a_policy` — `referrals` is
#: deliberately absent: it is platform-owned and carries no tenant_id at all.
OWNED_TABLES = ["referral_redemptions"]

status = postgresql.ENUM("pending", "vested", "void", name="referral_status", create_type=False)


def upgrade() -> None:
    op.add_column('tenants', sa.Column('referral_code', sa.String(16), nullable=True))
    op.create_unique_constraint('uq_tenants_referral_code', 'tenants', ['referral_code'])

    op.add_column(
        'platform_settings',
        sa.Column('referral_credit_amount', sa.Numeric(12, 2), nullable=False, server_default='100.00'),
    )

    bind = op.get_bind()
    status.create(bind, checkfirst=True)

    op.create_table(
        "referrals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("referrer_tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("referred_tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_used", sa.String(16), nullable=False),
        sa.Column("credit_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", status, nullable=False, server_default="pending"),
        sa.Column("vested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("referred_tenant_id", name="uq_referral_referred_tenant"),
        sa.CheckConstraint("referrer_tenant_id <> referred_tenant_id", name="ck_referral_not_self"),
        sa.CheckConstraint("credit_amount >= 0", name="ck_referral_credit_amount"),
    )
    op.create_index("ix_referrals_referrer_tenant_id", "referrals", ["referrer_tenant_id"])
    # No RLS on this table: it is platform-owned, not tenant-owned. Grant the
    # app role ordinary access the same way every other table gets it.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON referrals TO {APP_ROLE}';
            END IF;
        END
        $$;
        """
    )

    op.create_table(
        REDEMPTIONS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subscription_invoice_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("subscription_invoices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("subscription_invoice_id", name="uq_referral_redemption_invoice"),
        sa.CheckConstraint("amount > 0", name="ck_referral_redemption_amount"),
    )
    op.create_index("ix_referral_redemptions_tenant_id", REDEMPTIONS_TABLE, ["tenant_id"])
    op.create_index("ix_referral_redemptions_tenant_id_id", REDEMPTIONS_TABLE, ["tenant_id", "id"])

    op.execute(f"ALTER TABLE {REDEMPTIONS_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {REDEMPTIONS_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {REDEMPTIONS_TABLE}
            USING (tenant_id = {CURRENT})
            WITH CHECK (tenant_id = {CURRENT})
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON {REDEMPTIONS_TABLE} TO {APP_ROLE}';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {REDEMPTIONS_TABLE}")
    op.drop_table(REDEMPTIONS_TABLE)
    op.drop_table("referrals")
    status.drop(op.get_bind(), checkfirst=True)
    op.drop_column('platform_settings', 'referral_credit_amount')
    op.drop_constraint('uq_tenants_referral_code', 'tenants', type_='unique')
    op.drop_column('tenants', 'referral_code')
