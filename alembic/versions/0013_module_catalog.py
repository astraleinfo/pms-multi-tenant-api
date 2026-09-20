"""module catalogue, per-module pricing, subscription items and the cart

The structural half of selling modules individually. Nothing here changes what
any existing clinic is billed: every table is new, every backfilled row records
a figure that was already being charged, and no service reads any of it yet.

Four ideas, and the first is the one the rest depend on:

*   **A module's price is now pinned to the subscription, not looked up.**
    Until now "what does this clinic pay for the lab" was answered by walking
    `plan_features` → `features.price_monthly` on every read, so the figure
    moved whenever the catalogue moved. `subscription_items` records the
    number at the moment of purchase. That is what makes grandfathering, a
    yearly module beside a monthly one, and a per-item trial possible at all.
*   **`unit_amount` is the authority; `price_id` is provenance.** The pinned
    figure is the one that bills. `price_id` says which catalogue row it came
    from and is null when a `plan_features` override, or this migration's
    backfill, set it instead — recording "priced from the catalogue" when it
    was not would be a lie that only surfaces in an audit.
*   **The cart stores references, never amounts.** `cart_items` has no money
    column, deliberately. A cart is a list of things the clinic intends to buy;
    it is priced on read and re-priced at checkout, so a price that moves
    between the two is caught rather than honoured.
*   **Entitlement is still `tenant_features`.** This migration does not create
    a second answer to "is this module switched on" — `app/api/deps.py` keeps
    reading the row it always read. `subscription_items` answers the different
    question of what is being paid for, and step 2 makes the first a projection
    of the second inside one transaction.

Three duplicates are prevented by indexes rather than by checks in Python,
following `uq_subscriptions_one_live_per_tenant`: one live item per
(tenant, module), one open cart per tenant, one line per (cart, module). Two
browser tabs pressing Add to Cart at once meet an index, not a race.

**The backfill lifts `FORCE ROW LEVEL SECURITY` while it runs, and it has to.**
Forced RLS binds the table owner too, so `pms_owner` — which is what alembic
connects as, and which sets no `app.tenant_id` — reads *zero* rows from
`tenant_features`, `subscriptions` and `billing_cycles`.
A backfill written without this does not fail: it selects nothing, inserts
nothing, reports success, and leaves every clinic without the items it is
supposed to have. The window is re-forced in the same transaction, and a
`DO` block at the end refuses to let the migration commit if the number of
rows written does not match the number of entitlements that qualified — so
the silent version of this mistake cannot come back.

`features.price_monthly` is deliberately left in place. Step 2 moves the
services onto `module_prices` and drops it then; dropping it here would break
`app/schemas/tenancy.py` and the seed loader for no gain.

Revision ID: 0013_module_catalog
Revises: 0012_billing
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013_module_catalog"
down_revision = "0012_billing"
branch_labels = None
depends_on = None

CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
APP_ROLE = "pms_app"

#: The new tables whose rows belong to exactly one clinic. Read by
#: `test_every_tenant_owned_table_has_a_policy`, which parses this name.
OWNED_TABLES = [
    "subscription_items",
    "carts",
    "cart_items",
]

#: The new catalogue tables. Platform-owned, like `plans` and `tax_rates`:
#: no tenant, no policy, and the application role may read but not write.
CATALOGUE_TABLES = ["module_prices", "module_listings"]

MONEY = sa.Numeric(12, 2)

# Both already exist (0001 and 0012). Reused rather than re-spelled: a module
# bills monthly or yearly on exactly the same vocabulary a plan does.
currency = postgresql.ENUM("GBP", "EUR", "USD", "INR", name="currency", create_type=False)
plan_interval = postgresql.ENUM("monthly", "yearly", name="plan_interval", create_type=False)

module_listing_status = postgresql.ENUM(
    "draft", "published", "retired", name="module_listing_status", create_type=False
)
subscription_item_status = postgresql.ENUM(
    "trialing", "active", "pending_cancel", "cancelled",
    name="subscription_item_status", create_type=False,
)
cart_status = postgresql.ENUM(
    "open", "converted", "abandoned", name="cart_status", create_type=False
)

NEW_ENUMS = (module_listing_status, subscription_item_status, cart_status)


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


def _feature_column(ondelete: str) -> sa.Column:
    return sa.Column(
        "feature_key", sa.String(40), sa.ForeignKey("features.key", ondelete=ondelete), nullable=False
    )


def upgrade() -> None:
    bind = op.get_bind()
    for enum in NEW_ENUMS:
        enum.create(bind, checkfirst=True)

    # --- the catalogue ------------------------------------------------------
    #
    # One row per (module, interval, currency) that is for sale. Yearly is a
    # row, not `12 * monthly`: an annual commitment is normally discounted, and
    # deriving it would make that discount impossible to express as data.
    op.create_table(
        "module_prices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _feature_column("CASCADE"),
        sa.Column("interval", plan_interval, nullable=False, server_default="monthly"),
        sa.Column("currency", currency, nullable=False, server_default="INR"),
        sa.Column("amount", MONEY, nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("effective_from", sa.Date(), nullable=False, server_default=sa.func.current_date()),
        sa.Column("razorpay_plan_id", sa.String(80), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            "feature_key", "interval", "currency", "effective_from", name="uq_module_price_effective"
        ),
        sa.UniqueConstraint("razorpay_plan_id", name="uq_module_price_rzp_plan"),
        sa.CheckConstraint("amount >= 0", name="ck_module_price_amount"),
    )
    # Exactly one price a module is sold at today, per interval and currency.
    # A superseded price stays in the table — items that pinned it still point
    # at it — and is simply no longer `active`.
    op.create_index(
        "uq_module_prices_one_active",
        "module_prices",
        ["feature_key", "interval", "currency"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_index("ix_module_prices_feature_key", "module_prices", ["feature_key"])

    # What the module's page shows: the description, the demo video, the
    # screenshots. Its own table rather than columns on `features`, because
    # that row is an FK target the feature guard reads on request paths and
    # has no business carrying a blob of marketing copy.
    op.create_table(
        "module_listings",
        _feature_column("CASCADE"),
        sa.Column("headline", sa.String(160), nullable=False, server_default=""),
        sa.Column("long_description", sa.Text(), nullable=False, server_default=""),
        #: Ordered bullets — the feature list on the module's page.
        sa.Column("highlights", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("video_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("video_poster_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("video_duration_s", sa.Integer(), nullable=True),
        #: Ordered `{url, alt, caption}` objects. `alt` is not optional in the
        #: application: a screenshot nobody can read is not documentation.
        sa.Column("screenshots", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("doc_links", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("category", sa.String(40), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("status", module_listing_status, nullable=False, server_default="draft"),
        sa.Column("badge", sa.String(24), nullable=False, server_default=""),
        *_timestamps(),
        sa.PrimaryKeyConstraint("feature_key", name="pk_module_listings"),
        sa.CheckConstraint("jsonb_typeof(highlights) = 'array'", name="ck_module_listing_highlights"),
        sa.CheckConstraint("jsonb_typeof(screenshots) = 'array'", name="ck_module_listing_screenshots"),
        sa.CheckConstraint("jsonb_typeof(doc_links) = 'array'", name="ck_module_listing_docs"),
        sa.CheckConstraint(
            "video_duration_s is null or video_duration_s > 0", name="ck_module_listing_duration"
        ),
    )
    op.create_index(
        "ix_module_listings_published",
        "module_listings",
        ["status", "sort_order"],
    )

    # --- what the clinic is actually paying for -----------------------------
    op.create_table(
        "subscription_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_column(),
        sa.Column(
            "subscription_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False,
        ),
        _feature_column("RESTRICT"),
        # Nullable on purpose — see the module docstring. The figure that bills
        # is `unit_amount`; this only records where it came from.
        sa.Column(
            "price_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module_prices.id", ondelete="RESTRICT"), nullable=True,
        ),
        sa.Column("interval", plan_interval, nullable=False, server_default="monthly"),
        # One today. Seats and usage add-ons become a number here rather than a
        # migration, which is the whole reason it exists before anything sets it.
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit_amount", MONEY, nullable=False, server_default="0"),
        sa.Column("currency", currency, nullable=False, server_default="INR"),
        sa.Column("status", subscription_item_status, nullable=False, server_default="active"),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("current_period_start", sa.Date(), nullable=True),
        #: Exclusive, like `billing_cycles.period_end`.
        sa.Column("current_period_end", sa.Date(), nullable=True),
        #: Exclusive. Billing is paid up to, but not including, this day — the
        #: same guarantee `tenant_features.charged_through` gives, so running
        #: the renewal job twice cannot charge twice.
        sa.Column("charged_through", sa.Date(), nullable=True),
        sa.Column("cancel_at", sa.Date(), nullable=True),
        sa.Column("trial_ends_on", sa.Date(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("quantity > 0", name="ck_subscription_item_quantity"),
        sa.CheckConstraint("unit_amount >= 0", name="ck_subscription_item_amount"),
        sa.CheckConstraint(
            "cancel_at is null or cancel_at >= started_on", name="ck_subscription_item_cancel_order"
        ),
        sa.CheckConstraint(
            "current_period_end is null or current_period_start is null "
            "or current_period_end > current_period_start",
            name="ck_subscription_item_period",
        ),
    )
    # One live item per module per clinic. Buying the lab twice is not a
    # conflict the service has to look for.
    op.create_index(
        "uq_subscription_items_one_live",
        "subscription_items",
        ["tenant_id", "feature_key"],
        unique=True,
        postgresql_where=sa.text("status <> 'cancelled'"),
    )
    op.create_index("ix_subscription_items_subscription", "subscription_items", ["subscription_id"])
    op.create_index("ix_subscription_items_status", "subscription_items", ["tenant_id", "status"])
    # The renewal job's sweep: everything paid up to before a given day.
    op.create_index(
        "ix_subscription_items_charged_through",
        "subscription_items",
        ["charged_through"],
        postgresql_where=sa.text("status <> 'cancelled'"),
    )
    op.create_index("ix_subscription_items_cancel_at", "subscription_items", ["cancel_at"])

    # --- the cart -----------------------------------------------------------
    #
    # Server-side rather than in the browser, for the same reason no request
    # body in this API carries an amount: a cart the client owns is a total the
    # client can choose.
    op.create_table(
        "carts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_column(),
        sa.Column("status", cart_status, nullable=False, server_default="open"),
        sa.Column("currency", currency, nullable=False, server_default="INR"),
        #: Held as typed. It is validated against `coupons` at quote and again
        #: at checkout; storing the resolved discount here would freeze a figure
        #: that the coupon's own rules are entitled to change.
        sa.Column("coupon_code", sa.String(40), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "converted_invoice_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscription_invoices.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index(
        "uq_carts_one_open_per_tenant",
        "carts",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index("ix_carts_status", "carts", ["tenant_id", "status"])

    op.create_table(
        "cart_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_column(),
        sa.Column(
            "cart_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("carts.id", ondelete="CASCADE"), nullable=False,
        ),
        _feature_column("CASCADE"),
        sa.Column(
            "price_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module_prices.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("interval", plan_interval, nullable=False, server_default="monthly"),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        # No money column, deliberately. The cart is priced on read.
        #
        # One line per module: adding the lab monthly and the lab yearly to the
        # same cart is a contradiction, not two lines.
        sa.UniqueConstraint("cart_id", "feature_key", name="uq_cart_item_feature"),
        sa.CheckConstraint("quantity > 0", name="ck_cart_item_quantity"),
    )
    op.create_index("ix_cart_items_cart_id", "cart_items", ["cart_id"])

    # --- tying an invoice line back to the item it billed -------------------
    #
    # Nullable: every line written before today has no item, and the renewal
    # and proration paths only start setting it in step 2. The table's
    # no-UPDATE rule is untouched by this — a line still records the item it
    # was raised for at INSERT and can never be re-pointed afterwards.
    op.add_column(
        "subscription_invoice_items",
        sa.Column(
            "subscription_item_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscription_items.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    op.create_index(
        "ix_subscription_invoice_items_item",
        "subscription_invoice_items",
        ["subscription_item_id"],
    )

    # --- the per-tenant indexes the mixin adds ------------------------------
    for table in OWNED_TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        op.create_index(f"ix_{table}_tenant_id_id", table, ["tenant_id", "id"])

    _seed_catalogue()
    _backfill_items()

    # --- row-level security -------------------------------------------------
    #
    # After the backfill, and that ordering is load-bearing: `FORCE ROW LEVEL
    # SECURITY` applies to the owner too, so an insert made under these
    # policies with no `app.tenant_id` bound writes nothing and says nothing.
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

    # --- grants -------------------------------------------------------------
    catalogue = ", ".join(CATALOGUE_TABLES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}';
                EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}';
                -- The catalogue is the platform's to write. The API reads it.
                EXECUTE 'REVOKE INSERT, UPDATE, DELETE ON {catalogue} FROM {APP_ROLE}';
                -- Re-asserted: the blanket GRANT above would otherwise hand
                -- back what 0012 took away.
                EXECUTE 'REVOKE INSERT, UPDATE, DELETE ON plans, plan_features, tax_rates FROM {APP_ROLE}';
                EXECUTE 'REVOKE UPDATE, DELETE ON subscription_invoice_items FROM {APP_ROLE}';
            END IF;
        END
        $$;
        """
    )


def _seed_catalogue() -> None:
    """Copy the prices that already exist into the new table, and open a draft
    listing for every module.

    No number is invented here. The monthly price of a module is exactly
    `features.price_monthly`, which is what `billing_service.addon_prices`
    already falls back to; a plan that overrides it keeps doing so through
    `plan_features` until step 2. **Yearly prices are deliberately not
    created** — what a year of the lab costs is a commercial decision, not
    something a migration is entitled to guess at twelve times the monthly.

    Currency is INR: it is the server default on `plans`, `billing_accounts`
    and every invoice in this deployment. Selling a module in another currency
    is another row, not another migration.
    """
    op.execute(
        """
        INSERT INTO module_prices (id, feature_key, "interval", currency, amount, active, effective_from)
        SELECT gen_random_uuid(), f.key, 'monthly', 'INR', f.price_monthly, true, current_date
        FROM features f
        WHERE NOT f.is_core AND f.price_monthly > 0
        """
    )
    op.execute(
        """
        INSERT INTO module_listings (feature_key, headline, long_description, status, sort_order)
        SELECT f.key, f.summary, '', 'draft', 100
        FROM features f
        """
    )


def _backfill_items() -> None:
    """Give every module a clinic is already paying for a row that says so.

    The amount is resolved by the same three-line fallback
    `billing_service.addon_prices` uses — an `included` plan row is free, an
    `addon_amount` overrides, otherwise `features.price_monthly` — because the
    point of a backfill is to record what is already true, not to reprice
    anybody. A module resolving to zero or to "included" gets no row: it is
    switched on, and it is not an item on a bill.

    `price_id` is filled only where the pinned figure genuinely matches a
    catalogue price, which is every clinic whose plan does not override. The
    rest keep a null and their own `unit_amount`.

    Everything below runs with forced row-level security lifted — see the
    module docstring — and re-forced before it returns.
    """
    # The tables this reads that are under forced RLS. `subscription_items`
    # is not among them: its own policies are not created until later.
    sources = ["tenant_features", "subscriptions", "billing_cycles"]
    for table in sources:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

    op.execute(f"INSERT INTO subscription_items ({_ITEM_COLUMNS}) {_ITEM_SELECT}")
    # Refuse to commit a backfill that quietly did nothing. `expected` counts
    # the entitlements that qualify by exactly the same rule the INSERT selects
    # on, so a mismatch means the two have drifted apart or something filtered
    # the rows away.
    #
    # No `try` around any of this on purpose: the migration runs in one
    # transaction, so a failure rolls the `NO FORCE` back with everything else.
    # Catching it would only hide the error behind a second one.
    op.execute(
        f"""
        DO $$
        DECLARE
            expected bigint;
            written  bigint;
        BEGIN
            SELECT count(*) INTO expected FROM ({_QUALIFYING}) q;
            SELECT count(*) INTO written FROM subscription_items;
            IF expected <> written THEN
                RAISE EXCEPTION
                    'subscription_items backfill wrote % rows for % qualifying entitlements',
                    written, expected;
            END IF;
        END
        $$;
        """
    )

    for table in sources:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


#: The one place the rule "which entitlements are a billable item" is written.
#: The INSERT selects on it and the self-check counts it, so they cannot drift.
_QUALIFYING = """
    SELECT tf.tenant_id, tf.feature_key
    FROM tenant_features tf
    JOIN features f
      ON f.key = tf.feature_key
    JOIN subscriptions s
      ON s.tenant_id = tf.tenant_id
     AND s.status NOT IN ('cancelled', 'expired')
    LEFT JOIN plan_features pf
      ON pf.plan_id = s.plan_id
     AND pf.feature_key = tf.feature_key
    WHERE tf.status IN ('active', 'trial')
      AND NOT f.is_core
      AND COALESCE(pf.included, false) = false
      AND COALESCE(pf.addon_amount, f.price_monthly) > 0
"""

_ITEM_COLUMNS = """
    id, tenant_id, subscription_id, feature_key, price_id, "interval", quantity,
    unit_amount, currency, status, started_on,
    current_period_start, current_period_end, charged_through, cancel_at
"""

_ITEM_SELECT = """
    SELECT
        gen_random_uuid(),
        tf.tenant_id,
        s.id,
        tf.feature_key,
        mp.id,
        'monthly',
        1,
        priced.amount,
        pl.currency,
        CASE
            WHEN tf.cancel_at IS NOT NULL THEN 'pending_cancel'
            WHEN tf.status = 'trial' THEN 'trialing'
            ELSE 'active'
        END::subscription_item_status,
        COALESCE(tf.activated_at, s.started_on),
        cyc.period_start,
        cyc.period_end,
        tf.charged_through,
        tf.cancel_at
    FROM tenant_features tf
    JOIN features f
      ON f.key = tf.feature_key
    JOIN subscriptions s
      ON s.tenant_id = tf.tenant_id
     AND s.status NOT IN ('cancelled', 'expired')
    LEFT JOIN plan_features pf
      ON pf.plan_id = s.plan_id
     AND pf.feature_key = tf.feature_key
    CROSS JOIN LATERAL (
        SELECT COALESCE(pf.addon_amount, f.price_monthly) AS amount
    ) AS priced
    -- The plan's currency, because that is the one the invoice bills in
    -- (`generation_service` takes `currency=plan.currency`). The billing
    -- account has a currency too, and taking it here would let an item and the
    -- invoice that charges for it disagree.
    JOIN plans pl
      ON pl.id = s.plan_id
    -- Exactly one cycle, not every open one. `billing_cycles` is unique on
    -- (subscription, period_start) rather than on status, so a subscription
    -- can hold more than one `open` cycle and a plain join here fans the
    -- module out into one item per cycle. The newest is the one being lived in.
    LEFT JOIN LATERAL (
        SELECT bc.period_start, bc.period_end
        FROM billing_cycles bc
        WHERE bc.subscription_id = s.id
          AND bc.status = 'open'
        ORDER BY bc.period_start DESC
        LIMIT 1
    ) AS cyc ON true
    LEFT JOIN module_prices mp
      ON mp.feature_key = tf.feature_key
     AND mp."interval" = 'monthly'
     AND mp.currency = pl.currency
     AND mp.active
     AND mp.amount = priced.amount
    WHERE tf.status IN ('active', 'trial')
      AND NOT f.is_core
      AND COALESCE(pf.included, false) = false
      AND priced.amount > 0
"""


def downgrade() -> None:
    for table in OWNED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")

    op.drop_index(
        "ix_subscription_invoice_items_item", table_name="subscription_invoice_items"
    )
    op.drop_column("subscription_invoice_items", "subscription_item_id")

    for table in ("cart_items", "carts", "subscription_items", "module_listings", "module_prices"):
        op.drop_table(table)

    for enum in NEW_ENUMS:
        enum.drop(op.get_bind(), checkfirst=True)
