"""row-level security

Tenant isolation, enforced by PostgreSQL rather than by remembering to write a
WHERE clause.

Three shapes of policy:

*   **owned** — the ordinary case. A row is visible only when its `tenant_id`
    equals `app.tenant_id`, and a row can only be written with that same value.
    `FORCE ROW LEVEL SECURITY` extends this to the table owner, so not even the
    migration role can read across clinics without setting the variable.
*   **shared_or_own** — `field_definitions`: the common and clinical-type rows
    belong to the platform (`tenant_id IS NULL`) and everyone may read them,
    but a clinic may only write rows carrying its own id.
*   **own_or_platform** — the auth and audit tables, where a row with no tenant
    belongs to the console and is only visible when no tenant is bound.

`nullif(current_setting('app.tenant_id', true), '')::uuid` is NULL when nothing
has been bound, and comparing anything to NULL is NULL — so an unbound session
sees no rows at all. The failure mode is emptiness, never leakage.

Revision ID: 0002_rls
Revises: 0001_initial
"""

from __future__ import annotations

from alembic import op

revision = "0002_rls"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"

# Every table whose rows belong to exactly one clinic.
OWNED_TABLES = [
    "appointments",
    "encounters",
    "inventory_batches",
    "inventory_items",
    "inventory_transactions",
    "invoice_line_items",
    "invoices",
    "lab_orders",
    "patients",
    "payments",
    "prescription_items",
    "prescriptions",
    "queue_entries",
    "tenant_configs",
    "tenant_features",
    "users",
]

SHARED_OR_OWN_TABLES = ["field_definitions"]

OWN_OR_PLATFORM_TABLES = ["audit_events", "login_attempts", "refresh_tokens"]

APP_ROLE = "pms_app"


def upgrade() -> None:
    for table in OWNED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = {CURRENT})
            WITH CHECK (tenant_id = {CURRENT})
            """
        )

    for table in SHARED_OR_OWN_TABLES:
        # Not FORCEd: the platform owns the shared rows and manages them as the
        # migration/seed role. The application role is fully constrained.
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id IS NULL OR tenant_id = {CURRENT})
            WITH CHECK (tenant_id = {CURRENT})
            """
        )

    for table in OWN_OR_PLATFORM_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (
                tenant_id = {CURRENT}
                OR (tenant_id IS NULL AND {CURRENT} IS NULL)
            )
            WITH CHECK (
                tenant_id = {CURRENT}
                OR (tenant_id IS NULL AND {CURRENT} IS NULL)
            )
            """
        )

    # The audit trail is written, never rewritten.
    op.execute(
        """
        CREATE RULE audit_events_no_update AS ON UPDATE TO audit_events DO INSTEAD NOTHING
        """
    )
    op.execute(
        """
        CREATE RULE audit_events_no_delete AS ON DELETE TO audit_events DO INSTEAD NOTHING
        """
    )

    # Grants for the application role, if this deployment uses the standard name.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA public TO {APP_ROLE}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}';
                EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}';
                -- The application never changes the shape of the database.
                EXECUTE 'REVOKE CREATE ON SCHEMA public FROM {APP_ROLE}';
                -- …and never edits history.
                EXECUTE 'REVOKE UPDATE, DELETE ON audit_events FROM {APP_ROLE}';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP RULE IF EXISTS audit_events_no_update ON audit_events")
    op.execute("DROP RULE IF EXISTS audit_events_no_delete ON audit_events")
    for table in OWNED_TABLES + SHARED_OR_OWN_TABLES + OWN_OR_PLATFORM_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
