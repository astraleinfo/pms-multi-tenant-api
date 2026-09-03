"""Twilio SMS and WhatsApp.

One HTTP call, form-encoded, basic auth. The only subtlety is WhatsApp, where
both endpoints of the conversation carry a `whatsapp:` scheme prefix.

Twilio's status codes are mapped onto the two failure shapes rather than passed
through: 429 and 5xx are worth retrying, and a 400 that says "not a mobile
number" is not.
"""

from __future__ import annotations

import httpx

from app.services.messaging.providers.base import (
    Outbound,
    Provider,
    SendRefused,
    SendResult,
    SendRetryable,
)

API_ROOT = "https://api.twilio.com/2010-04-01"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)


def _address(value: str, channel: str) -> str:
    if channel != "whatsapp":
        return value
    return value if value.startswith("whatsapp:") else f"whatsapp:{value}"


class TwilioProvider(Provider):
    name = "twilio"

    def __init__(self, account_sid: str, auth_token: str) -> None:
        if not account_sid or not auth_token:
            raise SendRefused("Twilio is selected but its credentials are not set.")
        self._sid = account_sid
        self._token = auth_token

    async def send(self, message: Outbound) -> SendResult:
        if not message.sender_id:
            raise SendRefused("Twilio needs a sender number; none is configured.")

        form = {
            "To": _address(message.to, message.channel),
            "From": _address(message.sender_id, message.channel),
            "Body": message.body,
        }
        url = f"{API_ROOT}/Accounts/{self._sid}/Messages.json"

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.post(url, data=form, auth=(self._sid, self._token))
        except httpx.HTTPError as exc:  # DNS, TLS, timeout — all worth retrying
            raise SendRetryable(f"Could not reach Twilio: {exc}") from exc

        if response.status_code < 300:
            return SendResult(provider_message_id=str(response.json().get("sid", "")))

        detail = _detail(response)
        # 429 is a rate limit and 5xx is Twilio's problem; both deserve a retry.
        if response.status_code == 429 or response.status_code >= 500:
            raise SendRetryable(f"Twilio {response.status_code}: {detail}")
        raise SendRefused(f"Twilio {response.status_code}: {detail}")


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    return str(body.get("message") or body)[:200]
