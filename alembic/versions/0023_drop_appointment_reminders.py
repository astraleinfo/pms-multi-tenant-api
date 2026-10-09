"""drop appointment reminders

Patients are no longer sent an appointment reminder. Every reminder still
waiting — `scheduled`, or `queued` with a Celery job — is cancelled, so nothing
left in the broker or the outbox can send one. Reminders already sent or failed
stay, as the record of what the patient received.

Gone with the feature: the job each reminder carried (`messages.job_id`), the
clinic's switch for it (`messaging_configs.send_appointment_reminder`) and the
platform's lead time (`platform_settings.reminder_lead_hours`). The live-message
index now guards the booking confirmation alone.

PostgreSQL cannot drop an enum value, so `message_status` keeps `scheduled` and
`message_template` keeps `appointment_reminder`; after this no row is
`scheduled`.

Revision ID: 0023_drop_appointment_reminders
Revises: 0022_consent_forms
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0023_drop_appointment_reminders"
down_revision = "0022_consent_forms"
branch_labels = None
depends_on = None

INDEX = """
    CREATE UNIQUE INDEX uq_messages_appointment_template
        ON messages (appointment_id, template)
     WHERE appointment_id IS NOT NULL
       AND template IN ({templates})
       AND status <> 'cancelled'
"""


def upgrade() -> None:
    # `messages` is FORCEd, so even its owner sees no rows with no clinic
    # bound; relaxed for a data fix meant to touch every clinic's rows, and put
    # straight back.
    op.execute("ALTER TABLE messages NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE messages SET status = 'cancelled' "
        "WHERE template = 'appointment_reminder' AND status IN ('scheduled', 'queued')"
    )
    op.execute("ALTER TABLE messages FORCE ROW LEVEL SECURITY")

    op.execute("DROP INDEX uq_messages_appointment_template")
    op.execute(INDEX.format(templates="'booking_confirmation'"))

    op.drop_column("messages", "job_id")
    op.drop_column("messaging_configs", "send_appointment_reminder")
    op.drop_column("platform_settings", "reminder_lead_hours")


def downgrade() -> None:
    # The columns come back with their old defaults; the reminders cancelled on
    # the way up stay cancelled.
    op.add_column(
        "platform_settings",
        sa.Column("reminder_lead_hours", sa.Integer(), nullable=False, server_default="24"),
    )
    op.add_column(
        "messaging_configs",
        sa.Column("send_appointment_reminder", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("messages", sa.Column("job_id", sa.String(64), nullable=True))

    op.execute("DROP INDEX uq_messages_appointment_template")
    op.execute(INDEX.format(templates="'booking_confirmation', 'appointment_reminder'"))
