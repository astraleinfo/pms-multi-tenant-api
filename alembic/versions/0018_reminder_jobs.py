"""reminder jobs: appointment reminders sent by a Celery job

A reminder now waits as `scheduled` until its day comes, and is then handed to
Celery with the job's id written on the row. `scheduled` is new; `job_id` is
the job that will send it.

Reminders already `queued` were waiting for the outbox sweep. They become
`scheduled`. Later days are queued by their midnight runs; any due on the day
of the deploy are after that day's midnight run, so the reminder sweep picks
each one up within a few minutes of its time.

Revision ID: 0018_reminder_jobs
Revises: 0017_import_jobs
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0018_reminder_jobs"
down_revision = "0017_import_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A new enum value cannot be used in the transaction that adds it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE message_status ADD VALUE IF NOT EXISTS 'scheduled' BEFORE 'queued'")

    op.add_column("messages", sa.Column("job_id", sa.String(64), nullable=True))
    op.execute(
        "UPDATE messages SET status = 'scheduled' "
        "WHERE template = 'appointment_reminder' AND status = 'queued'"
    )


def downgrade() -> None:
    # Back to the outbox sweep. PostgreSQL cannot drop an enum value, so the
    # type keeps `scheduled`; no row uses it.
    op.execute("UPDATE messages SET status = 'queued' WHERE status = 'scheduled'")
    op.drop_column("messages", "job_id")
