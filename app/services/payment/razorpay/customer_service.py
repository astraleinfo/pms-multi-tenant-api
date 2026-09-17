"""Razorpay customers — the gateway's idea of a clinic.

One customer per tenant, keyed by the tenant id in `notes`. `fail_existing=0`
is what makes `ensure` idempotent: Razorpay returns the existing customer for a
repeated email instead of erroring, so a retried onboarding does not leave two
behind.
"""

from __future__ import annotations

from app.services.payment.gateway import CustomerRef
from app.services.payment.razorpay import client


class RazorpayCustomerService:
    """Implements `gateway.CustomerAdapter`."""

    async def ensure(
        self, *, reference: str, name: str, email: str, phone: str = ""
    ) -> CustomerRef:
        payload = {
            "name": (name or reference)[:50],
            # Razorpay rejects a blank string where it accepts a missing key,
            # so an unknown contact is omitted rather than sent empty.
            **({"email": email} if email else {}),
            **({"contact": phone} if phone else {}),
            "fail_existing": 0,
            "notes": client.notes({"tenant_id": reference}),
        }
        body = await client.request("POST", "/customers", json=payload)
        return CustomerRef(id=body.get("id", ""), email=body.get("email", ""), name=body.get("name", ""))

    async def fetch(self, customer_id: str) -> CustomerRef:
        body = await client.request("GET", f"/customers/{customer_id}")
        return CustomerRef(id=body.get("id", ""), email=body.get("email", ""), name=body.get("name", ""))
