"""What a provider is.

An adapter takes a rendered message and tries to hand it to a carrier. It says
nothing about queues, retries, or persistence — those are the outbox's job, and
keeping them out of here is what makes a new provider a single small file.

Two failure shapes, and the difference decides whether we try again:

*   `SendRetryable` — the carrier was unreachable, rate-limited, or returned 5xx.
    Worth another attempt later.
*   `SendRefused` — the carrier understood and said no: bad number, blocked
    recipient, bad credentials. Trying again would fail identically, so the
    message is parked rather than spun on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class SendRetryable(Exception):
    """Transient. The worker will back off and try again."""


class SendRefused(Exception):
    """Permanent. The message is marked failed without further attempts."""


@dataclass(frozen=True)
class Outbound:
    to: str
    body: str
    channel: str
    sender_id: str


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str = ""


@runtime_checkable
class Provider(Protocol):
    """Implement this, register it, and the clinic can switch to it."""

    name: str

    async def send(self, message: Outbound) -> SendResult:
        """Deliver, or raise SendRetryable / SendRefused."""
        ...
