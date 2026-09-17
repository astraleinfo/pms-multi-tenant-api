"""Platform billing: what a clinic pays *us*.

This is a different ledger from `app/models/ops.py`, and the separation is
deliberate rather than tidy. `ops.Invoice` is a clinic billing a patient for a
consultation; everything here is the platform billing a clinic for the product.
They share no table, no enum and no service, which is why the table names below
carry a `subscription_` prefix — `invoices` and `payments` were already taken,
by a ledger that means something else.

Four kinds of row live here:

*   **catalogue** — `plans`, `plan_features`, `tax_rates`. Platform-owned, the
    same for every clinic, no `tenant_id`, no row-level policy. They are the
    only place a price or a tax rate is written down: nothing in the services
    carries a number.
*   **the agreement** — `billing_accounts`, `subscriptions`, `billing_cycles`.
    What this clinic bought, who to bill, and the calendar it is billed on.
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
    Currency,
    InvoiceItemKind,
    PaymentGateway,
    PlanInterval,
    RefundStatus,
    SubscriptionInvoiceStatus,
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
