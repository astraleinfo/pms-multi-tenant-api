"""platform billing — subscriptions, add-ons, invoices, payments, webhooks

What a clinic pays *us*, which is a different ledger from what a clinic charges
its patients. `invoices` and `payments` were already taken by that other ledger,
so the tables here are `subscription_`-prefixed; nothing is renamed, and no
existing row is touched.

Three things in this migration are worth reading rather than skimming:

*   **`billing_cycles` is half-open.** `period_end` is the first day of the
    *next* cycle, so `period_end - period_start` is the real length of the
    month — 28, 29, 30 or 31 — and proration never assumes a number.
*   **An issued invoice is immutable, below the application.** A trigger
    refuses any change to the number, the currency or any money column once an
    invoice leaves `draft`, and `subscription_invoice_items` has rules against
    UPDATE and DELETE entirely, exactly as `audit_events` does. The application
    role is not trusted to remember this.
*   **Duplicates are prevented by indexes, not by checks in Python.** One live
    subscription per tenant, one cycle per `(subscription, period_start)`, one
    non-void invoice per cycle, one payment per gateway payment id, one webhook
    row per gateway event id. Every job below is idempotent because of these
    five indexes, not because it looks first.

`webhook_events` carries no `tenant_id` and gets no policy. A delivery arrives
naming a gateway id, not a clinic — there is no tenant to bind at the moment
the row is written — so it is platform-owned like `tenants` and `plans`, and it
is reachable only through the console.

Revision ID: 0009_billing
Revises: 0008_charges
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0009_billing"
down_revision = "0008_charges"
branch_labels = None
depends_on = None

CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
APP_ROLE = "pms_app"

#: The new tables whose rows belong to exactly one clinic.
OWNED_TABLES = [
    "billing_accounts",
    "subscriptions",
    "billing_cycles",
    "subscription_invoices",
    "subscription_invoice_items",
    "subscription_payments",
    "subscription_refunds",
]

MONEY = sa.Numeric(12, 2)
RATE = sa.Numeric(6, 3)

# `currency` already exists (0001). Everything else is new here.
currency = postgresql.ENUM(
    "GBP", "EUR", "USD", "INR", name="currency", create_type=False
)
plan_interval = postgresql.ENUM("monthly", "yearly", name="plan_interval", create_type=False)
tax_scope = postgresql.ENUM("any", "intra_state", "inter_state", name="tax_scope", create_type=False)
subscription_status = postgresql.ENUM(
    "pending", "trialing", "active", "past_due", "paused", "cancelled", "expired",
    name="subscription_status", create_type=False,
)
billing_cycle_status = postgresql.ENUM(
    "open", "closed", "superseded", name="billing_cycle_status", create_type=False
)
subscription_invoice_status = postgresql.ENUM(
    "draft", "issued", "partially_paid", "paid", "void", "uncollectible",
    name="subscription_invoice_status", create_type=False,
)
invoice_item_kind = postgresql.ENUM(
    "base_plan", "addon", "proration", "discount", "credit", "tax",
    name="invoice_item_kind", create_type=False,
)
payment_gateway = postgresql.ENUM(
    "razorpay", "manual", name="payment_gateway", create_type=False
)
subscription_payment_status = postgresql.ENUM(
    "created", "authorised", "captured", "failed", "refunded", "partially_refunded",
    name="subscription_payment_status", create_type=False,
)
refund_status = postgresql.ENUM(
    "pending", "processed", "failed", name="refund_status", create_type=False
)
webhook_status = postgresql.ENUM(
    "received", "processed", "ignored", "failed", "rejected",
    name="webhook_status", create_type=False,
)

NEW_ENUMS = (
    plan_interval, tax_scope, subscription_status, billing_cycle_status,
    subscription_invoice_status, invoice_item_kind, payment_gateway,
    subscription_payment_status, refund_status, webhook_status,
)

#: Invoice numbers are platform-wide, not per clinic: `nextval` is atomic, so
#: two renewal jobs running at once cannot mint the same number, and no row has
#: to be locked to find out what the last one was. The clinic's own
#: `tenant_configs.invoice_sequence` is left alone — it numbers a different
#: ledger, and sharing a counter between the two would be a bug that only shows
#: up in an audit.
INVOICE_SEQUENCE = "subscription_invoice_number_seq"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def _tenant_column() -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    bind = op.get_bind()
    for enum in NEW_ENUMS:
        enum.create(bind, checkfirst=True)

    op.execute(f"CREATE SEQUENCE IF NOT EXISTS {INVOICE_SEQUENCE} START WITH 1000")

    # --- catalogue ---------------------------------------------------------
    op.create_table(
        "plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("interval", plan_interval, nullable=False, server_default="monthly"),
        sa.Column("amount", MONEY, nullable=False, server_default="0"),
        sa.Column("currency", currency, nullable=False, server_default="INR"),
        sa.Column("trial_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("razorpay_plan_id", sa.String(80), nullable=True, unique=True),
        *_timestamps(),
        sa.CheckConstraint("amount >= 0", name="ck_plan_amount"),
        sa.CheckConstraint("trial_days >= 0", name="ck_plan_trial_days"),
        sa.CheckConstraint("code ~ '^[a-z0-9][a-z0-9_-]{1,39}$'", name="ck_plan_code_shape"),
    )
    op.create_index("ix_plans_code", "plans", ["code"], unique=True)

    op.create_table(
        "plan_features",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "plan_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plans.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "feature_key", sa.String(40),
            sa.ForeignKey("features.key", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("included", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("addon_amount", MONEY, nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("plan_id", "feature_key", name="uq_plan_feature"),
        sa.CheckConstraint("addon_amount is null or addon_amount >= 0", name="ck_plan_feature_amount"),
        sa.CheckConstraint(
            "not (included and addon_amount is not null)", name="ck_plan_feature_included_free"
        ),
    )
    op.create_index("ix_plan_features_plan_id", "plan_features", ["plan_id"])

    op.create_table(
        "tax_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("percent", RATE, nullable=False, server_default="0"),
        sa.Column("scope", tax_scope, nullable=False, server_default="any"),
        sa.Column("country", sa.String(2), nullable=False, server_default=""),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamps(),
        sa.UniqueConstraint("code", "effective_from", name="uq_tax_rate_code_from"),
        sa.CheckConstraint("percent >= 0 and percent <= 100", name="ck_tax_rate_percent"),
        sa.CheckConstraint(
            "effective_to is null or effective_to > effective_from", name="ck_tax_rate_window"
        ),
    )
    op.create_index("ix_tax_rates_lookup", "tax_rates", ["active", "country", "scope", "effective_from"])

    # --- the agreement -----------------------------------------------------
    op.create_table(
        "billing_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_column(),
        sa.Column("legal_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("billing_email", sa.String(160), nullable=False, server_default=""),
        sa.Column("phone", sa.String(40), nullable=False, server_default=""),
        sa.Column("address", sa.Text(), nullable=False, server_default=""),
        sa.Column("tax_id", sa.String(40), nullable=False, server_default=""),
        sa.Column("country", sa.String(2), nullable=False, server_default=""),
        sa.Column("place_of_supply", sa.String(60), nullable=False, server_default=""),
        sa.Column("currency", currency, nullable=False, server_default="INR"),
        sa.Column("razorpay_customer_id", sa.String(80), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", name="uq_billing_account_tenant"),
        sa.UniqueConstraint("razorpay_customer_id", name="uq_billing_account_rzp_customer"),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_column(),
        sa.Column(
            "plan_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("status", subscription_status, nullable=False, server_default="pending"),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("anchor_day", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("trial_ends_on", sa.Date(), nullable=True),
        sa.Column("cancel_at_cycle_end", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("cancelled_at", sa.Date(), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gateway", payment_gateway, nullable=False, server_default="razorpay"),
        sa.Column("razorpay_subscription_id", sa.String(80), nullable=True),
        sa.Column("gateway_status", sa.String(40), nullable=False, server_default=""),
        sa.Column("gateway_synced_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("razorpay_subscription_id", name="uq_subscription_rzp_id"),
        sa.CheckConstraint(
            "cancelled_at is null or cancelled_at >= started_on", name="ck_subscription_cancel_order"
        ),
        sa.CheckConstraint("anchor_day between 1 and 31", name="ck_subscription_anchor"),
    )
    op.create_index("ix_subscriptions_plan_id", "subscriptions", ["plan_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])
    # One live subscription per clinic. Two browser tabs subscribing at once
    # meet this index, not a race.
    op.create_index(
        "uq_subscriptions_one_live_per_tenant",
        "subscriptions",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status not in ('cancelled', 'expired')"),
    )

    op.create_table(
        "billing_cycles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_column(),
        sa.Column(
            "subscription_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", billing_cycle_status, nullable=False, server_default="open"),
        sa.Column(
            "plan_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("plan_amount", MONEY, nullable=False, server_default="0"),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("subscription_id", "period_start", name="uq_billing_cycle_period"),
        sa.UniqueConstraint("subscription_id", "sequence", name="uq_billing_cycle_sequence"),
        sa.CheckConstraint("period_end > period_start", name="ck_billing_cycle_window"),
        sa.CheckConstraint("sequence >= 1", name="ck_billing_cycle_sequence"),
    )
    op.create_index("ix_billing_cycles_subscription_id", "billing_cycles", ["subscription_id"])
    op.create_index("ix_billing_cycles_open", "billing_cycles", ["status", "period_end"])

    # --- the money ---------------------------------------------------------
    op.create_table(
        "subscription_invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_column(),
        sa.Column(
            "subscription_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "billing_cycle_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_cycles.id", ondelete="RESTRICT"), nullable=True,
        ),
        sa.Column("number", sa.String(40), nullable=False),
        sa.Column("status", subscription_invoice_status, nullable=False, server_default="draft"),
        sa.Column("currency", currency, nullable=False, server_default="INR"),
        sa.Column("issued_on", sa.Date(), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("subtotal", MONEY, nullable=False, server_default="0"),
        sa.Column("discount_total", MONEY, nullable=False, server_default="0"),
        sa.Column("tax_total", MONEY, nullable=False, server_default="0"),
        sa.Column("total", MONEY, nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("razorpay_invoice_id", sa.String(80), nullable=True),
        sa.Column("payment_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "number", name="uq_subscription_invoice_number"),
        sa.UniqueConstraint("razorpay_invoice_id", name="uq_subscription_invoice_rzp_id"),
        sa.CheckConstraint("subtotal >= 0", name="ck_sub_invoice_subtotal"),
        sa.CheckConstraint("discount_total >= 0", name="ck_sub_invoice_discount"),
        sa.CheckConstraint("tax_total >= 0", name="ck_sub_invoice_tax"),
        sa.CheckConstraint("total >= 0", name="ck_sub_invoice_total"),
        sa.CheckConstraint("due_on >= issued_on", name="ck_sub_invoice_due"),
    )
    op.create_index("ix_subscription_invoices_subscription_id", "subscription_invoices", ["subscription_id"])
    op.create_index("ix_subscription_invoices_tenant_status", "subscription_invoices", ["tenant_id", "status"])
    op.create_index("ix_subscription_invoices_issued", "subscription_invoices", ["issued_on"])
    # The duplicate-invoice guard. A cycle may accumulate void invoices; it may
    # only ever have one that counts.
    op.create_index(
        "uq_subscription_invoice_per_cycle",
        "subscription_invoices",
        ["billing_cycle_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'void'"),
    )

    op.create_table(
        "subscription_invoice_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_column(),
        sa.Column(
            "invoice_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscription_invoices.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind", invoice_item_kind, nullable=False),
        sa.Column("description", sa.String(240), nullable=False),
        sa.Column(
            "feature_key", sa.String(40),
            sa.ForeignKey("features.key", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit_amount", MONEY, nullable=False, server_default="0"),
        sa.Column("amount", MONEY, nullable=False, server_default="0"),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("prorated_days", sa.Integer(), nullable=True),
        sa.Column("cycle_days", sa.Integer(), nullable=True),
        sa.Column("tax_code", sa.String(30), nullable=False, server_default=""),
        sa.Column("tax_percent", RATE, nullable=True),
        *_timestamps(),
        sa.CheckConstraint("quantity > 0", name="ck_sub_item_quantity"),
        sa.CheckConstraint(
            "(period_end is null and period_start is null) or period_end > period_start",
            name="ck_sub_item_window",
        ),
        sa.CheckConstraint(
            "prorated_days is null or (prorated_days >= 0 and cycle_days > 0)",
            name="ck_sub_item_proration",
        ),
    )
    op.create_index("ix_sub_invoice_items_invoice", "subscription_invoice_items", ["invoice_id", "position"])
    op.create_index(
        "ix_sub_invoice_items_feature", "subscription_invoice_items", ["tenant_id", "feature_key"]
    )

    op.create_table(
        "subscription_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_column(),
        sa.Column(
            "invoice_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscription_invoices.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("gateway", payment_gateway, nullable=False, server_default="razorpay"),
        sa.Column("gateway_payment_id", sa.String(80), nullable=True),
        sa.Column("gateway_order_id", sa.String(80), nullable=True),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("amount_refunded", MONEY, nullable=False, server_default="0"),
        sa.Column("currency", currency, nullable=False, server_default="INR"),
        sa.Column("status", subscription_payment_status, nullable=False, server_default="created"),
        sa.Column("method", sa.String(40), nullable=False, server_default=""),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=False, server_default=""),
        *_timestamps(),
        # Not per tenant. A unique index is not filtered by row-level security,
        # so this also stops one gateway payment being booked to two clinics.
        sa.UniqueConstraint("gateway", "gateway_payment_id", name="uq_subscription_payment_gateway_id"),
        sa.CheckConstraint("amount > 0", name="ck_subscription_payment_amount"),
        sa.CheckConstraint("amount_refunded >= 0", name="ck_subscription_payment_refunded"),
        sa.CheckConstraint("amount_refunded <= amount", name="ck_subscription_payment_refund_cap"),
    )
    op.create_index("ix_subscription_payments_invoice", "subscription_payments", ["invoice_id"])
    op.create_index("ix_subscription_payments_status", "subscription_payments", ["tenant_id", "status"])
    op.create_index("ix_subscription_payments_gateway_order_id", "subscription_payments", ["gateway_order_id"])

    op.create_table(
        "subscription_refunds",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_column(),
        sa.Column(
            "payment_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscription_payments.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("gateway", payment_gateway, nullable=False, server_default="razorpay"),
        sa.Column("gateway_refund_id", sa.String(80), nullable=True),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("currency", currency, nullable=False, server_default="INR"),
        sa.Column("status", refund_status, nullable=False, server_default="pending"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("gateway", "gateway_refund_id", name="uq_subscription_refund_gateway_id"),
        sa.CheckConstraint("amount > 0", name="ck_subscription_refund_amount"),
    )
    op.create_index("ix_subscription_refunds_payment", "subscription_refunds", ["payment_id"])

    # --- what the gateway said ---------------------------------------------
    op.create_table(
        "webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("gateway", payment_gateway, nullable=False, server_default="razorpay"),
        sa.Column("event_id", sa.String(120), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("entity_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("signature_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", webhook_status, nullable=False, server_default="received"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        # Redelivery is a no-op because of this index, not because anything
        # looked first. Razorpay retries for 24 hours.
        sa.UniqueConstraint("gateway", "event_id", name="uq_webhook_event_id"),
        sa.CheckConstraint("attempts >= 0", name="ck_webhook_attempts"),
    )
    op.create_index("ix_webhook_events_pending", "webhook_events", ["status", "created_at"])
    op.create_index("ix_webhook_events_type", "webhook_events", ["gateway", "event_type"])
    op.create_index("ix_webhook_events_entity", "webhook_events", ["entity_id"])

    # --- add-on billing on the entitlement row -----------------------------
    #
    # `tenant_features` already answers "is this module on". These three
    # columns let it also answer "is it paid for, and until when", so the two
    # cannot drift apart. Existing rows get NULL, which is honest: they predate
    # billing and are owed nothing.
    op.add_column("tenant_features", sa.Column("charged_through", sa.Date(), nullable=True))
    op.add_column("tenant_features", sa.Column("cancel_at", sa.Date(), nullable=True))
    op.add_column(
        "tenant_features", sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_tenant_feature_cancel_order",
        "tenant_features",
        "cancel_at is null or activated_at is null or cancel_at >= activated_at",
    )
    op.create_index("ix_tenant_features_cancel_at", "tenant_features", ["cancel_at"])

    # --- the per-tenant indexes the mixin adds ------------------------------
    for table in OWNED_TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        op.create_index(f"ix_{table}_tenant_id_id", table, ["tenant_id", "id"])

    # --- row-level security -------------------------------------------------
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

    # --- immutability -------------------------------------------------------
    #
    # An invoice is a statement of what was charged on a day. Once it is issued
    # its money cannot move, and this is enforced here rather than remembered
    # in a service: the trigger fires for the migration role too.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION subscription_invoice_freeze() RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'draft' THEN
                RETURN NEW;
            END IF;
            IF NEW.number       IS DISTINCT FROM OLD.number
            OR NEW.currency     IS DISTINCT FROM OLD.currency
            OR NEW.subtotal     IS DISTINCT FROM OLD.subtotal
            OR NEW.discount_total IS DISTINCT FROM OLD.discount_total
            OR NEW.tax_total    IS DISTINCT FROM OLD.tax_total
            OR NEW.total        IS DISTINCT FROM OLD.total
            OR NEW.period_start IS DISTINCT FROM OLD.period_start
            OR NEW.period_end   IS DISTINCT FROM OLD.period_end
            OR NEW.issued_on    IS DISTINCT FROM OLD.issued_on
            OR NEW.subscription_id IS DISTINCT FROM OLD.subscription_id
            OR NEW.billing_cycle_id IS DISTINCT FROM OLD.billing_cycle_id
            OR NEW.tenant_id    IS DISTINCT FROM OLD.tenant_id THEN
                RAISE EXCEPTION
                    'subscription invoice % is issued and its figures cannot be changed', OLD.number
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_subscription_invoice_freeze
        BEFORE UPDATE ON subscription_invoices
        FOR EACH ROW EXECUTE FUNCTION subscription_invoice_freeze()
        """
    )
    # Line items are written once and never touched again — the same treatment
    # `audit_events` gets, for the same reason.
    op.execute(
        "CREATE RULE subscription_invoice_items_no_update AS "
        "ON UPDATE TO subscription_invoice_items DO INSTEAD NOTHING"
    )
    op.execute(
        "CREATE RULE subscription_invoice_items_no_delete AS "
        "ON DELETE TO subscription_invoice_items DO INSTEAD NOTHING"
    )

    # --- grants -------------------------------------------------------------
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}';
                EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}';
                -- The catalogue is the platform's to write. The API reads it.
                EXECUTE 'REVOKE INSERT, UPDATE, DELETE ON plans, plan_features, tax_rates FROM {APP_ROLE}';
                -- History is not edited, here either.
                EXECUTE 'REVOKE UPDATE, DELETE ON subscription_invoice_items FROM {APP_ROLE}';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP RULE IF EXISTS subscription_invoice_items_no_update ON subscription_invoice_items")
    op.execute("DROP RULE IF EXISTS subscription_invoice_items_no_delete ON subscription_invoice_items")
    op.execute("DROP TRIGGER IF EXISTS trg_subscription_invoice_freeze ON subscription_invoices")
    op.execute("DROP FUNCTION IF EXISTS subscription_invoice_freeze()")

    for table in OWNED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")

    op.drop_index("ix_tenant_features_cancel_at", table_name="tenant_features")
    op.drop_constraint("ck_tenant_feature_cancel_order", "tenant_features", type_="check")
    op.drop_column("tenant_features", "deactivated_at")
    op.drop_column("tenant_features", "cancel_at")
    op.drop_column("tenant_features", "charged_through")

    for table in (
        "webhook_events",
        "subscription_refunds",
        "subscription_payments",
        "subscription_invoice_items",
        "subscription_invoices",
        "billing_cycles",
        "subscriptions",
        "billing_accounts",
        "tax_rates",
        "plan_features",
        "plans",
    ):
        op.drop_table(table)

    op.execute(f"DROP SEQUENCE IF EXISTS {INVOICE_SEQUENCE}")
    for enum in NEW_ENUMS:
        enum.drop(op.get_bind(), checkfirst=True)
