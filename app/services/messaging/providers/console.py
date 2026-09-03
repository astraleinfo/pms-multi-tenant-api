"""The provider that does not send.

This is the default, and it is deliberately the default: a clinic that has not
finished configuring a carrier must not silently start texting patients, and
the test suite must never reach the network. It records what *would* have gone
out, which is also what makes it useful in development.
"""

from __future__ import annotations

import logging
import uuid

from app.services.messaging.providers.base import Outbound, Provider, SendResult

log = logging.getLogger("pms.messaging")


class ConsoleProvider(Provider):
    name = "console"

    async def send(self, message: Outbound) -> SendResult:
        log.info(
            "console provider would send",
            extra={
                "to": message.to,
                "channel": message.channel,
                "sender_id": message.sender_id,
                "body": message.body,
            },
        )
        return SendResult(provider_message_id=f"console:{uuid.uuid4()}")
