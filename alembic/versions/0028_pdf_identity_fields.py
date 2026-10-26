"""print identity fields, and two more vitals

What a printed document says about who issued it: the clinic's registration
number, GSTIN and brand colour (`tenant_configs`), and a doctor's
qualifications and registration number (`users`). All optional — a clinic that
sets none of them prints exactly what it printed before — except the brand
colour, which always has one (the product's teal) so a branded document never
has to ask whether it has a colour to use.

Temperature and SpO2 join blood pressure and pulse as vitals. Like
`0016_patient_weight_field` they get no column: each is declared once as a
`field_definitions` row and carried in `clinical_ext` from then on, because
`validate_dynamic_values` drops anything not in the resolved schema.
`scope="common"` — every body has a temperature whatever the clinic treats.

Revision ID: 0028_pdf_identity_fields
Revises: 0027_link_email_status
Create Date: 2026-10-27 00:00:00.000000
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0028_pdf_identity_fields"
down_revision = "0027_link_email_status"
branch_labels = None
depends_on = None

TEMPERATURE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000fe")
SPO2_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

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


def _vital(row_id: uuid.UUID, key: str, label: str, order: int) -> dict:
    return {
        "id": row_id, "scope": "common", "clinical_type": None, "tenant_id": None,
        "entity": "encounter", "field_key": key, "label": label, "type": "number",
        "required": False, "order": order, "span": 1, "group": "Vitals",
    }


def upgrade() -> None:
    op.add_column("tenant_configs", sa.Column("registration_no", sa.String(60), nullable=False, server_default=""))
    op.add_column("tenant_configs", sa.Column("gstin", sa.String(20), nullable=False, server_default=""))
    op.add_column(
        "tenant_configs", sa.Column("brand_color", sa.String(7), nullable=False, server_default="#0f766e")
    )
    op.add_column("users", sa.Column("qualifications", sa.String(160), nullable=True))
    op.add_column("users", sa.Column("registration_no", sa.String(60), nullable=True))

    op.execute(field_definitions.insert().values(_vital(TEMPERATURE_ID, "temperature", "Temperature (°F)", 25)))
    op.execute(field_definitions.insert().values(_vital(SPO2_ID, "spo2", "SpO₂ (%)", 35)))


def downgrade() -> None:
    op.execute(field_definitions.delete().where(field_definitions.c.id.in_([TEMPERATURE_ID, SPO2_ID])))
    op.drop_column("users", "registration_no")
    op.drop_column("users", "qualifications")
    op.drop_column("tenant_configs", "brand_color")
    op.drop_column("tenant_configs", "gstin")
    op.drop_column("tenant_configs", "registration_no")
