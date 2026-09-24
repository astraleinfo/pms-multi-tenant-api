"""platform settings

One new table: the platform's own configuration, the sibling of
TenantConfig but with no tenant behind it. Seeded with exactly one row at a
fixed id so "singleton" is a database fact (the id is the primary key, and
nothing ever inserts a second one), not an application convention to trust.

The six timing columns (invite/reset TTL, reminder lead hours, rent due/
grace/reminder-before-due) ship now so a later pass never needs a second
migration for them, but nothing reads them yet — see the design spec.

Revision ID: 0017_platform_settings
Revises: 0016_patient_weight_field
Create Date: 2026-09-23 00:00:00.000000
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0017_platform_settings'
down_revision = '0016_patient_weight_field'
branch_labels = None
depends_on = None

PLATFORM_SETTINGS_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

currency = postgresql.ENUM("GBP", "EUR", "USD", "INR", name="currency", create_type=False)


def upgrade() -> None:
    op.create_table(
        "platform_settings",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("support_email", sa.String(200), nullable=False, server_default="support@astrale.health"),
        sa.Column("support_phone", sa.String(40), nullable=False, server_default="+91 80 4718 2200"),
        sa.Column("support_hours", sa.String(200), nullable=False, server_default="Monday to Saturday, 9am – 7pm IST"),
        sa.Column("default_day_start", sa.String(5), nullable=False, server_default="09:00"),
        sa.Column("default_day_end", sa.String(5), nullable=False, server_default="17:30"),
        sa.Column("default_slot_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("default_duration", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("default_tax_pct", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("default_payment_terms_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column("default_expiry_alert_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("default_currency", currency, nullable=False, server_default="INR"),
        sa.Column("invite_token_ttl_seconds", sa.Integer(), nullable=False, server_default="604800"),
        sa.Column("reset_token_ttl_seconds", sa.Integer(), nullable=False, server_default="7200"),
        sa.Column("reminder_lead_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("rent_due_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("rent_grace_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("rent_reminder_days_before_due", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "id = '00000000-0000-0000-0000-000000000001'", name="ck_platform_settings_singleton"
        ),
    )
    op.execute(
        sa.text("INSERT INTO platform_settings (id) VALUES (:id)").bindparams(id=PLATFORM_SETTINGS_ID)
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pms_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON platform_settings TO pms_app;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_table("platform_settings")
