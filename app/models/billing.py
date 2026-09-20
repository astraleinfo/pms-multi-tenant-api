"""Platform billing: what a clinic pays *us*.

This is a different ledger from `app/models/ops.py`, and the separation is
deliberate rather than tidy. `ops.Invoice` is a clinic billing a patient for a
consultation; everything here is the platform billing a clinic for the product.
They share no table, no enum and no service, which is why the table names below
carry a `subscription_` prefix — `invoices` and `payments` were already taken,
by a ledger that means something else.

Five kinds of row live here:

*   **catalogue** — `plans`, `plan_features`, `tax_rates`, `module_prices`,
    `module_listings`. Platform-owned, the same for every clinic, no
    `tenant_id`, no row-level policy. They are the only place a price or a tax
    rate is written down: nothing in the services carries a number.
*   **the agreement** — `billing_accounts`, `subscriptions`, `billing_cycles`,
    `subscription_items`. What this clinic bought, who to bill, the calendar it
    is billed on, and — one row per module — what each part of it costs.
*   **intent** — `carts`, `cart_items`. Modules a clinic means to buy together
    on one payment. A cart holds references and no amounts: it is priced on
    read and re-priced at checkout, so a price that moves between the two is
    caught rather than honoured.
*   **the money** — `subscription_invoices`, `subscription_invoice_items`,
    `subscription_payments`, `subscription_refunds`. An issued invoice is
    immutable: its totals are frozen by a database trigger and its line items
    cannot be updated or deleted at all.
*   **what the gateway said** — `webhook_events`. Platform-level, because a
    delivery arrives before anyone knows which clinic it is about.

Add-on entitlement is *not* here. `tenant_features` already answers "is this
module switched on", and a second table answering the same question would be a
second answer. It gains billing columns instead (`app/models/tenancy.py`).
"""

from __future__ import annotations

import uuid
from datetime import date as date_t
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantScoped, TimestampMixin, uuid_pk
from app.models.enums import (
    BillingCycleStatus,
    CartStatus,
    Currency,
    InvoiceItemKind,
    ModuleListingStatus,
    PaymentGateway,
    PlanInterval,
    RefundStatus,
    SubscriptionInvoiceStatus,
    SubscriptionItemStatus,
    SubscriptionPaymentStatus,
    SubscriptionStatus,
    TaxScope,
    WebhookStatus,
)

#: Money. Twelve digits with two after the point is enough for any monthly
#: subscription in any currency this product sells in, and — unlike a float —
#: it adds up to the same number every time.
MONEY = Numeric(12, 2)
#: A percentage. Two decimals, so 18.00 and 2.50 are both exact.
RATE = Numeric(6, 3)


def _enum(cls, name: str):
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


# --- catalogue ---------------------------------------------------------------


class Plan(Base, TimestampMixin):
    """A base plan. Every tenant has exactly one, and it is mandatory.

    Platform-owned, like `features` and `clinical_types`: there is one
    catalogue, and a clinic chooses from it rather than inventing a price.
    """

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_plan_amount"),
        CheckConstraint("trial_days >= 0", name="ck_plan_trial_days"),
        CheckConstraint("code ~ '^[a-z0-9][a-z0-9_-]{1,39}$'", name="ck_plan_code_shape"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    interval: Mapped[PlanInterval] = mapped_column(
        _enum(PlanInterval, "plan_interval"), nullable=False, default=PlanInterval.MONTHLY
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    currency: Mapped[Currency] = mapped_column(
        _enum(Currency, "currency"), nullable=False, default=Currency.INR
    )
    trial_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: An inactive plan is kept for the subscriptions that already quote it and
    #: is not offered to the next one. The same rule as `service_prices.active`.
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    #: The plan's id at the gateway, where the gateway keeps its own catalogue.
    #: Stored so a subscription can be created against it without a lookup, and
    #: nullable because a plan exists here before it is pushed anywhere.
    razorpay_plan_id: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)

    @property
    def gateway_plan_id(self) -> str | None:
        """The plan's id at whichever gateway this deployment bills through.

        The column is named for Razorpay because the requirement is to store
        Razorpay's ids locally and under their own names. This accessor is what
        the billing services use, so nothing above `gateway.py` has to name a
        gateway to read one — adding Cashfree means a column beside that one
        and a branch here, and no change in any service.
        """
        return self.razorpay_plan_id

    features: Mapped[list[PlanFeature]] = relationship(
        "PlanFeature", back_populates="plan", cascade="all, delete-orphan", lazy="selectin"
    )


class PlanFeature(Base, TimestampMixin):
    """What a plan includes, and what each extra costs on top of it.

    A row with `included` is part of the base price and is never billed as an
    add-on. A row without it is purchasable: `addon_amount` is what this plan
    charges for it, and a null falls back to `features.price_monthly` — so the
    common case needs no row at all, and a plan that discounts a module needs
    one row rather than a second catalogue.
    """

    __tablename__ = "plan_features"
    __table_args__ = (
        UniqueConstraint("plan_id", "feature_key", name="uq_plan_feature"),
        CheckConstraint(
            "addon_amount is null or addon_amount >= 0", name="ck_plan_feature_amount"
        ),
        CheckConstraint(
            "not (included and addon_amount is not null)", name="ck_plan_feature_included_free"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    feature_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("features.key", ondelete="CASCADE"), nullable=False
    )
    included: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    addon_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)

    plan: Mapped[Plan] = relationship("Plan", back_populates="features")


class TaxRate(Base, TimestampMixin):
    """A tax component, as configuration.

    Requirement 11 in one table: nothing in the services knows what GST is.
    A deployment that charges 18% IGST writes one row; one that splits it into
    CGST 9% + SGST 9% writes two, both `intra_state`, and the invoice grows two
    tax lines instead of one. A deployment that charges nothing writes none.

    `effective_from`/`effective_to` mean a rate change is a new row rather than
    an edit, so an invoice raised last year can still be explained.
    """

    __tablename__ = "tax_rates"
    __table_args__ = (
        UniqueConstraint("code", "effective_from", name="uq_tax_rate_code_from"),
        CheckConstraint("percent >= 0 and percent <= 100", name="ck_tax_rate_percent"),
        CheckConstraint(
            "effective_to is null or effective_to > effective_from", name="ck_tax_rate_window"
        ),
        Index("ix_tax_rates_lookup", "active", "country", "scope", "effective_from"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    percent: Mapped[Decimal] = mapped_column(RATE, nullable=False, default=0)
    scope: Mapped[TaxScope] = mapped_column(
        _enum(TaxScope, "tax_scope"), nullable=False, default=TaxScope.ANY
    )
    #: ISO-3166 alpha-2, or empty for "wherever this deployment bills".
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="")
    effective_from: Mapped[date_t] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date_t | None] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ModulePrice(Base, TimestampMixin):
    """What one module costs, per interval and per currency.

    This supersedes `features.price_monthly`, which priced a module in exactly
    one way and therefore could not express a year of it. A yearly row is a
    row, not `12 * monthly`: an annual commitment is normally sold at a
    discount, and deriving the figure would make that discount inexpressible.

    Superseded prices stay. A `subscription_items` row that pinned one still
    points at it, so the price a clinic is grandfathered on remains legible
    years later; `active` is what says which row is on sale today, and a
    partial unique index allows exactly one per (module, interval, currency).
    """

    __tablename__ = "module_prices"
    __table_args__ = (
        UniqueConstraint(
            "feature_key", "interval", "currency", "effective_from", name="uq_module_price_effective"
        ),
        UniqueConstraint("razorpay_plan_id", name="uq_module_price_rzp_plan"),
        Index("ix_module_prices_feature_key", "feature_key"),
        CheckConstraint("amount >= 0", name="ck_module_price_amount"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    feature_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("features.key", ondelete="CASCADE"), nullable=False
    )
    interval: Mapped[PlanInterval] = mapped_column(
        _enum(PlanInterval, "plan_interval"), nullable=False, default=PlanInterval.MONTHLY
    )
    currency: Mapped[Currency] = mapped_column(
        _enum(Currency, "currency"), nullable=False, default=Currency.INR
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    effective_from: Mapped[date_t] = mapped_column(Date, nullable=False)
    #: The gateway's own plan object, when one has been created for recurring
    #: collection. Null for a module billed on one-off invoices.
    razorpay_plan_id: Mapped[str | None] = mapped_column(String(80), nullable=True)


class ModuleListing(Base, TimestampMixin):
    """The module's page: what it does, a demo, and what it looks like.

    Kept apart from `features` on purpose. That row is the FK target the
    feature guard reads on request paths, and loading a blob of marketing copy
    and a screenshot manifest to answer "may this clinic open the lab" would
    put the catalogue on a hot path for no reason.

    Nothing here is priced and nothing here grants anything. A listing can be
    `draft` while the module bills perfectly well for the clinics already on
    it — publishing is a decision about the shop window, not about billing.
    """

    __tablename__ = "module_listings"
    __table_args__ = (
        Index("ix_module_listings_published", "status", "sort_order"),
        CheckConstraint("jsonb_typeof(highlights) = 'array'", name="ck_module_listing_highlights"),
        CheckConstraint("jsonb_typeof(screenshots) = 'array'", name="ck_module_listing_screenshots"),
        CheckConstraint("jsonb_typeof(doc_links) = 'array'", name="ck_module_listing_docs"),
        CheckConstraint(
            "video_duration_s is null or video_duration_s > 0", name="ck_module_listing_duration"
        ),
    )

    feature_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("features.key", ondelete="CASCADE"), primary_key=True
    )
    headline: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    long_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Ordered bullets — the feature list the module's page leads with.
    highlights: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    video_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    video_poster_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    video_duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Ordered `{url, alt, caption}` objects. `alt` is required by the schema
    #: above this: a screenshot nobody can read is not documentation.
    screenshots: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    doc_links: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    category: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    status: Mapped[ModuleListingStatus] = mapped_column(
        _enum(ModuleListingStatus, "module_listing_status"),
        nullable=False,
        default=ModuleListingStatus.DRAFT,
    )
    #: "New", "Beta" — a word on the card, or empty for none.
    badge: Mapped[str] = mapped_column(String(24), nullable=False, default="")


# --- the agreement -----------------------------------------------------------


class BillingAccount(Base, TenantScoped, TimestampMixin):
    """Who to bill, where, and under which tax identity.

    One row per clinic. It is also where the gateway's customer id lives —
    on the account rather than on the subscription, because a clinic that
    cancels and re-subscribes is the same customer to the gateway.
    """

    __tablename__ = "billing_accounts"
    __extra_table_args__ = (
        UniqueConstraint("tenant_id", name="uq_billing_account_tenant"),
        UniqueConstraint("razorpay_customer_id", name="uq_billing_account_rzp_customer"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    legal_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    #: Where invoices are emailed. Blank means "the clinic's administrators",
    #: which is resolved from the user table at send time.
    billing_email: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    address: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: GSTIN, VAT number, whatever this jurisdiction calls it. Printed on the
    #: invoice; never parsed.
    tax_id: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="")
    #: The state or region the supply is made in. Decides `intra_state` versus
    #: `inter_state` when the tax table draws that distinction.
    place_of_supply: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    currency: Mapped[Currency] = mapped_column(
        _enum(Currency, "currency"), nullable=False, default=Currency.INR
    )
    razorpay_customer_id: Mapped[str | None] = mapped_column(String(80), nullable=True)

    @property
    def gateway_customer_id(self) -> str | None:
        """See `Plan.gateway_plan_id` — the same arrangement, for the customer."""
        return self.razorpay_customer_id

    @gateway_customer_id.setter
    def gateway_customer_id(self, value: str | None) -> None:
        self.razorpay_customer_id = value


class Subscription(Base, TenantScoped, TimestampMixin):
    """One clinic's agreement to pay for the product.

    A tenant has at most one live subscription — the partial unique index says
    so, so two browser tabs cannot subscribe the same clinic twice. Cancelled
    and expired rows stay, because the invoices they raised point at them.

    The cycle *dates* are not stored here: they are `billing_cycles` rows, one
    per period, so a plan change mid-month leaves a record of both halves
    rather than overwriting the one that was lived through.
    """

    __tablename__ = "subscriptions"
    __extra_table_args__ = (
        Index(
            "uq_subscriptions_one_live_per_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("status not in ('cancelled', 'expired')"),
        ),
        Index("ix_subscriptions_status", "status"),
        UniqueConstraint("razorpay_subscription_id", name="uq_subscription_rzp_id"),
        CheckConstraint(
            "cancelled_at is null or cancelled_at >= started_on", name="ck_subscription_cancel_order"
        ),
        CheckConstraint("anchor_day between 1 and 31", name="ck_subscription_anchor"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        _enum(SubscriptionStatus, "subscription_status"),
        nullable=False,
        default=SubscriptionStatus.PENDING,
    )

    started_on: Mapped[date_t] = mapped_column(Date, nullable=False)
    #: The day of the month every cycle starts on. Held separately from
    #: `started_on` so a subscription that began on the 31st keeps billing on
    #: the 31st after a February that clamped it to the 28th.
    anchor_day: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    trial_ends_on: Mapped[date_t | None] = mapped_column(Date, nullable=True)

    #: Set when the clinic asks to cancel at the end of the period it has paid
    #: for. The subscription stays `active` until the renewal job sees this.
    cancel_at_cycle_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancelled_at: Mapped[date_t | None] = mapped_column(Date, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Which gateway collects for this subscription, and its id there. Stored
    #: locally so the gateway is never the source of truth for what we think
    #: the clinic owes — only for what it managed to collect.
    gateway: Mapped[PaymentGateway] = mapped_column(
        _enum(PaymentGateway, "payment_gateway"), nullable=False, default=PaymentGateway.RAZORPAY
    )
    razorpay_subscription_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: What the gateway last told us its own status was. Kept beside `status`
    #: rather than merged into it: ours is the one the product obeys, theirs is
    #: evidence, and a disagreement is worth being able to see.
    gateway_status: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    gateway_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    plan: Mapped[Plan] = relationship("Plan", lazy="selectin")

    @property
    def gateway_subscription_id(self) -> str | None:
        """See `Plan.gateway_plan_id` — the same arrangement, for the
        subscription itself. This is what `subscription_service` reads, which
        is why that file names no gateway."""
        return self.razorpay_subscription_id

    @gateway_subscription_id.setter
    def gateway_subscription_id(self, value: str | None) -> None:
        self.razorpay_subscription_id = value


class BillingCycle(Base, TenantScoped, TimestampMixin):
    """One billed period, `[period_start, period_end)`.

    Half-open on purpose: `period_end` is the first day of the *next* cycle, so
    `(period_end - period_start).days` is the real number of days in this
    period — 28, 29, 30 or 31 — and the proration calculator never has to
    assume a month length.

    The unique index on `(subscription_id, period_start)` is what makes the
    renewal job idempotent: running it twice cannot open the same cycle twice.
    """

    __tablename__ = "billing_cycles"
    __extra_table_args__ = (
        UniqueConstraint("subscription_id", "period_start", name="uq_billing_cycle_period"),
        UniqueConstraint("subscription_id", "sequence", name="uq_billing_cycle_sequence"),
        Index("ix_billing_cycles_open", "status", "period_end"),
        CheckConstraint("period_end > period_start", name="ck_billing_cycle_window"),
        CheckConstraint("sequence >= 1", name="ck_billing_cycle_sequence"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    period_start: Mapped[date_t] = mapped_column(Date, nullable=False)
    #: Exclusive. The first day of the next cycle.
    period_end: Mapped[date_t] = mapped_column(Date, nullable=False)
    status: Mapped[BillingCycleStatus] = mapped_column(
        _enum(BillingCycleStatus, "billing_cycle_status"),
        nullable=False,
        default=BillingCycleStatus.OPEN,
    )
    #: The plan in force for this period, frozen. A plan change opens a new
    #: cycle rather than rewriting this one.
    plan_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False
    )
    plan_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def days(self) -> int:
        """The actual length of this period. Never 30 by assumption."""
        return (self.period_end - self.period_start).days


class SubscriptionItem(Base, TenantScoped, TimestampMixin):
    """One module on one clinic's subscription, at its own price.

    The reason this table exists: until it did, "what does this clinic pay for
    the lab" was answered by walking `plan_features` to `features.price_monthly`
    on every read, so the figure moved whenever the catalogue moved and no two
    modules could bill on different intervals. A row here pins the number at
    the moment of purchase.

    Two fields are easy to mix up, so they are worth stating plainly:

    *   **`unit_amount` is the authority.** It is what this item bills, pinned
        when it was bought, and it is the only figure any invoice derives from.
    *   **`price_id` is provenance.** It records which catalogue row the figure
        came from, and is null when a `plan_features` override or the 0013
        backfill set it instead. Claiming a catalogue price that was never
        applied would be a lie that only surfaces in an audit.

    `charged_through` is exclusive and carries the same guarantee its
    counterpart on `tenant_features` does: the invoice generator will not bill
    a period ending on or before it, so running the renewal job twice cannot
    charge twice.

    Entitlement is still `tenant_features`. This is not a second answer to "is
    the module switched on" — it answers what is being paid for, and the two
    are written in one transaction.
    """

    __tablename__ = "subscription_items"
    __extra_table_args__ = (
        Index("ix_subscription_items_subscription", "subscription_id"),
        Index("ix_subscription_items_status", "tenant_id", "status"),
        Index("ix_subscription_items_cancel_at", "cancel_at"),
        CheckConstraint("quantity > 0", name="ck_subscription_item_quantity"),
        CheckConstraint("unit_amount >= 0", name="ck_subscription_item_amount"),
        CheckConstraint(
            "cancel_at is null or cancel_at >= started_on", name="ck_subscription_item_cancel_order"
        ),
        CheckConstraint(
            "current_period_end is null or current_period_start is null "
            "or current_period_end > current_period_start",
            name="ck_subscription_item_period",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    feature_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("features.key", ondelete="RESTRICT"), nullable=False
    )
    price_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("module_prices.id", ondelete="RESTRICT"), nullable=True
    )

    interval: Mapped[PlanInterval] = mapped_column(
        _enum(PlanInterval, "plan_interval"), nullable=False, default=PlanInterval.MONTHLY
    )
    #: One, today. Seats and usage add-ons become a number here rather than a
    #: migration, which is the whole reason it exists before anything sets it.
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    currency: Mapped[Currency] = mapped_column(
        _enum(Currency, "currency"), nullable=False, default=Currency.INR
    )
    status: Mapped[SubscriptionItemStatus] = mapped_column(
        _enum(SubscriptionItemStatus, "subscription_item_status"),
        nullable=False,
        default=SubscriptionItemStatus.ACTIVE,
    )

    started_on: Mapped[date_t] = mapped_column(Date, nullable=False)
    current_period_start: Mapped[date_t | None] = mapped_column(Date, nullable=True)
    #: Exclusive, like `billing_cycles.period_end`.
    current_period_end: Mapped[date_t | None] = mapped_column(Date, nullable=True)
    #: Exclusive. Billing is paid up to, but not including, this day.
    charged_through: Mapped[date_t | None] = mapped_column(Date, nullable=True)
    #: The cycle end a scheduled cancellation takes effect on. Access continues
    #: until then; nothing is refunded and nothing is charged after.
    cancel_at: Mapped[date_t | None] = mapped_column(Date, nullable=True)
    trial_ends_on: Mapped[date_t | None] = mapped_column(Date, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    price: Mapped[ModulePrice | None] = relationship("ModulePrice", lazy="selectin")

    @property
    def cancellation_scheduled(self) -> bool:
        return self.status == SubscriptionItemStatus.PENDING_CANCEL

    @property
    def entitled(self) -> bool:
        """Whether this item grants access right now.

        A `pending_cancel` item does: it is paid for to the end of its cycle
        and the module stays usable until then.
        """
        return self.status != SubscriptionItemStatus.CANCELLED


# --- the money ---------------------------------------------------------------


class SubscriptionInvoice(Base, TenantScoped, TimestampMixin):
    """What a clinic owes for one billing cycle. Immutable once issued.

    "Immutable" is enforced below the application: a trigger refuses any change
    to the number, the currency or any of the money columns on a row that has
    been issued, and the line items table refuses UPDATE and DELETE outright.
    What may still change is the *status* and the gateway references — an
    invoice being paid is not an invoice being rewritten.

    The partial unique index on `billing_cycle_id` is the duplicate-invoice
    guard: a cycle can have many void invoices in its history but only ever one
    that counts.
    """

    __tablename__ = "subscription_invoices"
    __extra_table_args__ = (
        UniqueConstraint("tenant_id", "number", name="uq_subscription_invoice_number"),
        Index(
            "uq_subscription_invoice_per_cycle",
            "billing_cycle_id",
            unique=True,
            postgresql_where=text("status <> 'void'"),
        ),
        Index("ix_subscription_invoices_tenant_status", "tenant_id", "status"),
        Index("ix_subscription_invoices_issued", "issued_on"),
        UniqueConstraint("razorpay_invoice_id", name="uq_subscription_invoice_rzp_id"),
        CheckConstraint("subtotal >= 0", name="ck_sub_invoice_subtotal"),
        CheckConstraint("discount_total >= 0", name="ck_sub_invoice_discount"),
        CheckConstraint("tax_total >= 0", name="ck_sub_invoice_tax"),
        CheckConstraint("total >= 0", name="ck_sub_invoice_total"),
        CheckConstraint("due_on >= issued_on", name="ck_sub_invoice_due"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: Null for a one-off invoice (an add-on bought mid-cycle and charged
    #: immediately). Every periodic invoice names the cycle it settles.
    billing_cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("billing_cycles.id", ondelete="RESTRICT"), nullable=True
    )

    number: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[SubscriptionInvoiceStatus] = mapped_column(
        _enum(SubscriptionInvoiceStatus, "subscription_invoice_status"),
        nullable=False,
        default=SubscriptionInvoiceStatus.DRAFT,
    )
    currency: Mapped[Currency] = mapped_column(
        _enum(Currency, "currency"), nullable=False, default=Currency.INR
    )

    issued_on: Mapped[date_t] = mapped_column(Date, nullable=False)
    due_on: Mapped[date_t] = mapped_column(Date, nullable=False)
    period_start: Mapped[date_t] = mapped_column(Date, nullable=False)
    period_end: Mapped[date_t] = mapped_column(Date, nullable=False)

    #: The sum of requirement 10, stored rather than recomputed. An invoice is
    #: a statement of what was charged on a day, and a later price change must
    #: not be able to alter it retrospectively.
    subtotal: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    discount_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    tax_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)

    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    razorpay_invoice_id: Mapped[str | None] = mapped_column(String(80), nullable=True)

    #: The hosted page the "Pay Now" button opens. Minted by the gateway; never
    #: constructed here, and never containing a secret.
    payment_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: When the invoice email last went out. The send job reads it to stay
    #: idempotent — a reminder is a deliberate act, not a retry.
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list[SubscriptionInvoiceItem]] = relationship(
        "SubscriptionInvoiceItem",
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="SubscriptionInvoiceItem.position",
        lazy="selectin",
    )
    payments: Mapped[list[SubscriptionPayment]] = relationship(
        "SubscriptionPayment",
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="SubscriptionPayment.created_at",
        lazy="selectin",
    )


class SubscriptionInvoiceItem(Base, TenantScoped, TimestampMixin):
    """One line. Written once, never touched again — the table has rules
    against UPDATE and DELETE, so "detailed line items" stays true."""

    __tablename__ = "subscription_invoice_items"
    __extra_table_args__ = (
        Index("ix_sub_invoice_items_invoice", "invoice_id", "position"),
        Index("ix_sub_invoice_items_feature", "tenant_id", "feature_key"),
        Index("ix_subscription_invoice_items_item", "subscription_item_id"),
        CheckConstraint("quantity > 0", name="ck_sub_item_quantity"),
        CheckConstraint(
            "(period_end is null and period_start is null) or period_end > period_start",
            name="ck_sub_item_window",
        ),
        CheckConstraint(
            "prorated_days is null or (prorated_days >= 0 and cycle_days > 0)",
            name="ck_sub_item_proration",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("subscription_invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[InvoiceItemKind] = mapped_column(
        _enum(InvoiceItemKind, "invoice_item_kind"), nullable=False
    )
    description: Mapped[str] = mapped_column(String(240), nullable=False)

    #: Which module this line is for, when it is for one. Null on the base
    #: plan, on discounts and on tax.
    feature_key: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("features.key", ondelete="SET NULL"), nullable=True
    )
    #: Which subscription item this line billed, when it billed one. Null on
    #: the base plan, on tax, and on every line written before 0013. Set at
    #: INSERT and never re-pointed — the table's no-UPDATE rule sees to that.
    subscription_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("subscription_items.id", ondelete="SET NULL"), nullable=True
    )

    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: The full monthly price this line derives from, before any proration. It
    #: is kept so an invoice can explain itself: "₹2,499/month, 11 of 31 days".
    unit_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    #: What this line actually adds to the subtotal. Negative for a discount or
    #: a credit, which is why there is no CHECK forcing it positive.
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)

    #: The window this line covers, for a prorated or partial charge.
    period_start: Mapped[date_t | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date_t | None] = mapped_column(Date, nullable=True)
    #: The proration workings, kept rather than recomputed: how many days were
    #: charged, out of how many the cycle actually had.
    prorated_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cycle_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Which tax component produced a `tax` line, and at what rate. Recorded on
    #: the line so a rate change later cannot silently restate an old invoice.
    tax_code: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    tax_percent: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)

    invoice: Mapped[SubscriptionInvoice] = relationship("SubscriptionInvoice", back_populates="items")


class SubscriptionPayment(Base, TenantScoped, TimestampMixin):
    """Money the gateway says it took.

    A row is created when a payment is attempted and updated only by things the
    *server* learned: a webhook, or a server-side fetch. Nothing a browser
    posts can set `status` to `captured` — the verification path re-fetches the
    payment from the gateway and compares the amount before it believes it.

    `(gateway, gateway_payment_id)` is unique across the whole table, not per
    tenant. A unique index is not filtered by row-level security, so it is also
    what stops one gateway payment being recorded against two clinics.
    """

    __tablename__ = "subscription_payments"
    __extra_table_args__ = (
        UniqueConstraint("gateway", "gateway_payment_id", name="uq_subscription_payment_gateway_id"),
        Index("ix_subscription_payments_invoice", "invoice_id"),
        Index("ix_subscription_payments_status", "tenant_id", "status"),
        CheckConstraint("amount > 0", name="ck_subscription_payment_amount"),
        CheckConstraint("amount_refunded >= 0", name="ck_subscription_payment_refunded"),
        CheckConstraint("amount_refunded <= amount", name="ck_subscription_payment_refund_cap"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("subscription_invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    gateway: Mapped[PaymentGateway] = mapped_column(
        _enum(PaymentGateway, "payment_gateway"), nullable=False, default=PaymentGateway.RAZORPAY
    )
    #: Null only for the brief moment between creating an order and the gateway
    #: naming the payment. `manual` payments carry a reference instead.
    gateway_payment_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    gateway_order_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)

    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    amount_refunded: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    currency: Mapped[Currency] = mapped_column(
        _enum(Currency, "currency"), nullable=False, default=Currency.INR
    )
    status: Mapped[SubscriptionPaymentStatus] = mapped_column(
        _enum(SubscriptionPaymentStatus, "subscription_payment_status"),
        nullable=False,
        default=SubscriptionPaymentStatus.CREATED,
    )
    #: What the gateway called it: `card`, `upi`, `netbanking`. A free string,
    #: because it is theirs to name and we only ever display it.
    method: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    invoice: Mapped[SubscriptionInvoice] = relationship(
        "SubscriptionInvoice", back_populates="payments"
    )
    refunds: Mapped[list[SubscriptionRefund]] = relationship(
        "SubscriptionRefund", back_populates="payment", cascade="all, delete-orphan", lazy="selectin"
    )


class SubscriptionRefund(Base, TenantScoped, TimestampMixin):
    """Money given back. Always against a payment, never against an invoice —
    an invoice is a statement and is never rewritten."""

    __tablename__ = "subscription_refunds"
    __extra_table_args__ = (
        UniqueConstraint("gateway", "gateway_refund_id", name="uq_subscription_refund_gateway_id"),
        Index("ix_subscription_refunds_payment", "payment_id"),
        CheckConstraint("amount > 0", name="ck_subscription_refund_amount"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    payment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("subscription_payments.id", ondelete="CASCADE"),
        nullable=False,
    )
    gateway: Mapped[PaymentGateway] = mapped_column(
        _enum(PaymentGateway, "payment_gateway"), nullable=False, default=PaymentGateway.RAZORPAY
    )
    gateway_refund_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[Currency] = mapped_column(
        _enum(Currency, "currency"), nullable=False, default=Currency.INR
    )
    status: Mapped[RefundStatus] = mapped_column(
        _enum(RefundStatus, "refund_status"), nullable=False, default=RefundStatus.PENDING
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    payment: Mapped[SubscriptionPayment] = relationship(
        "SubscriptionPayment", back_populates="refunds"
    )


# --- what the gateway said ---------------------------------------------------


class WebhookEvent(Base, TimestampMixin):
    """Every delivery, stored before anything is believed about it.

    Deliberately *not* `TenantScoped`. A delivery arrives naming a gateway id,
    not a clinic, and the tenant is only known after the payload has been
    matched against something local — so there is no tenant to bind at the
    moment the row is written. It is a platform-owned table like `tenants` and
    `plans`, reachable only through the console, and it holds gateway metadata
    rather than anything clinical.

    `(gateway, event_id)` is unique, and that index — not a check in Python —
    is what makes redelivery a no-op. A signature that does not verify is still
    stored, as `rejected`, and is never processed: throwing it away would erase
    the evidence of the one delivery most worth keeping.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("gateway", "event_id", name="uq_webhook_event_id"),
        Index("ix_webhook_events_pending", "status", "created_at"),
        Index("ix_webhook_events_type", "gateway", "event_type"),
        Index("ix_webhook_events_entity", "entity_id"),
        CheckConstraint("attempts >= 0", name="ck_webhook_attempts"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    gateway: Mapped[PaymentGateway] = mapped_column(
        _enum(PaymentGateway, "payment_gateway"), nullable=False, default=PaymentGateway.RAZORPAY
    )
    #: The gateway's own id for the delivery — `x-razorpay-event-id`. When a
    #: gateway does not send one, the handler derives a stable digest of the
    #: body, so redelivery still collides.
    event_id: Mapped[str] = mapped_column(String(120), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    #: The gateway id the payload is *about* — a payment, subscription or
    #: invoice id — so a delivery can be found without reading the JSON.
    entity_id: Mapped[str] = mapped_column(String(80), nullable=False, default="")

    signature_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[WebhookStatus] = mapped_column(
        _enum(WebhookStatus, "webhook_status"), nullable=False, default=WebhookStatus.RECEIVED
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WebhookEvent {self.gateway}:{self.event_type} {self.status}>"


# --- intent ------------------------------------------------------------------


class Cart(Base, TenantScoped, TimestampMixin):
    """Modules a clinic means to buy together, on one payment.

    Server-side rather than in the browser, for the same reason no request body
    in this API carries an amount: a cart the client owns is a total the client
    can choose. What is stored is a list of references; the money is worked out
    on read, and worked out again at checkout.

    One open cart per clinic, enforced by a partial unique index rather than by
    a service looking first — two tabs pressing Add to Cart at once meet the
    index. A converted cart is kept, because it is the only place the intent
    behind a multi-module invoice is written down.
    """

    __tablename__ = "carts"
    __extra_table_args__ = (Index("ix_carts_status", "tenant_id", "status"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    status: Mapped[CartStatus] = mapped_column(
        _enum(CartStatus, "cart_status"), nullable=False, default=CartStatus.OPEN
    )
    currency: Mapped[Currency] = mapped_column(
        _enum(Currency, "currency"), nullable=False, default=Currency.INR
    )
    #: Held as typed, and resolved against the coupon rules at quote and again
    #: at checkout. Storing the discount it worked out to would freeze a figure
    #: the coupon's own rules are entitled to change.
    coupon_code: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: The single invoice this cart became. One invoice for every module in it
    #: is what makes "one payment" true, and what makes activating them all or
    #: none of them a property of the invoice rather than a loop that can stop
    #: halfway.
    converted_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("subscription_invoices.id", ondelete="SET NULL"), nullable=True
    )
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list[CartItem]] = relationship(
        "CartItem", back_populates="cart", cascade="all, delete-orphan", lazy="selectin"
    )


class CartItem(Base, TenantScoped, TimestampMixin):
    """One module in a cart, at one interval.

    There is no money column here, deliberately. `price_id` names the
    catalogue row the line intends to buy at; what it comes to is computed, and
    a price that has moved since the module went in the cart is something the
    quote reports rather than something the cart remembers.

    One line per module: the lab monthly *and* the lab yearly in the same cart
    is a contradiction rather than two lines, and the unique constraint says so.
    """

    __tablename__ = "cart_items"
    __extra_table_args__ = (
        UniqueConstraint("cart_id", "feature_key", name="uq_cart_item_feature"),
        Index("ix_cart_items_cart_id", "cart_id"),
        CheckConstraint("quantity > 0", name="ck_cart_item_quantity"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    cart_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("carts.id", ondelete="CASCADE"), nullable=False
    )
    feature_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("features.key", ondelete="CASCADE"), nullable=False
    )
    price_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("module_prices.id", ondelete="RESTRICT"), nullable=False
    )
    interval: Mapped[PlanInterval] = mapped_column(
        _enum(PlanInterval, "plan_interval"), nullable=False, default=PlanInterval.MONTHLY
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    cart: Mapped[Cart] = relationship("Cart", back_populates="items")
    price: Mapped[ModulePrice] = relationship("ModulePrice", lazy="selectin")
