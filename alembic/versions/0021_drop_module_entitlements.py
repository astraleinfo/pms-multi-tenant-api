"""every tenant gets every module

The per-tenant module entitlement is gone. There is no catalogue of modules in
the database any more, nothing to purchase or switch on, and no feature guard:
every clinic has patients, appointments, queue, consultation, prescriptions,
pharmacy, lab, inventory, billing, advanced reports, configuration and team,
and what a person reaches inside them is decided by their role alone.

What the platform bills a clinic becomes one flat monthly fee, kept on the
`platform_settings` singleton. Upgrade, in order:

1.  `platform_settings.subscription_fee_monthly`, set to the sum of today's
    paid-module prices so the existing pricing carries over as the
    all-inclusive fee.
2.  `support_requests`: a request that named a module keeps the module's name
    in its message (``Module: <name>``), `activation` requests become
    `general`, the `feature_key` column (and its foreign key) goes, and the
    `support_request_kind` enum is rebuilt without `activation`.
3.  `tenant_features` (with its policy), its `tenant_feature_status` type, and
    then the `features` catalogue are dropped.

Downgrade recreates the structures, empty, as best effort: a module that was
switched on, a price, or a request's `feature_key` cannot be recovered.

Revision ID: 0021_drop_module_entitlements
Revises: 0020_tenant_config_logo
Create Date: 2026-10-06 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0021_drop_module_entitlements"
down_revision = "0020_tenant_config_logo"
branch_labels = None
depends_on = None

CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
APP_ROLE = "pms_app"


def upgrade() -> None:
    # 1. the flat clinic fee, carried over from the module prices
    op.add_column(
        "platform_settings",
        sa.Column(
            "subscription_fee_monthly", sa.Numeric(10, 2), nullable=False, server_default="0"
        ),
    )
    op.execute(
        """
        UPDATE platform_settings
           SET subscription_fee_monthly =
               COALESCE((SELECT SUM(price_monthly) FROM features WHERE NOT is_core), 0)
        """
    )

    # 2. support requests no longer name a module. The table's row-level
    # security is FORCEd, so even its owner sees no rows with no clinic bound;
    # it is relaxed for the length of the rewrite — a data fix that is meant
    # to touch every clinic's rows — and put straight back.
    op.execute("ALTER TABLE support_requests NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        UPDATE support_requests AS r
           SET message = 'Module: ' || f.name
                         || CASE WHEN r.message <> '' THEN E'\\n\\n' || r.message ELSE '' END
          FROM features AS f
         WHERE f.key = r.feature_key
        """
    )
    op.execute("UPDATE support_requests SET kind = 'general' WHERE kind = 'activation'")
    op.execute("ALTER TABLE support_requests FORCE ROW LEVEL SECURITY")

    # Dropping the column drops its foreign key to `features` with it.
    op.drop_column("support_requests", "feature_key")

    op.execute("ALTER TYPE support_request_kind RENAME TO support_request_kind_old")
    op.execute("CREATE TYPE support_request_kind AS ENUM ('walkthrough', 'general')")
    op.execute(
        "ALTER TABLE support_requests ALTER COLUMN kind "
        "TYPE support_request_kind USING kind::text::support_request_kind"
    )
    op.execute("DROP TYPE support_request_kind_old")

    # 3. the entitlement table, its type, and the catalogue it pointed at
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tenant_features")
    op.drop_table("tenant_features")  # its indexes and constraints go with it
    op.execute("DROP TYPE IF EXISTS tenant_feature_status")
    op.drop_table("features")


def downgrade() -> None:
    # 3. the catalogue and the entitlement table, empty
    op.create_table(
        "features",
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("is_core", sa.Boolean(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("price_monthly", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("unlocks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "tenant_features",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("feature_key", sa.String(length=40), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "inactive", "trial", name="tenant_feature_status"),
            nullable=False,
        ),
        sa.Column("activated_at", sa.Date(), nullable=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["feature_key"], ["features.key"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "feature_key", name="uq_tenant_feature"),
    )
    op.create_index(op.f("ix_tenant_features_tenant_id"), "tenant_features", ["tenant_id"], unique=False)
    op.create_index("ix_tenant_features_tenant_id_id", "tenant_features", ["tenant_id", "id"], unique=False)
    op.execute("ALTER TABLE tenant_features ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_features FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON tenant_features
        USING (tenant_id = {CURRENT})
        WITH CHECK (tenant_id = {CURRENT})
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON features TO {APP_ROLE}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_features TO {APP_ROLE}';
            END IF;
        END
        $$;
        """
    )

    # 2. `activation` back in the kind enum, and the module column (empty)
    op.execute("ALTER TYPE support_request_kind RENAME TO support_request_kind_new")
    op.execute("CREATE TYPE support_request_kind AS ENUM ('activation', 'walkthrough', 'general')")
    op.execute(
        "ALTER TABLE support_requests ALTER COLUMN kind "
        "TYPE support_request_kind USING kind::text::support_request_kind"
    )
    op.execute("DROP TYPE support_request_kind_new")
    op.add_column(
        "support_requests",
        sa.Column(
            "feature_key",
            sa.String(40),
            sa.ForeignKey("features.key", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # 1. the flat fee
    op.drop_column("platform_settings", "subscription_fee_monthly")
