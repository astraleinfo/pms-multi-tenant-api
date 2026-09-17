"""The Razorpay adapter, assembled.

This package is the only thing in the codebase that knows what Razorpay is.
Everything it exports is shaped by `payment/gateway.py`, so the billing engine
holds a `Gateway` and could hold a different one tomorrow without a line of
arithmetic changing.

`SUPPORTED_EVENTS` is deliberately a list rather than a catch-all. Razorpay
will deliver whatever is switched on in the dashboard, and an event this
product has no opinion about is stored and marked `ignored` rather than
half-processed — which is the difference between a webhook log you can audit
and one you can only guess at.
"""

from __future__ import annotations

import functools
import hashlib
import json
from typing import Any

from app.core.config import settings
from app.services.payment.gateway import WebhookEnvelope
from app.services.payment.razorpay import client
from app.services.payment.razorpay.customer_service import RazorpayCustomerService
from app.services.payment.razorpay.invoice_service import RazorpayInvoiceService
from app.services.payment.razorpay.payment_service import RazorpayPaymentService
from app.services.payment.razorpay.subscription_service import RazorpaySubscriptionService

#: The events this product acts on, grouped by the Razorpay product that emits
#: them. Enable exactly these in the dashboard; anything else is stored and
#: ignored, and anything missing simply never arrives.
SUPPORTED_EVENTS: frozenset[str] = frozenset(
    {
        # Payments — the ones that move an invoice.
        "payment.captured",
        "payment.failed",
        # Subscriptions — lifecycle, mirrored into `gateway_status`.
        "subscription.activated",
        "subscription.charged",
        "subscription.pending",
        "subscription.halted",
        "subscription.paused",
        "subscription.resumed",
        "subscription.cancelled",
        "subscription.completed",
        # Hosted invoices — the "Pay Now" page.
        "invoice.paid",
        "invoice.partially_paid",
        "invoice.expired",
        # Refunds.
        "refund.created",
        "refund.processed",
        "refund.failed",
    }
)

#: Which entity in a delivery's payload the event is *about*. Razorpay nests
#: every entity it touched under `payload`, so a `payment.captured` delivery
#: also carries the order; this says which one identifies the event.
_PRIMARY_ENTITY = {
    "payment": "payment",
    "subscription": "subscription",
    "invoice": "invoice",
    "refund": "refund",
    "order": "order",
}


def _entity_id(event_type: str, payload: dict[str, Any]) -> str:
    """The gateway id this delivery is about.

    Stored on the webhook row so a delivery can be found — and correlated with
    a payment or a subscription — without anybody parsing the JSON again.
    """
    kind = event_type.split(".", 1)[0]
    holder = _PRIMARY_ENTITY.get(kind, kind)
    entity = ((payload.get("payload") or {}).get(holder) or {}).get("entity") or {}
    return str(entity.get("id") or "")


class RazorpayGateway:
    """One gateway, as `payment/gateway.py` defines one."""

    name = "razorpay"

    def __init__(self) -> None:
        self.customers = RazorpayCustomerService()
        self.subscriptions = RazorpaySubscriptionService()
        self.payments = RazorpayPaymentService()
        self.invoices = RazorpayInvoiceService()

    @property
    def configured(self) -> bool:
        return settings.razorpay_configured

    @property
    def public_key(self) -> str:
        """The key id, and nothing else, ever.

        This is the one credential value that is allowed to reach a browser: it
        identifies the merchant to Checkout and is public by design. The key
        secret and the webhook secret have no accessor at all.
        """
        return settings.razorpay_key_id

    def verify_webhook(self, *, body: bytes, signature: str) -> bool:
        return client.verify_webhook(body=body, signature=signature)

    def parse_webhook(self, *, body: bytes, headers: dict[str, str]) -> WebhookEnvelope:
        """Reduce a delivery to the shape the handler works in.

        The signature is verified against the **raw bytes** before the JSON is
        touched, because re-serialising would change them. An unparseable body
        still produces an envelope — with an empty payload and whatever the
        signature check said — so that it can be stored and refused rather than
        disappearing into a 500.
        """
        lowered = {k.lower(): v for k, v in headers.items()}
        signature = lowered.get("x-razorpay-signature", "")
        verified = self.verify_webhook(body=body, signature=signature)

        try:
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                payload = {}
        except (ValueError, UnicodeDecodeError):
            payload = {}

        # Razorpay sends its own delivery id. When it is missing — a replay
        # through a proxy that stripped headers, say — a digest of the body
        # stands in, so a redelivery still collides on the unique index rather
        # than being processed twice.
        event_id = lowered.get("x-razorpay-event-id") or (
            "sha256:" + hashlib.sha256(body).hexdigest()
        )
        event_type = str(payload.get("event") or "")

        return WebhookEnvelope(
            event_id=event_id[:120],
            event_type=event_type[:80],
            entity_id=_entity_id(event_type, payload)[:80],
            payload=payload,
            signature_verified=verified,
        )


@functools.lru_cache(maxsize=1)
def gateway() -> RazorpayGateway:
    """The adapter instance. Resolved by name from `payment/gateway.py`.

    Cached because it holds no per-request state: each call opens its own HTTP
    client, so there is nothing here that could leak between clinics.
    """
    return RazorpayGateway()


__all__ = ["SUPPORTED_EVENTS", "RazorpayGateway", "gateway"]
