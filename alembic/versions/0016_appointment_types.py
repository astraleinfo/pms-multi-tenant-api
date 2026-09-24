"""appointment types: the kinds of visit a clinic books, each with its length

A booking of a type is that long, and the diary offers it at that interval —
a 45-minute procedure at 45-minute steps, a 15-minute review at 15-minute ones.
Stored on the clinic's configuration as a small ordered list, replaced whole.
Existing clinics are given an ordinary visit (their default length) and a
15-minute follow-up, so the diary never opens with nothing to book.

Revision ID: 0016_appointment_types
Revises: 0015_subscription_rent
Create Date: 2026-09-24 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0016_appointment_types'
down_revision = '0015_subscription_rent'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'tenant_configs',
        sa.Column(
            'appointment_types',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # tenant_configs has FORCEd row-level security, so even the owner sees a
    # clinic's row only with that clinic set on the session — one at a time.
    conn = op.get_bind()
    tenant_ids = conn.execute(sa.text("select id from tenants")).scalars().all()
    for tenant_id in tenant_ids:
        conn.execute(sa.text("select set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            sa.text(
                """
                update tenant_configs
                   set appointment_types = jsonb_build_array(
                         jsonb_build_object('id', 'consultation', 'name', 'Consultation', 'minutes', default_duration),
                         jsonb_build_object('id', 'follow-up', 'name', 'Follow-up', 'minutes', 15)
                       )
                 where appointment_types = '[]'::jsonb
                """
            )
        )
    conn.execute(sa.text("select set_config('app.tenant_id', '', true)"))


def downgrade() -> None:
    op.drop_column('tenant_configs', 'appointment_types')
