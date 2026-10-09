"""follow-up calls: ringing patients about upcoming appointments

- `appointment_status` gains `confirmed` — the patient said on the phone that
  they are coming. It sits between `scheduled` and `checked_in`.
- `follow_up_calls` is the log of every call: the appointment, what came of it,
  the notes, who rang, and — for a call that moved the booking — from where to
  where. Tenant-owned, under the same forced row-level security as every other
  clinical table.

Nothing is backfilled: every existing booking simply has no calls yet, which
is what "pending" means.

Revision ID: 0024_follow_up_calls
Revises: 0023_drop_appointment_reminders
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0024_follow_up_calls"
down_revision = "0023_drop_appointment_reminders"
branch_labels = None
depends_on = None

CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
APP_ROLE = "pms_app"
#: read by `test_every_tenant_owned_table_has_a_policy`
OWNED_TABLES = ["follow_up_calls"]

follow_up_outcome = postgresql.ENUM(
    "confirmed", "reschedule_required", "rescheduled", "no_answer", "declined", "call_back",
    name="follow_up_outcome", create_type=False,
)


def _uuid(name: str, *args, **kw) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, **kw)


def upgrade() -> None:
    # A new enum value cannot be used in the transaction that adds it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE appointment_status ADD VALUE IF NOT EXISTS 'confirmed' AFTER 'scheduled'")

    follow_up_outcome.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "follow_up_calls",
        _uuid("id", primary_key=True),
        _uuid("tenant_id", sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        _uuid("appointment_id", sa.ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False),
        _uuid("patient_id", sa.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("outcome", follow_up_outcome, nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        _uuid("called_by_id", sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("called_by_label", sa.String(160), nullable=False, server_default=""),
        sa.Column("moved_from", sa.String(16), nullable=True),
        sa.Column("moved_to", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_follow_up_calls_tenant_id", "follow_up_calls", ["tenant_id"])
    op.create_index("ix_follow_up_calls_tenant_id_id", "follow_up_calls", ["tenant_id", "id"])
    op.create_index(
        "ix_follow_up_calls_appointment", "follow_up_calls", ["tenant_id", "appointment_id", "created_at"]
    )

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


def downgrade() -> None:
    op.drop_table("follow_up_calls")
    follow_up_outcome.drop(op.get_bind(), checkfirst=True)
    # PostgreSQL cannot drop an enum value, so `appointment_status` keeps
    # `confirmed`; its bookings go back to plain `scheduled`.
    op.execute("ALTER TABLE appointments NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE appointments SET status = 'scheduled' WHERE status = 'confirmed'")
    op.execute("ALTER TABLE appointments FORCE ROW LEVEL SECURITY")
