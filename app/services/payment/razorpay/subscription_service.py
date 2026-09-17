"""Razorpay subscriptions — create, fetch, pause, resume, cancel.

`status` comes back exactly as Razorpay wrote it and is handed up unmapped. The
billing engine stores it beside its own status and never obeys it: a gateway
that thinks a subscription is halted does not get to stop a clinic whose
invoices this system knows are paid.
"""

from __future__ import annotations

from datetime import datetime

from app.services.payment.gateway import SubscriptionRef
from app.services.payment.razorpay import client


def _ref(body: dict) -> SubscriptionRef:
    return SubscriptionRef(
        id=body.get("id", ""),
        status=body.get("status", ""),
        current_start=client.epoch(body.get("current_start")),
        current_end=client.epoch(body.get("current_end")),
        raw=body,
    )


class RazorpaySubscriptionService:
    """Implements `gateway.SubscriptionAdapter`."""

    async def create(
        self,
        *,
        plan_reference: str,
        customer_id: str,
        total_count: int,
        start_at: datetime | None = None,
        notes: dict[str, str] | None = None,
    ) -> SubscriptionRef:
        payload: dict = {
            "plan_id": plan_reference,
            "total_count": max(int(total_count), 1),
            "customer_notify": 1,
            "notes": client.notes(notes),
        }
        if customer_id:
            payload["customer_id"] = customer_id
        if start_at is not None:
            payload["start_at"] = int(start_at.timestamp())
        return _ref(await client.request("POST", "/subscriptions", json=payload))

    async def fetch(self, subscription_id: str) -> SubscriptionRef:
        return _ref(await client.request("GET", f"/subscriptions/{subscription_id}"))

    async def pause(self, subscription_id: str) -> SubscriptionRef:
        return _ref(
            await client.request(
                "POST", f"/subscriptions/{subscription_id}/pause", json={"pause_at": "now"}
            )
        )

    async def resume(self, subscription_id: str) -> SubscriptionRef:
        return _ref(
            await client.request(
                "POST", f"/subscriptions/{subscription_id}/resume", json={"resume_at": "now"}
            )
        )

    async def cancel(self, subscription_id: str, *, at_cycle_end: bool) -> SubscriptionRef:
        # Razorpay takes 1/0 here, not true/false, and the difference decides
        # whether the clinic keeps the period it has already paid for.
        return _ref(
            await client.request(
                "POST",
                f"/subscriptions/{subscription_id}/cancel",
                json={"cancel_at_cycle_end": 1 if at_cycle_end else 0},
            )
        )
