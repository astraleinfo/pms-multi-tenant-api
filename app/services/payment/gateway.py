"""The seam. What a payment gateway is, expressed without naming one.

Everything above this file — `subscription_service`, `billing_service`,
`invoice/generation_service`, `proration/calculator`, `addon/billing_service`,
`metrics/` — works out what a clinic owes and never imports anything from
`payment/razorpay/`. Everything below it talks HTTP to somebody and never
decides what anything costs. Adding Cashfree or Stripe is a new package beside
`razorpay/` plus a line in `_ADAPTERS`; not one arithmetic path changes.

Three shapes carry the weight:

*   **Amounts are `Decimal` with a currency, here.** Minor units — paise,
    cents — are a gateway's encoding, and converting to them is the adapter's
    job. A `Decimal` that has been multiplied by 100 somewhere in the billing
    engine is a bug waiting for a currency without hundredths.
*   **Failures come in two kinds**, the same split the messaging providers use
    (`app/services/messaging/providers/base.py`), because it is the split that
    decides whether a job retries: `GatewayUnavailable` is worth another
    attempt, `GatewayRefused` is not.
*   **Nothing here returns a secret.** `checkout_context()` is the only thing
    a browser is given, and it is the public key id and an order id — both of
    which are meant to be public. The key secret and the webhook secret never
    leave the process.

The protocols are `runtime_checkable` so a deployment can assert a partially
implemented adapter at start-up rather than at the first renewal.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from app.core.errors import AppError


class GatewayError(AppError):
    """Base. Reaches a client as a 502 rather than leaking adapter text."""

    status_code = 502
    code = "gateway_error"


class GatewayNotConfigured(GatewayError):
    """No credentials, or no adapter for the configured gateway. A deployment
    problem, not a clinic's."""

    code = "gateway_not_configured"


class GatewayUnavailable(GatewayError):
    """Transient — a timeout, a 5xx, a rate limit. The job will try again."""

    code = "gateway_unavailable"


class GatewayRefused(GatewayError):
    """The gateway understood and said no. Trying again would fail the same
    way, so the caller records it instead of spinning."""

    status_code = 402
    code = "gateway_refused"


class SignatureInvalid(GatewayError):
    """A payload did not carry the signature it should have.

    Deliberately its own class, and never merged into `GatewayRefused`: this is
    the one failure that means somebody may be lying to us, and it is audited
    rather than retried.
    """

    status_code = 400
    code = "signature_invalid"


# --- what crosses the seam ---------------------------------------------------


@dataclass(frozen=True)
class Money:
    """An amount and the currency it is in. Never a bare number."""

    amount: Decimal
    currency: str = "INR"

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("a gateway is never asked to move a negative amount")


@dataclass(frozen=True)
class CustomerRef:
    """The gateway's idea of the clinic."""

    id: str
    email: str = ""
    name: str = ""


@dataclass(frozen=True)
class SubscriptionRef:
    """A subscription at the gateway.

    `status` is the gateway's own word, unmapped. The billing engine keeps its
    own `SubscriptionStatus` and treats this as evidence, not instruction —
    which is what stops a gateway outage from cancelling a clinic.
    """

    id: str
    status: str = ""
    current_start: datetime | None = None
    current_end: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderRef:
    """An intent to collect. What checkout is opened against."""

    id: str
    amount: Money
    receipt: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PaymentRef:
    """A payment, as the gateway describes it.

    `captured` is the only field the billing engine trusts to mean "paid", and
    it is only ever set from a server-side fetch or a verified webhook.
    """

    id: str
    amount: Money
    status: str
    captured: bool
    method: str = ""
    order_id: str = ""
    error: str = ""
    created_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RefundRef:
    id: str
    payment_id: str
    amount: Money
    status: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HostedInvoiceRef:
    """An invoice the gateway hosts, and the page its "Pay Now" opens.

    `short_url` is what goes in the email. It is minted by the gateway, carries
    no secret of ours, and is stored rather than rebuilt so the link in an old
    email keeps working.
    """

    id: str
    short_url: str = ""
    status: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WebhookEnvelope:
    """One delivery, reduced to what the handler needs before it trusts it.

    Every adapter produces this shape, so `webhook/handler.py` contains no
    gateway-specific parsing at all — only the idempotency, the storing and the
    dispatch.
    """

    event_id: str
    event_type: str
    entity_id: str
    payload: dict[str, Any]
    signature_verified: bool


@dataclass(frozen=True)
class CheckoutContext:
    """Everything a browser is given to open a payment, and nothing else.

    The key id is public by design. The amount is the one the *server* worked
    out — it is echoed here so the widget can display it, and it is re-checked
    server-side before any payment is believed, because a browser that sends
    back a different number is not evidence of anything.
    """

    gateway: str
    key_id: str
    order_id: str
    amount: Money
    invoice_number: str
    customer_name: str = ""
    customer_email: str = ""
    description: str = ""


# --- what an adapter must provide --------------------------------------------


@runtime_checkable
class CustomerAdapter(Protocol):
    async def ensure(
        self, *, reference: str, name: str, email: str, phone: str = ""
    ) -> CustomerRef:
        """Find or create the gateway's customer for this clinic.

        Idempotent by `reference` (the tenant id), because a retried onboarding
        must not leave two customers behind.
        """
        ...

    async def fetch(self, customer_id: str) -> CustomerRef: ...


@runtime_checkable
class SubscriptionAdapter(Protocol):
    async def create(
        self,
        *,
        plan_reference: str,
        customer_id: str,
        total_count: int,
        start_at: datetime | None = None,
        notes: dict[str, str] | None = None,
    ) -> SubscriptionRef: ...

    async def fetch(self, subscription_id: str) -> SubscriptionRef: ...

    async def pause(self, subscription_id: str) -> SubscriptionRef: ...

    async def resume(self, subscription_id: str) -> SubscriptionRef: ...

    async def cancel(self, subscription_id: str, *, at_cycle_end: bool) -> SubscriptionRef: ...


@runtime_checkable
class PaymentAdapter(Protocol):
    async def create_order(
        self, *, amount: Money, receipt: str, notes: dict[str, str] | None = None
    ) -> OrderRef: ...

    async def fetch(self, payment_id: str) -> PaymentRef: ...

    def verify_checkout_signature(
        self, *, order_id: str, payment_id: str, signature: str
    ) -> bool:
        """Does this browser's claim carry our own signature?

        Necessary and not sufficient: the caller still re-fetches the payment
        and compares the amount. A valid signature proves the fields were not
        tampered with in the browser, not that the money arrived.
        """
        ...

    async def refund(
        self, *, payment_id: str, amount: Money | None = None, notes: dict[str, str] | None = None
    ) -> RefundRef: ...


@runtime_checkable
class InvoiceAdapter(Protocol):
    async def create(
        self,
        *,
        customer_id: str,
        number: str,
        amount: Money,
        description: str,
        due_at: datetime | None = None,
        notes: dict[str, str] | None = None,
    ) -> HostedInvoiceRef: ...

    async def fetch(self, invoice_id: str) -> HostedInvoiceRef: ...

    async def cancel(self, invoice_id: str) -> HostedInvoiceRef: ...


class Gateway(Protocol):
    """One gateway, assembled. The only object the billing engine ever holds."""

    name: str
    customers: CustomerAdapter
    subscriptions: SubscriptionAdapter
    payments: PaymentAdapter
    invoices: InvoiceAdapter

    @property
    def configured(self) -> bool:
        """Whether this deployment can actually reach the gateway."""
        ...

    @property
    def public_key(self) -> str:
        """The half of the credential pair a browser may see. Nothing else
        about the credentials is exposed anywhere in this codebase."""
        ...

    def verify_webhook(self, *, body: bytes, signature: str) -> bool: ...

    def parse_webhook(self, *, body: bytes, headers: dict[str, str]) -> WebhookEnvelope: ...


# --- the registry ------------------------------------------------------------
#
# Imported lazily and by name. That is not a micro-optimisation: it is what
# lets this module — and therefore every billing service that imports it — stay
# free of any dependency on a particular gateway's client library.

_ADAPTERS: dict[str, str] = {
    "razorpay": "app.services.payment.razorpay",
}


def available_gateways() -> list[str]:
    return sorted(_ADAPTERS)


def get_gateway(name: str | None = None) -> Gateway:
    """The configured gateway, or the one named.

    Raises `GatewayNotConfigured` rather than returning something half-built:
    a billing path that silently degrades is a billing path that silently stops
    collecting money.
    """
    from app.core.config import settings

    key = (name or settings.billing_gateway or "").strip().lower()
    module_path = _ADAPTERS.get(key)
    if module_path is None:
        raise GatewayNotConfigured(
            f"No payment gateway adapter is installed for {key!r}.",
            details={"available": available_gateways()},
        )
    module = importlib.import_module(module_path)
    return module.gateway()
