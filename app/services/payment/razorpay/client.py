"""The HTTP half of the Razorpay adapter: auth, transport, signatures, errors.

Nothing in here decides what anything costs. It takes a call, signs it, makes
it, and translates whatever comes back into the two failure shapes the billing
jobs know how to act on — `GatewayUnavailable` (try again) and `GatewayRefused`
(do not). That split is the same one `app/services/messaging/providers/base.py`
draws, and for the same reason: it is what a retry loop needs to be told.

Credentials never leave this module. `key_id` is exposed through
`RazorpayGateway.public_key` because a checkout widget needs it and it is the
public half of the pair; the secret is used only to sign, and the webhook
secret only to verify. Neither is logged, returned, or serialised anywhere —
including in the error paths, which is why `_describe` builds its message from
Razorpay's own error body rather than from the request.

Amounts cross this boundary as `Decimal` and become paise here, once
(`to_minor_units`). Above this file, in the billing engine, a number multiplied
by 100 would be a bug.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.services.payment.gateway import (
    GatewayNotConfigured,
    GatewayRefused,
    GatewayUnavailable,
)

log = logging.getLogger("pms.billing.razorpay")

#: Razorpay answers a client error with a body describing it, and a server
#: error with one that mostly does not. Only the first kind is worth showing.
_REFUSAL_STATUSES = frozenset({400, 401, 402, 403, 404, 409, 422})


def _auth() -> tuple[str, str]:
    if not settings.razorpay_configured:
        raise GatewayNotConfigured(
            "Razorpay credentials are not configured for this deployment."
        )
    return settings.razorpay_key_id, settings.razorpay_key_secret


def _describe(response: httpx.Response) -> str:
    """Razorpay's own words for why it said no, and nothing of ours.

    The request is never echoed: it carries the amount, the receipt and the
    notes, and an error message is a place those can end up in a log that
    outlives the request.
    """
    try:
        body = response.json()
    except ValueError:
        return f"Razorpay returned {response.status_code}."
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        description = error.get("description") or error.get("reason") or ""
        code = error.get("code") or ""
        return f"{code}: {description}".strip(": ") or f"Razorpay returned {response.status_code}."
    return f"Razorpay returned {response.status_code}."


async def request(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One call to Razorpay, with its failures translated.

    A timeout or a connection error is `GatewayUnavailable`, as is any 5xx and
    a 429: all of them mean "ask again later", and the billing jobs will. A 4xx
    that is not a rate limit is `GatewayRefused` — asking again would get the
    same answer, so the caller records it instead of spinning.
    """
    url = f"{settings.razorpay_api_base.rstrip('/')}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=settings.razorpay_timeout_seconds) as client:
            response = await client.request(
                method, url, json=json, params=params, auth=_auth()
            )
    except httpx.TimeoutException as exc:
        raise GatewayUnavailable("Razorpay did not answer in time.") from exc
    except httpx.HTTPError as exc:
        raise GatewayUnavailable(f"Razorpay could not be reached: {exc}") from exc

    if response.status_code == 429 or response.status_code >= 500:
        raise GatewayUnavailable(_describe(response))
    if response.status_code in _REFUSAL_STATUSES:
        raise GatewayRefused(_describe(response))
    if response.status_code >= 400:  # pragma: no cover - anything else is new
        raise GatewayUnavailable(_describe(response))

    try:
        payload = response.json()
    except ValueError as exc:
        raise GatewayUnavailable("Razorpay returned something that was not JSON.") from exc
    return payload if isinstance(payload, dict) else {"items": payload}


# --- signatures --------------------------------------------------------------


def sign(payload: bytes | str, secret: str) -> str:
    """HMAC-SHA256, hex. Razorpay's scheme for both signatures it issues."""
    body = payload.encode() if isinstance(payload, str) else payload
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(payload: bytes | str, signature: str, secret: str) -> bool:
    """Constant-time comparison.

    `compare_digest` rather than `==` is not decoration: a signature check that
    returns early on the first wrong byte leaks, one byte at a time, what the
    right signature would have been.
    """
    if not signature or not secret:
        return False
    return hmac.compare_digest(sign(payload, secret), signature)


def verify_checkout(*, order_id: str, payment_id: str, signature: str) -> bool:
    """The signature Checkout hands back: `order_id|payment_id`, key secret.

    Proves the browser did not edit the fields. It does *not* prove the money
    arrived — `payment_service.verify_and_capture` re-fetches the payment and
    compares the amount, and both checks have to pass.
    """
    if not settings.razorpay_configured:
        return False
    return verify(f"{order_id}|{payment_id}", signature, settings.razorpay_key_secret)


def verify_webhook(*, body: bytes, signature: str) -> bool:
    """The signature on a delivery: the raw body, webhook secret.

    The *raw* body, before any parsing — re-serialising the JSON would change
    the bytes and fail every time. A deployment with no webhook secret set
    verifies nothing, and the handler treats an unverified delivery as
    something to store and refuse rather than something to act on.
    """
    return verify(body, signature, settings.razorpay_webhook_secret)


# --- shapes ------------------------------------------------------------------


def epoch(value: Any) -> datetime | None:
    """Razorpay's timestamps are Unix seconds, and sometimes absent."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):  # pragma: no cover - malformed input
        return None


def notes(values: dict[str, str] | None) -> dict[str, str]:
    """Razorpay accepts at most 15 notes, each a short string.

    The tenant id goes in here on everything we create, which is what lets a
    webhook about an entity we have never seen still be traced to a clinic.
    """
    if not values:
        return {}
    return {str(k)[:256]: str(v)[:256] for k, v in list(values.items())[:15]}
