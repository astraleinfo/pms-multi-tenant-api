"""Razorpay hosted invoices — what the "Pay Now" button in an email opens.

The invoice this system raises is the authority on what is owed; the one
created here is a payment page for it, carrying the same number and the same
total. Razorpay mints the `short_url`, which is stored against our invoice so
the link in an email that went out last month still works.

One line item, not many: the detail lives on our invoice, which is the document
the clinic is actually being billed under, and splitting it across two systems
would mean two places to disagree about the total.
"""

from __future__ import annotations

from datetime import datetime

from app.services.payment.gateway import HostedInvoiceRef, Money
from app.services.payment.money import to_minor_units
from app.services.payment.razorpay import client


def _ref(body: dict) -> HostedInvoiceRef:
    return HostedInvoiceRef(
        id=body.get("id", ""),
        short_url=body.get("short_url", "") or "",
        status=body.get("status", "") or "",
        raw=body,
    )


class RazorpayInvoiceService:
    """Implements `gateway.InvoiceAdapter`."""

    async def create(
        self,
        *,
        customer_id: str,
        number: str,
        amount: Money,
        description: str,
        due_at: datetime | None = None,
        notes: dict[str, str] | None = None,
    ) -> HostedInvoiceRef:
        payload: dict = {
            "type": "invoice",
            "description": description[:2048],
            "currency": amount.currency,
            "receipt": number[:40],
            "line_items": [
                {
                    "name": description[:255] or number,
                    "amount": to_minor_units(amount.amount, amount.currency),
                    "currency": amount.currency,
                    "quantity": 1,
                }
            ],
            "sms_notify": 0,
            # We send our own invoice email, with our own totals and line
            # items. Letting Razorpay send a second one would give the clinic
            # two invoices for one charge.
            "email_notify": 0,
            "notes": client.notes(notes),
        }
        if customer_id:
            payload["customer_id"] = customer_id
        if due_at is not None:
            payload["expire_by"] = int(due_at.timestamp())
        return _ref(await client.request("POST", "/invoices", json=payload))

    async def fetch(self, invoice_id: str) -> HostedInvoiceRef:
        return _ref(await client.request("GET", f"/invoices/{invoice_id}"))

    async def cancel(self, invoice_id: str) -> HostedInvoiceRef:
        return _ref(await client.request("POST", f"/invoices/{invoice_id}/cancel"))
