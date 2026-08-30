"""The provider registry.

Adding a carrier is three lines: write the adapter, add a member to
`MessageProvider`, and add an entry to `_BUILDERS`. Nothing in the outbox, the
worker, the API or the UI needs to know it happened — a clinic switches by
changing one column.

The builder takes the clinic's own config, so credentials never become process
globals and two clinics on different Twilio accounts cannot collide.
"""

from __future__ import annotations

from collections.abc import Callable

from app.models.enums import MessageProvider
from app.models.messaging import MessagingConfig
from app.services.messaging.crypto import decrypt_secret
from app.services.messaging.providers.base import (
    Outbound,
    Provider,
    SendRefused,
    SendResult,
    SendRetryable,
)
from app.services.messaging.providers.console import ConsoleProvider
from app.services.messaging.providers.twilio import TwilioProvider


def _console(_: MessagingConfig) -> Provider:
    return ConsoleProvider()


def _twilio(config: MessagingConfig) -> Provider:
    return TwilioProvider(
        account_sid=config.account_sid,
        auth_token=decrypt_secret(config.auth_token_encrypted),
    )


_BUILDERS: dict[MessageProvider, Callable[[MessagingConfig], Provider]] = {
    MessageProvider.CONSOLE: _console,
    MessageProvider.TWILIO: _twilio,
}


def build_provider(config: MessagingConfig) -> Provider:
    """The adapter this clinic is currently switched to."""
    builder = _BUILDERS.get(MessageProvider(config.provider))
    if builder is None:  # pragma: no cover - unreachable while the enum is the source
        raise SendRefused(f"No adapter is registered for {config.provider}.")
    return builder(config)


def available_providers() -> list[str]:
    """What the settings screen offers. Derived, never a second hardcoded list."""
    return [p.value for p in MessageProvider if p in _BUILDERS]


__all__ = [
    "Outbound",
    "Provider",
    "SendRefused",
    "SendResult",
    "SendRetryable",
    "available_providers",
    "build_provider",
]
