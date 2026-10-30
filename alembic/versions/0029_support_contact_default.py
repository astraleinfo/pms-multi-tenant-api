"""the support desk's contact moves to astraleinfo@gmail.com / 7305961254

`0017_platform_settings` seeded the singleton row with the placeholder desk
(`support@astrale.health`, `+91 80 4718 2200`). The real desk is now the
address and number below, so two things change: the column defaults, so a
row created from scratch starts there, and the existing row — but only a
column that still holds the old placeholder. A super admin who has already
typed their own email or phone into the console keeps it; each column is
judged on its own.

The downgrade is the mirror image: a column still holding the new desk goes
back to the placeholder, a customised one is left alone.

Revision ID: 0029_support_contact_default
Revises: 0028_pdf_identity_fields
Create Date: 2026-10-30 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0029_support_contact_default"
down_revision = "0028_pdf_identity_fields"
branch_labels = None
depends_on = None

OLD_EMAIL = "support@astrale.health"
OLD_PHONE = "+91 80 4718 2200"
NEW_EMAIL = "astraleinfo@gmail.com"
NEW_PHONE = "7305961254"


def move_support_contact(from_email: str, from_phone: str, to_email: str, to_phone: str) -> list[sa.TextClause]:
    """The statements that move a column from one desk to another, only
    where it still holds `from_*`. Exposed so a test can run exactly what the
    migration runs."""
    return [
        sa.text(
            "UPDATE platform_settings SET support_email = :to_value WHERE support_email = :from_value"
        ).bindparams(to_value=to_email, from_value=from_email),
        sa.text(
            "UPDATE platform_settings SET support_phone = :to_value WHERE support_phone = :from_value"
        ).bindparams(to_value=to_phone, from_value=from_phone),
    ]


def upgrade() -> None:
    op.alter_column("platform_settings", "support_email", server_default=NEW_EMAIL)
    op.alter_column("platform_settings", "support_phone", server_default=NEW_PHONE)
    for statement in move_support_contact(OLD_EMAIL, OLD_PHONE, NEW_EMAIL, NEW_PHONE):
        op.execute(statement)


def downgrade() -> None:
    op.alter_column("platform_settings", "support_email", server_default=OLD_EMAIL)
    op.alter_column("platform_settings", "support_phone", server_default=OLD_PHONE)
    for statement in move_support_contact(NEW_EMAIL, NEW_PHONE, OLD_EMAIL, OLD_PHONE):
        op.execute(statement)
