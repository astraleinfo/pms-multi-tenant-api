"""patient messaging

An outbox and the per-clinic provider settings that drive it.

Both tables are tenant-owned, so both get the same row-level security every
other clinical table has: a session that has not bound `app.tenant_id` sees
nothing, and one that has sees only its own clinic. `FORCE ROW LEVEL SECURITY`
extends that to the owner, so a migration cannot read across clinics either.

Revision ID: 0004_messaging
Revises: 0003_user_preferences
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_messaging"
down_revision = "0003_user_preferences"
branch_labels = None
depends_on = None

CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
APP_ROLE = "pms_app"
OWNED_TABLES = ["messaging_configs", "messages"]

provider = postgresql.ENUM("console", "twilio", name="message_provider", create_type=False)
channel = postgresql.ENUM("sms", "whatsapp", name="message_channel", create_type=False)
template = postgresql.ENUM(
    "booking_confirmation", "appointment_reminder", "follow_up", "custom",
    name="message_template", create_type=False,
)
status = postgresql.ENUM(
    "queued", "sending", "sent", "failed", "cancelled", name="message_status", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    provider.create(bind, checkfirst=True)
    channel.create(bind, checkfirst=True)
    template.create(bind, checkfirst=True)
    status.create(bind, checkfirst=True)

    op.create_table(
        "messaging_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", provider, nullable=False, server_default="console"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("send_booking_confirmation", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("send_appointment_reminder", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sender_id", sa.String(40), nullable=False, server_default=""),
        sa.Column("account_sid", sa.String(120), nullable=False, server_default=""),
        sa.Column("auth_token_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", name="uq_messaging_config_tenant"),
    )
    op.create_index("ix_messaging_configs_tenant_id", "messaging_configs", ["tenant_id"])
    op.create_index("ix_messaging_configs_tenant_id_id", "messaging_configs", ["tenant_id", "id"])

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("appointment_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("channel", channel, nullable=False, server_default="sms"),
        sa.Column("template", template, nullable=False),
        sa.Column("status", status, nullable=False, server_default="queued"),
        sa.Column("to_address", sa.String(40), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("send_after", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("provider", provider, nullable=True),
        sa.Column("provider_message_id", sa.String(120), nullable=False, server_default=""),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_messages_tenant_id", "messages", ["tenant_id"])
    op.create_index("ix_messages_tenant_id_id", "messages", ["tenant_id", "id"])
    op.create_index("ix_messages_due", "messages", ["status", "send_after"])
    op.create_index("ix_messages_tenant_created", "messages", ["tenant_id", "created_at"])
    # One automatic message per appointment per template. A retry that races
    # cannot produce a second booking confirmation.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_messages_appointment_template
            ON messages (appointment_id, template)
         WHERE appointment_id IS NOT NULL AND template <> 'custom'
        """
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
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def downgrade() -> None:
    for table in reversed(OWNED_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table("messages")
    op.drop_table("messaging_configs")
    bind = op.get_bind()
    for enum in (status, template, channel, provider):
        enum.drop(bind, checkfirst=True)
