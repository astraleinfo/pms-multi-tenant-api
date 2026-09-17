"""Razorpay orders, payments and refunds.

The conversion to paise happens here and only here: `to_minor_units` is called
on the way out, `from_minor_units` on the way back, and everything above this
file deals in `Decimal`.

`captured` is computed from Razorpay's own `status` field rather than from
anything a caller passes. It is the single fact the billing engine trusts to
mean "the money is ours", so it is derived from the gateway's word in one
place.
"""

from __future__ import annotations

from app.services.payment.gateway import Money, OrderRef, PaymentRef, RefundRef
from app.services.payment.money import from_minor_units, to_minor_units
from app.services.payment.razorpay import client

#: The only Razorpay payment status that means the money has actually moved.
#: `authorized` is permission to take it, which is not the same thing, and
#: treating the two alike is how an invoice ends up marked paid for a payment
#: that was later voided.
_CAPTURED = "captured"


def _payment(body: dict) -> PaymentRef:
    status = body.get("status", "")
    return PaymentRef(
        id=body.get("id", ""),
        amount=Money(
            amount=from_minor_units(body.get("amount", 0)),
            currency=body.get("currency", "INR"),
        ),
        status=status,
        captured=status == _CAPTURED,
        method=body.get("method", "") or "",
        order_id=body.get("order_id", "") or "",
        error=body.get("error_description", "") or "",
        created_at=client.epoch(body.get("created_at")),
        raw=body,
    )


class RazorpayPaymentService:
    """Implements `gateway.PaymentAdapter`."""

    async def create_order(
        self, *, amount: Money, receipt: str, notes: dict[str, str] | None = None
    ) -> OrderRef:
        body = await client.request(
            "POST",
            "/orders",
            json={
                "amount": to_minor_units(amount.amount, amount.currency),
                "currency": amount.currency,
                # Razorpay caps this at 40 characters and rejects a longer one.
                "receipt": receipt[:40],
                "notes": client.notes(notes),
            },
        )
        return OrderRef(
            id=body.get("id", ""),
            amount=Money(
                amount=from_minor_units(body.get("amount", 0)),
                currency=body.get("currency", amount.currency),
            ),
            receipt=body.get("receipt", "") or "",
            raw=body,
        )

    async def fetch(self, payment_id: str) -> PaymentRef:
        return _payment(await client.request("GET", f"/payments/{payment_id}"))

    def verify_checkout_signature(
        self, *, order_id: str, payment_id: str, signature: str
    ) -> bool:
        return client.verify_checkout(
            order_id=order_id, payment_id=payment_id, signature=signature
        )

    async def refund(
        self, *, payment_id: str, amount: Money | None = None, notes: dict[str, str] | None = None
    ) -> RefundRef:
        payload: dict = {"notes": client.notes(notes)}
        if amount is not None:
            # Omitted entirely for a full refund — Razorpay reads a missing
            # amount as "all of it", and sending the full amount explicitly
            # fails when a partial refund has already been taken.
            payload["amount"] = to_minor_units(amount.amount, amount.currency)
        body = await client.request("POST", f"/payments/{payment_id}/refund", json=payload)
        return RefundRef(
            id=body.get("id", ""),
            payment_id=body.get("payment_id", payment_id),
            amount=Money(
                amount=from_minor_units(body.get("amount", 0)),
                currency=body.get("currency", "INR"),
            ),
            status=body.get("status", ""),
            raw=body,
        )
