"""patient notes: clinicians' notes on a patient, kept as a history

`patient_notes` holds every note written about a patient outside a single
consultation — who wrote it, in what role, and when. Tenant-owned, under the
same forced row-level security as every other clinical table.

A note is never rewritten. A correction is a new row pointing at the note it
corrects (`amends_id`), and a trigger refuses any change to a saved note's
words, author, patient or time — whatever path the change comes by. The one
change allowed is the database's own: `author_id` going to NULL when the
author's account is deleted, since `author_label` still names them.

Revision ID: 0025_patient_notes
Revises: 0024_follow_up_calls
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0025_patient_notes"
down_revision = "0024_follow_up_calls"
branch_labels = None
depends_on = None

CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
APP_ROLE = "pms_app"
#: read by `test_every_tenant_owned_table_has_a_policy`
OWNED_TABLES = ["patient_notes"]


def _uuid(name: str, *args, **kw) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, **kw)


def upgrade() -> None:
    op.create_table(
        "patient_notes",
        _uuid("id", primary_key=True),
        _uuid("tenant_id", sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        _uuid("patient_id", sa.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        _uuid("author_id", sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("author_label", sa.String(160), nullable=False, server_default=""),
        sa.Column("author_role", sa.String(40), nullable=False, server_default=""),
        _uuid("amends_id", sa.ForeignKey("patient_notes.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("length(btrim(body)) > 0", name="ck_patient_note_body"),
    )
    op.create_index("ix_patient_notes_tenant_id", "patient_notes", ["tenant_id"])
    op.create_index("ix_patient_notes_tenant_id_id", "patient_notes", ["tenant_id", "id"])
    op.create_index("ix_patient_notes_patient", "patient_notes", ["tenant_id", "patient_id", "created_at"])

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
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}';
                END IF;
            END
            $$;
            """
        )

    op.execute(
        """
        CREATE FUNCTION patient_note_frozen() RETURNS trigger AS $$
        BEGIN
            IF NEW.body IS DISTINCT FROM OLD.body
               OR NEW.patient_id IS DISTINCT FROM OLD.patient_id
               OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.author_label IS DISTINCT FROM OLD.author_label
               OR NEW.author_role IS DISTINCT FROM OLD.author_role
               OR NEW.amends_id IS DISTINCT FROM OLD.amends_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR (NEW.author_id IS DISTINCT FROM OLD.author_id AND NEW.author_id IS NOT NULL)
            THEN
                RAISE EXCEPTION 'a saved patient note cannot be changed; add a correction instead'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER patient_note_frozen BEFORE UPDATE ON patient_notes
            FOR EACH ROW EXECUTE FUNCTION patient_note_frozen()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS patient_note_frozen ON patient_notes")
    op.execute("DROP FUNCTION IF EXISTS patient_note_frozen()")
    op.drop_table("patient_notes")
