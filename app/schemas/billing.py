"""Wire shapes for platform billing.

Two audiences, and the difference matters for what may appear in each:

*   **Settings → Billing**, inside a clinic. Sees its own plan, add-ons,
    prices, next billing date, scheduled cancellations, invoices and their
    status. Never sees a gateway credential beyond the public key id, never
    sees another clinic, and never sends an amount — every figure here is
    computed server-side and is output-only.
*   **the super-admin console**. Sees the same money across every clinic, plus
    the metrics.

`CheckoutOut` is the one response that carries anything gateway-shaped, and it
carries exactly two public values: the gateway's key id and an order id. The
key secret and the webhook secret have no field anywhere in this file.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from app.schemas.common import ApiModel, IdStr, InputModel, iso_date, money, stamp

# --- catalogue ---------------------------------------------------------------


class PlanOut(ApiModel):
    id: str
    code: str
    name: str
    description: str
    interval: str
    amount: float
    currency: str
    trial_days: int
    active: bool

    @classmethod
    def of(cls, plan) -> PlanOut:
        return cls(
            id=str(plan.id),
            code=plan.code,
            name=plan.name,
            description=plan.description,
            interval=str(plan.interval),
            amount=money(plan.amount),
            currency=str(plan.currency),
            trial_days=plan.trial_days,
            active=plan.active,
        )


class AddonOut(ApiModel):
    """One module, as the billing screen shows it."""

    feature_key: str
    name: str
    active: bool
    #: Covered by the base plan — switched on, and never invoiced separately.
    included: bool
    #: `None` when included: there is no price to show, and a zero would read
    #: as "free extra" rather than "part of the plan".
    monthly_amount: float | None
    activated_on: str | None
    #: The day a scheduled cancellation takes effect. The feature keeps working
    #: until then, and nothing is refunded.
    cancels_on: str | None
    #: Billing is paid up to, but not including, this day.
    charged_through: str | None

    @classmethod
    def of(cls, view) -> AddonOut:
        return cls(
            feature_key=view.feature_key,
            name=view.name,
            active=view.active,
            included=view.included,
            monthly_amount=None if view.amount is None else money(view.amount),
            activated_on=iso_date(view.activated_on),
            cancels_on=iso_date(view.cancels_on),
            charged_through=iso_date(view.charged_through),
        )


# --- subscription ------------------------------------------------------------


class SubscriptionOut(ApiModel):
    id: str
    status: str
    plan: PlanOut
    started_on: str
    trial_ends_on: str | None
    #: True while a cancellation is scheduled for the end of the paid period.
    cancel_at_cycle_end: bool
    cancelled_on: str | None
    current_period_start: str | None
    current_period_end: str | None
    #: How many days the current cycle actually has. 28, 29, 30 or 31 — the
    #: denominator every proration on this screen was computed over.
    current_period_days: int | None

    @classmethod
    def of(cls, subscription, plan, cycle) -> SubscriptionOut:
        return cls(
            id=str(subscription.id),
            status=str(subscription.status),
            plan=PlanOut.of(plan),
            started_on=iso_date(subscription.started_on) or "",
            trial_ends_on=iso_date(subscription.trial_ends_on),
            cancel_at_cycle_end=subscription.cancel_at_cycle_end,
            cancelled_on=iso_date(subscription.cancelled_at),
            current_period_start=iso_date(cycle.period_start) if cycle else None,
            current_period_end=iso_date(cycle.period_end) if cycle else None,
            current_period_days=cycle.days if cycle else None,
        )


class SubscribeIn(InputModel):
    """Choosing a base plan. The plan is named; the price is not.

    There is no amount on this model and there is not going to be one — what a
    plan costs is a row in `plans`, and a request body that could say otherwise
    would be a request body that could set its own price.
    """

    plan_code: str = Field(min_length=1, max_length=40)


class PlanChangeIn(InputModel):
    plan_code: str = Field(min_length=1, max_length=40)


class CancelIn(InputModel):
    """Ending the agreement. Cancellation takes effect at the end of the period
    already paid for; there is no field here to make it sooner, because that is
    the platform's decision rather than a clinic's."""

    reason: str = Field(default="", max_length=500)


class BillingAccountOut(ApiModel):
    legal_name: str
    billing_email: str
    phone: str
    address: str
    tax_id: str
    country: str
    place_of_supply: str
    currency: str

    @classmethod
    def of(cls, account) -> BillingAccountOut:
        return cls(
            legal_name=account.legal_name,
            billing_email=account.billing_email,
            phone=account.phone,
            address=account.address,
            tax_id=account.tax_id,
            country=account.country,
            place_of_supply=account.place_of_supply,
            currency=str(account.currency),
        )


class BillingAccountPatch(InputModel):
    legal_name: str | None = Field(default=None, max_length=200)
    billing_email: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=2000)
    tax_id: str | None = Field(default=None, max_length=40)
    country: str | None = Field(default=None, max_length=2)
    place_of_supply: str | None = Field(default=None, max_length=60)


# --- invoices ----------------------------------------------------------------


class InvoiceItemOut(ApiModel):
    kind: str
    description: str
    #: Which module this line is for, or null on the base plan, a discount or
    #: tax. It matters more since a cart checkout: an invoice can now carry
    #: five add-on lines, and without this a client can only tell them apart by
    #: parsing the description.
    feature_key: str | None
    quantity: int
    unit_amount: float
    amount: float
    period_start: str | None
    period_end: str | None
    #: The proration workings: how many days were charged, of how many the cycle
    #: had. Both null on a line that was not prorated.
    prorated_days: int | None
    cycle_days: int | None
    tax_code: str
    tax_percent: float | None

    @classmethod
    def of(cls, item) -> InvoiceItemOut:
        return cls(
            kind=str(item.kind),
            feature_key=item.feature_key,
            description=item.description,
            quantity=item.quantity,
            unit_amount=money(item.unit_amount),
            amount=money(item.amount),
            period_start=iso_date(item.period_start),
            period_end=iso_date(item.period_end),
            prorated_days=item.prorated_days,
            cycle_days=item.cycle_days,
            tax_code=item.tax_code,
            tax_percent=None if item.tax_percent is None else float(item.tax_percent),
        )


class InvoiceOut(ApiModel):
    id: str
    number: str
    status: str
    currency: str
    issued_on: str
    due_on: str
    period_start: str
    period_end: str
    subtotal: float
    discount_total: float
    tax_total: float
    total: float
    paid: float
    balance: float
    #: The hosted page "Pay Now" opens, when the gateway has minted one.
    payment_url: str
    emailed_at: str | None
    items: list[InvoiceItemOut]

    @classmethod
    def of(cls, invoice, *, paid: Decimal, balance: Decimal, tz: str | None = None) -> InvoiceOut:
        return cls(
            id=str(invoice.id),
            number=invoice.number,
            status=str(invoice.status),
            currency=str(invoice.currency),
            issued_on=iso_date(invoice.issued_on) or "",
            due_on=iso_date(invoice.due_on) or "",
            period_start=iso_date(invoice.period_start) or "",
            period_end=iso_date(invoice.period_end) or "",
            subtotal=money(invoice.subtotal),
            discount_total=money(invoice.discount_total),
            tax_total=money(invoice.tax_total),
            total=money(invoice.total),
            paid=money(paid),
            balance=money(balance),
            payment_url=invoice.payment_url,
            emailed_at=stamp(invoice.emailed_at, tz),
            items=[InvoiceItemOut.of(i) for i in invoice.items],
        )


class InvoicePageOut(ApiModel):
    items: list[InvoiceOut]
    total: int
    limit: int
    offset: int


class BillingSummaryOut(ApiModel):
    """Everything Settings → Billing renders, in one response."""

    subscription: SubscriptionOut | None
    account: BillingAccountOut
    addons: list[AddonOut]
    #: What the next invoice comes to before tax, at today's configuration.
    recurring_subtotal: float
    next_billing_on: str | None
    outstanding_total: float
    outstanding_count: int
    currency: str


# --- payment -----------------------------------------------------------------


class CheckoutOut(ApiModel):
    """What a browser is handed to open a payment, and nothing more.

    `key_id` is the gateway's public key — the half of the pair Checkout needs
    and the only credential value that ever leaves this process. `amount` is
    the server's figure, echoed so the widget can display it; it is re-derived
    and re-checked server-side before any payment is believed, so a browser
    that sends back a different one proves nothing.
    """

    gateway: str
    key_id: str
    order_id: str
    amount: float
    currency: str
    invoice_number: str
    customer_name: str
    customer_email: str
    description: str

    @classmethod
    def of(cls, context) -> CheckoutOut:
        return cls(
            gateway=context.gateway,
            key_id=context.key_id,
            order_id=context.order_id,
            amount=money(context.amount.amount),
            currency=context.amount.currency,
            invoice_number=context.invoice_number,
            customer_name=context.customer_name,
            customer_email=context.customer_email,
            description=context.description,
        )


class PaymentVerifyIn(InputModel):
    """What Checkout hands back. A claim, not a fact.

    There is no amount and no status on this model, deliberately: the server
    knows what it asked for and re-fetches what happened. The only thing the
    browser contributes is which payment to go and look at, plus a signature
    proving it did not edit that.
    """

    order_id: str = Field(min_length=1, max_length=80)
    payment_id: str = Field(min_length=1, max_length=80)
    signature: str = Field(min_length=1, max_length=256)


class PaymentOut(ApiModel):
    id: str
    status: str
    amount: float
    amount_refunded: float
    currency: str
    method: str
    gateway: str
    gateway_payment_id: str | None
    captured_at: str | None
    failure_reason: str

    @classmethod
    def of(cls, payment, tz: str | None = None) -> PaymentOut:
        return cls(
            id=str(payment.id),
            status=str(payment.status),
            amount=money(payment.amount),
            amount_refunded=money(payment.amount_refunded),
            currency=str(payment.currency),
            method=payment.method,
            gateway=str(payment.gateway),
            gateway_payment_id=payment.gateway_payment_id,
            captured_at=stamp(payment.captured_at, tz),
            failure_reason=payment.failure_reason,
        )


class RefundIn(InputModel):
    """A refund, initiated from the console. Nothing refunds automatically."""

    payment_id: IdStr
    #: Omit for the whole remaining balance of the payment.
    amount: float | None = Field(default=None, gt=0)
    reason: str = Field(default="", max_length=500)


class RefundOut(ApiModel):
    id: str
    payment_id: str
    amount: float
    currency: str
    status: str
    reason: str
    processed_at: str | None

    @classmethod
    def of(cls, refund, tz: str | None = None) -> RefundOut:
        return cls(
            id=str(refund.id),
            payment_id=str(refund.payment_id),
            amount=money(refund.amount),
            currency=str(refund.currency),
            status=str(refund.status),
            reason=refund.reason,
            processed_at=stamp(refund.processed_at, tz),
        )


# --- console -----------------------------------------------------------------


class AddonMetricOut(ApiModel):
    feature_key: str
    name: str
    active_tenants: int
    cancelling: int
    monthly_revenue: float
    billed_in_window: float


class TenantMetricOut(ApiModel):
    tenant_id: str
    name: str
    slug: str
    plan: str
    subscription_status: str
    mrr: float
    revenue: float
    outstanding: float
    active_addons: int


class BillingMetricsOut(ApiModel):
    """The console's billing dashboard.

    Stock figures (`mrr`, `subscriptions_*`, `*_outstanding`) describe the
    present and ignore the date filter; movement figures (`revenue`,
    `subscriptions_new`, `payments_*`, `refunds_*`) describe the window.
    """

    mrr: float
    arr: float
    revenue: float
    refunded: float
    net_revenue: float

    subscriptions_active: int
    subscriptions_trialing: int
    subscriptions_paused: int
    subscriptions_past_due: int
    subscriptions_new: int
    subscriptions_cancelled: int
    cancellations_scheduled: int

    payments_successful: int
    payments_failed: int
    payment_success_rate: float
    refunds_count: int

    invoices_issued: int
    invoices_paid: int
    invoices_outstanding: int
    outstanding_total: float

    addons: list[AddonMetricOut]
    tenants: list[TenantMetricOut]

    @classmethod
    def of(cls, metrics) -> BillingMetricsOut:
        return cls(
            mrr=money(metrics.mrr),
            arr=money(metrics.arr),
            revenue=money(metrics.revenue),
            refunded=money(metrics.refunded),
            net_revenue=money(metrics.net_revenue),
            subscriptions_active=metrics.subscriptions_active,
            subscriptions_trialing=metrics.subscriptions_trialing,
            subscriptions_paused=metrics.subscriptions_paused,
            subscriptions_past_due=metrics.subscriptions_past_due,
            subscriptions_new=metrics.subscriptions_new,
            subscriptions_cancelled=metrics.subscriptions_cancelled,
            cancellations_scheduled=metrics.cancellations_scheduled,
            payments_successful=metrics.payments_successful,
            payments_failed=metrics.payments_failed,
            payment_success_rate=metrics.payment_success_rate,
            refunds_count=metrics.refunds_count,
            invoices_issued=metrics.invoices_issued,
            invoices_paid=metrics.invoices_paid,
            invoices_outstanding=metrics.invoices_outstanding,
            outstanding_total=money(metrics.outstanding_total),
            addons=[
                AddonMetricOut(
                    feature_key=a.feature_key,
                    name=a.name,
                    active_tenants=a.active_tenants,
                    cancelling=a.cancelling,
                    monthly_revenue=money(a.monthly_revenue),
                    billed_in_window=money(a.billed_in_window),
                )
                for a in metrics.addons
            ],
            tenants=[
                TenantMetricOut(
                    tenant_id=str(t.tenant_id),
                    name=t.name,
                    slug=t.slug,
                    plan=t.plan,
                    subscription_status=t.subscription_status,
                    mrr=money(t.mrr),
                    revenue=money(t.revenue),
                    outstanding=money(t.outstanding),
                    active_addons=t.active_addons,
                )
                for t in metrics.tenants
            ],
        )


class WebhookEventOut(ApiModel):
    """A stored delivery, for the console's audit view.

    The payload is not returned. It is gateway metadata rather than anything
    clinical, but it is also unbounded third-party JSON, and a console table
    does not need it to answer "did this arrive and was it processed".
    """

    id: str
    gateway: str
    event_id: str
    event_type: str
    entity_id: str
    signature_verified: bool
    status: str
    attempts: int
    error: str
    received_at: str | None
    processed_at: str | None

    @classmethod
    def of(cls, event, tz: str | None = None) -> WebhookEventOut:
        return cls(
            id=str(event.id),
            gateway=str(event.gateway),
            event_id=event.event_id,
            event_type=event.event_type,
            entity_id=event.entity_id,
            signature_verified=event.signature_verified,
            status=str(event.status),
            attempts=event.attempts,
            error=event.error,
            received_at=stamp(event.received_at, tz),
            processed_at=stamp(event.processed_at, tz),
        )


class WebhookAckOut(ApiModel):
    """What the gateway gets back. Deliberately uninformative.

    A 200 with `received` is the answer whether the delivery was new, a
    redelivery, or refused for a bad signature. Telling an unauthenticated
    caller *which* would let them probe for valid event ids and confirm whether
    a forged signature got closer.
    """

    status: str = "received"


class AddonActionOut(ApiModel):
    """The result of switching a module on or off, in the terms a person needs.

    `charged` and `reinstated` are the two answers that surprise people, so
    they are returned rather than inferred: activating something you had just
    cancelled charges nothing, and cancelling charges nothing either.
    """

    feature_key: str
    active: bool
    charged: bool
    reinstated: bool
    #: The one-off invoice a mid-cycle activation raised, if any.
    invoice: InvoiceOut | None
    cancels_on: str | None
    prorated_days: int | None
    cycle_days: int | None
    amount: float | None


class ModuleOut(ApiModel):
    """One module as its card and its page show it.

    `monthly` and `yearly` are null when the catalogue has no active price on
    that interval — which is not the same as free. `included` is what free
    means here: bundled by the base plan, switched on, never invoiced
    separately. A zero would read as "free extra" and be wrong.
    """

    feature_key: str
    name: str
    summary: str
    is_core: bool

    headline: str
    long_description: str
    highlights: list[str]
    video_url: str
    video_poster_url: str
    video_duration_s: int | None
    screenshots: list[dict]
    doc_links: list[dict]
    category: str
    badge: str

    currency: str
    monthly: float | None
    yearly: float | None

    included: bool
    subscribed: bool
    cancels_on: str | None
    in_cart: bool
    purchasable: bool

    @classmethod
    def of(cls, card, tz: str | None = None) -> ModuleOut:
        return cls(
            feature_key=card.feature_key,
            name=card.name,
            summary=card.summary,
            is_core=card.is_core,
            headline=card.headline,
            long_description=card.long_description,
            highlights=[str(h) for h in card.highlights],
            video_url=card.video_url,
            video_poster_url=card.video_poster_url,
            video_duration_s=card.video_duration_s,
            screenshots=[dict(s) for s in card.screenshots if isinstance(s, dict)],
            doc_links=[dict(d) for d in card.doc_links if isinstance(d, dict)],
            category=card.category,
            badge=card.badge,
            currency=str(getattr(card.currency, "value", card.currency)),
            monthly=money(card.monthly) if card.monthly is not None else None,
            yearly=money(card.yearly) if card.yearly is not None else None,
            included=card.included,
            subscribed=card.subscribed,
            cancels_on=iso_date(card.cancels_on),
            in_cart=card.in_cart,
            purchasable=card.purchasable,
        )


class CartItemIn(InputModel):
    """What a browser may say about a cart line, which is deliberately little.

    A module and an interval — two things that name a row in the catalogue. No
    amount, no quantity of money, nothing a client could use to choose its own
    price. The server looks the figure up.
    """

    feature_key: str = Field(max_length=40)
    interval: str = Field(default="monthly", pattern="^(monthly|yearly)$")


class CartLineOut(ApiModel):
    """One module in the cart, priced two ways.

    `list_amount` recurs every cycle; `amount_now` is this payment's share of
    the cycle in flight, and is smaller whenever the module is bought partway
    through one. Showing only one of them is how a clinic ends up believing it
    was overcharged, so both are always sent.
    """

    feature_key: str
    name: str
    interval: str
    list_amount: float
    amount_now: float
    prorated_days: int | None
    cycle_days: int | None
    reinstated: bool
    already_paid: bool
    #: Why this line cannot be bought, or null. Carried per line so a cart can
    #: show every problem at once instead of failing on the first.
    blocked: str | None

    @classmethod
    def of(cls, line) -> CartLineOut:
        return cls(
            feature_key=line.feature_key,
            name=line.name,
            interval=str(getattr(line.interval, "value", line.interval)),
            list_amount=money(line.list_amount),
            amount_now=money(line.amount_now),
            prorated_days=line.prorated_days,
            cycle_days=line.cycle_days,
            reinstated=line.reinstated,
            already_paid=line.already_paid,
            blocked=line.blocked,
        )


class CartQuoteOut(ApiModel):
    """The cart, totalled. Requirement 4.

    `total` is what leaving this page costs. `recurring_total` is what every
    invoice after it carries. They differ whenever anything was prorated, and
    a screen that showed one figure would be read as the other.
    """

    lines: list[CartLineOut]
    currency: str
    subtotal: float
    tax_total: float
    total: float
    recurring_total: float
    period_start: str
    period_end: str
    purchasable: bool

    @classmethod
    def of(cls, quote) -> CartQuoteOut:
        return cls(
            lines=[CartLineOut.of(line) for line in quote.lines],
            currency=str(getattr(quote.currency, "value", quote.currency)),
            subtotal=money(quote.subtotal),
            tax_total=money(quote.tax_total),
            total=money(quote.total),
            recurring_total=money(quote.recurring_total),
            period_start=iso_date(quote.period_start),
            period_end=iso_date(quote.period_end),
            purchasable=quote.purchasable,
        )


class CheckoutResultOut(ApiModel):
    """What a checkout did.

    `invoice` is null when nothing was owed — every module in the cart was free
    this cycle, or already paid for. That is a success, and `reinstated` says
    which modules came back on without a charge, because "why was I not
    billed" is the question that follows.
    """

    invoice: InvoiceOut | None
    activated: list[str]
    reinstated: list[str]

    @classmethod
    def of(cls, result, invoice: InvoiceOut | None) -> CheckoutResultOut:
        return cls(
            invoice=invoice,
            activated=list(result.activated),
            reinstated=list(result.reinstated),
        )


__all__ = [
    "AddonActionOut", "AddonMetricOut", "AddonOut", "BillingAccountOut", "BillingAccountPatch",
    "CartItemIn", "CartLineOut", "CartQuoteOut", "CheckoutResultOut", "ModuleOut",
    "BillingMetricsOut", "BillingSummaryOut", "CancelIn", "CheckoutOut", "InvoiceItemOut",
    "InvoiceOut", "InvoicePageOut", "PaymentOut", "PaymentVerifyIn", "PlanChangeIn", "PlanOut",
    "RefundIn", "RefundOut", "SubscribeIn", "SubscriptionOut", "TenantMetricOut", "WebhookAckOut",
    "WebhookEventOut",
]
