"""merge the two heads left by the parallel weight-field and platform-settings work

`0016_appointment_types` and `0016_patient_weight_field` both branched off
`0015_subscription_rent`, and nothing since has brought them back together —
`alembic upgrade head` (singular) has been refusing to run with two heads
present. Nothing to migrate here; this just gives the tree one head again so
work can keep stacking on top of it.

Revision ID: 0019_merge_heads
Revises: 0017_platform_settings, 0018_reminder_jobs
Create Date: 2026-09-26 00:00:00.000000
"""

from __future__ import annotations

revision = '0019_merge_heads'
down_revision = ('0017_platform_settings', '0018_reminder_jobs')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
