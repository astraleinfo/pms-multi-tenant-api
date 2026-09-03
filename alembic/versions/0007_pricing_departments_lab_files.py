"""admin pricing, separate lab/pharmacy billing, and lab attachments

Four changes that belong together because they are all about money and the
paperwork behind it:

*   `inventory_items` learns how its selling price was arrived at — a typed
    price, a margin over cost, or the printed MRP — alongside the cost and MRP
    themselves. `unit_price` keeps its meaning: what one unit bills at.
*   `service_prices` is the tariff a practice admin keeps: the consultation,
    each procedure, and each lab test or scan by code. Billing reads it instead
    of carrying a number in the code.
*   `invoices.department` says which counter raised an invoice, so the lab and
    the pharmacy bill separately from the clinic.
*   `lab_attachments` holds the files a lab order collects — a scan, a printout,
    a signed report — as tenant-owned rows, which is what puts them behind the
    same row-level security as the order itself.

Every existing row is given the value it already implied: `pricing_mode` is
`price` (the number someone typed), and every invoice that predates this is a
clinic invoice.

Revision ID: 0007_pricing
Revises: 0006_invitations
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007_pricing"
down_revision = "0006_invitations"
branch_labels = None
depends_on = None

CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
APP_ROLE = "pms_app"
OWNED_TABLES = ["service_prices", "lab_attachments"]

pricing_mode = postgresql.ENUM("price", "percent", "mrp", name="pricing_mode", create_type=False)
service_kind = postgresql.ENUM(
    "consultation", "procedure", "lab", "other", name="service_kind", create_type=False
)
billing_department = postgresql.ENUM(
    "clinic", "lab", "pharmacy", name="billing_department", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    pricing_mode.create(bind, checkfirst=True)
    service_kind.create(bind, checkfirst=True)
    billing_department.create(bind, checkfirst=True)

    # --- how a stock item is priced ----------------------------------------
    op.add_column(
        "inventory_items",
        sa.Column("cost_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
    )
    op.add_column(
        "inventory_items", sa.Column("mrp", sa.Numeric(12, 2), nullable=False, server_default="0")
    )
    op.add_column(
        "inventory_items",
        sa.Column("margin_pct", sa.Numeric(6, 2), nullable=False, server_default="0"),
    )
    op.add_column(
        "inventory_items",
        sa.Column("pricing_mode", pricing_mode, nullable=False, server_default="price"),
    )
    op.create_check_constraint("ck_item_unit_price", "inventory_items", "unit_price >= 0")
    op.create_check_constraint("ck_item_cost_price", "inventory_items", "cost_price >= 0")
    op.create_check_constraint("ck_item_mrp", "inventory_items", "mrp >= 0")
    op.create_check_constraint(
        "ck_item_margin", "inventory_items", "margin_pct >= -100 and margin_pct <= 1000"
    )

    # --- the tariff the admin keeps ----------------------------------------
    op.create_table(
        "service_prices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", service_kind, nullable=False),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("tax_pct", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "kind", "code", name="uq_service_price_kind_code"),
        sa.CheckConstraint("price >= 0", name="ck_service_price_amount"),
        sa.CheckConstraint("tax_pct >= 0 and tax_pct <= 100", name="ck_service_price_tax"),
    )
    op.create_index("ix_service_prices_tenant_id", "service_prices", ["tenant_id"])
    op.create_index("ix_service_prices_tenant_id_id", "service_prices", ["tenant_id", "id"])

    # --- which counter an invoice belongs to -------------------------------
    op.add_column(
        "invoices",
        sa.Column("department", billing_department, nullable=False, server_default="clinic"),
    )
    op.create_index("ix_invoices_tenant_department", "invoices", ["tenant_id", "department"])

    # --- files a lab order collects ----------------------------------------
    op.create_table(
        "lab_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lab_order_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("lab_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("content_type", sa.String(120), nullable=False,
                  server_default="application/octet-stream"),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("size_bytes > 0", name="ck_lab_attachment_size"),
    )
    op.create_index("ix_lab_attachments_tenant_id", "lab_attachments", ["tenant_id"])
    op.create_index("ix_lab_attachments_tenant_id_id", "lab_attachments", ["tenant_id", "id"])
    op.create_index("ix_lab_attachments_tenant_order", "lab_attachments", ["tenant_id", "lab_order_id"])

    for table in OWNED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = {CURRENT})
                WITH CHECK (tenant_id = {CURRENT})
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def downgrade() -> None:
    for table in reversed(OWNED_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("lab_attachments")
    op.drop_index("ix_invoices_tenant_department", table_name="invoices")
    op.drop_column("invoices", "department")
    op.drop_table("service_prices")

    op.drop_constraint("ck_item_margin", "inventory_items", type_="check")
    op.drop_constraint("ck_item_mrp", "inventory_items", type_="check")
    op.drop_constraint("ck_item_cost_price", "inventory_items", type_="check")
    op.drop_constraint("ck_item_unit_price", "inventory_items", type_="check")
    op.drop_column("inventory_items", "pricing_mode")
    op.drop_column("inventory_items", "margin_pct")
    op.drop_column("inventory_items", "mrp")
    op.drop_column("inventory_items", "cost_price")

    bind = op.get_bind()
    for enum in (billing_department, service_kind, pricing_mode):
        enum.drop(bind, checkfirst=True)
