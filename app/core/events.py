"""In-process publish/subscribe, one channel per clinic.

The queue is the one screen where staleness is felt immediately: a token called
at the desk has to appear on the waiting-room screen and on the clinician's
dashboard now, not on the next poll. §3.6 calls for a push, so this is it.

Channels are keyed by tenant, and a subscriber is handed a channel by id rather
than choosing one — the same rule as everywhere else in this codebase: a
connection can only ever receive its own clinic's events.

This broker lives in one process. Behind more than one worker, replace
`publish` with a Redis or NATS publish and have each worker subscribe: the
interface here is deliberately the same shape.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("pms.events")

# A slow client must not hold the writer up, and must not grow without bound.
QUEUE_MAXSIZE = 64


class EventBroker:
    def __init__(self) -> None:
        self._channels: dict[uuid.UUID, set[asyncio.Queue]] = {}

    @contextlib.asynccontextmanager
    async def subscribe(self, tenant_id: uuid.UUID) -> AsyncIterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._channels.setdefault(tenant_id, set()).add(queue)
        try:
            yield queue
        finally:
            subscribers = self._channels.get(tenant_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    del self._channels[tenant_id]

    def publish(self, tenant_id: uuid.UUID, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Fan out to this clinic's subscribers. Never raises, never blocks."""
        subscribers = self._channels.get(tenant_id)
        if not subscribers:
            return

        event = {
            "type": event_type,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **(payload or {}),
        }
        for queue in list(subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A client that cannot keep up gets dropped rather than slowing
                # the request that published. It will resynchronise on reconnect.
                log.warning("dropping an event for a subscriber that is not keeping up")

    def subscriber_count(self, tenant_id: uuid.UUID) -> int:
        return len(self._channels.get(tenant_id, ()))


broker = EventBroker()
