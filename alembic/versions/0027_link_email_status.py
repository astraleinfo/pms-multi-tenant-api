"""users: where the newest invitation or reset email is

Invitation and reset emails move off the request and onto the Celery worker,
which mints the link itself so the plaintext never sits in the broker. These
columns are how a screen knows whether the email went: `queued` until the
worker has tried, then `sent` or `failed`, with the time and, on a failure,
why. NULL on every existing account — nothing was ever queued for them.

Revision ID: 0027_link_email_status
Revises: 0026_merge_heads
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0027_link_email_status"
down_revision = "0026_merge_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("link_email_status", sa.String(16), nullable=True))
    op.add_column("users", sa.Column("link_email_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users", sa.Column("link_email_error", sa.Text(), nullable=False, server_default="")
    )


def downgrade() -> None:
    op.drop_column("users", "link_email_error")
    op.drop_column("users", "link_email_at")
    op.drop_column("users", "link_email_status")
