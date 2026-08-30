"""messages for a moved or cancelled appointment

Two new templates, and a correction to the uniqueness rule that goes with them.

`uq_messages_appointment_template` existed to stop a retry producing a second
booking confirmation. As written it also stopped anything else happening twice
to one appointment — which breaks the moment a slot moves:

*   a reschedule cancels the old reminder and needs a fresh one for the new
    time, but the cancelled row still occupied the index;
*   an appointment moved twice needs two "your appointment has moved" messages,
    and the second could not be written.

So the rule is narrowed to what it was actually protecting: at most one *live*
confirmation and one *live* reminder per appointment. Notices about a change
are events, not state, and may legitimately repeat.

Revision ID: 0005_reschedule_messages
Revises: 0004_messaging
"""

from __future__ import annotations

from alembic import op

revision = "0005_reschedule_messages"
down_revision = "0004_messaging"
branch_labels = None
depends_on = None

NEW_VALUES = ["appointment_rescheduled", "appointment_cancelled"]

LIVE_ONLY = """
    CREATE UNIQUE INDEX uq_messages_appointment_template
        ON messages (appointment_id, template)
     WHERE appointment_id IS NOT NULL
       AND template IN ('booking_confirmation', 'appointment_reminder')
       AND status <> 'cancelled'
"""

ORIGINAL = """
    CREATE UNIQUE INDEX uq_messages_appointment_template
        ON messages (appointment_id, template)
     WHERE appointment_id IS NOT NULL AND template <> 'custom'
"""


def upgrade() -> None:
    for value in NEW_VALUES:
        op.execute(f"ALTER TYPE message_template ADD VALUE IF NOT EXISTS '{value}'")
    op.execute("DROP INDEX IF EXISTS uq_messages_appointment_template")
    op.execute(LIVE_ONLY)


def downgrade() -> None:
    # Any row carrying a value this index would not survive is removed first;
    # PostgreSQL cannot drop a value from an enum, so the type keeps them.
    op.execute(f"DELETE FROM messages WHERE template IN ({', '.join(repr(v) for v in NEW_VALUES)})")
    op.execute("DROP INDEX IF EXISTS uq_messages_appointment_template")
    op.execute(ORIGINAL)
