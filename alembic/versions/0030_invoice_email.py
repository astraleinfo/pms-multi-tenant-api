"""invoices emailed to patients

A clinic can now email an invoice, PDF attached, to the patient it bills. The
mail goes out through the platform's transport, so nothing secret is stored
here — only how the message reads and where a reply goes (`tenant_configs`),
all optional and empty by default, which means "use the default".

`invoices` gains when it was last emailed and to whom, so the screen can say
"last sent". Every attempt is in the audit trail as well; these two columns
are only the latest success.

Revision ID: 0030_invoice_email
Revises: 0029_support_contact_default
Create Date: 2026-10-30 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0030_invoice_email"
down_revision = "0029_support_contact_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_configs", sa.Column("invoice_email_reply_to", sa.String(160), nullable=False, server_default="")
    )
    op.add_column(
        "tenant_configs", sa.Column("invoice_email_sender_name", sa.String(80), nullable=False, server_default="")
    )
    op.add_column(
        "tenant_configs", sa.Column("invoice_email_subject", sa.String(200), nullable=False, server_default="")
    )
    op.add_column(
        "tenant_configs", sa.Column("invoice_email_message", sa.Text(), nullable=False, server_default="")
    )
    op.add_column("invoices", sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("invoices", sa.Column("emailed_to", sa.String(254), nullable=True))


def downgrade() -> None:
    op.drop_column("invoices", "emailed_to")
    op.drop_column("invoices", "emailed_at")
    op.drop_column("tenant_configs", "invoice_email_message")
    op.drop_column("tenant_configs", "invoice_email_subject")
    op.drop_column("tenant_configs", "invoice_email_sender_name")
    op.drop_column("tenant_configs", "invoice_email_reply_to")
