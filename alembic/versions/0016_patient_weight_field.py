"""patient weight field

Weight is printed on the consultation record alongside blood group, but it
gets no column of its own: it goes through the same door as `bruxism` or
`last_cleaning` — declared once as a `field_definitions` row and carried in
`clinical_ext` from then on. Declaring it is what makes it survive at all;
`validate_dynamic_values` drops anything not in the resolved schema.

`scope="common"` (not `clinical_type`) because a body has a weight whatever
the clinic treats.

Revision ID: 0016_patient_weight_field
Revises: 0015_subscription_rent
Create Date: 2026-09-23 00:00:00.000000
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0016_patient_weight_field'
down_revision = '0015_subscription_rent'
branch_labels = None
depends_on = None

FIELD_ID = uuid.UUID("00000000-0000-0000-0000-0000000000fd")

_field_scope = postgresql.ENUM(name="field_scope", create_type=False)
_field_entity = postgresql.ENUM(name="field_entity", create_type=False)
_field_type = postgresql.ENUM(name="field_type", create_type=False)

field_definitions = sa.table(
    "field_definitions",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("scope", _field_scope),
    sa.column("clinical_type", sa.String),
    sa.column("tenant_id", postgresql.UUID(as_uuid=True)),
    sa.column("entity", _field_entity),
    sa.column("field_key", sa.String),
    sa.column("label", sa.String),
    sa.column("type", _field_type),
    sa.column("required", sa.Boolean),
    sa.column("order", sa.Integer),
    sa.column("span", sa.Integer),
    sa.column("group", sa.String),
)


def upgrade() -> None:
    op.execute(
        field_definitions.insert().values(
            id=FIELD_ID,
            scope="common",
            clinical_type=None,
            tenant_id=None,
            entity="patient",
            field_key="weight",
            label="Weight (kg)",
            type="number",
            required=False,
            order=75,
            span=1,
            group="Clinical background",
        )
    )


def downgrade() -> None:
    op.execute(field_definitions.delete().where(field_definitions.c.id == FIELD_ID))
