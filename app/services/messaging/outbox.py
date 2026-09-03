"""The queue.

The `messages` row is the source of truth. Redis carries only a nudge saying
"something is due" — so if Redis is down, empty, or wiped, delivery is delayed
until the worker's next sweep but nothing is lost. That asymmetry is the whole
design: a patient message must never evaporate because a cache restarted.

Claiming is `FOR UPDATE SKIP LOCKED`, so several workers can run without two of
them sending the same message.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.enums import MessageStatus
from app.models.messaging import Message

log = logging.getLogger("pms.messaging")


async def nudge(message_id: str) -> None:
    """Tell a waiting worker there is something to do. Never raises: a failed
    nudge costs latency, not correctness."""
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url)
        try:
            await client.lpush(settings.outbox_stream, message_id)
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001 - deliberately swallowed, see docstring
        log.warning("outbox nudge failed, sweep will pick it up: %s", exc)


async def claim_due(session: AsyncSession, limit: int = 20) -> list[Message]:
    """Take up to `limit` messages that are due, locking them against other
    workers and flipping them to `sending` in the same transaction."""
    now = datetime.now(timezone.utc)
    rows = (
        (
            await session.execute(
                select(Message)
                .where(Message.status == MessageStatus.QUEUED, Message.send_after <= now)
                .order_by(Message.send_after)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = MessageStatus.SENDING
    return list(rows)


def backoff(attempts: int) -> datetime:
    """Exponential, capped at an hour. `attempts` is the count *after* the
    failure that triggered this."""
    seconds = min(settings.message_retry_base_seconds * (2 ** max(0, attempts - 1)), 3600)
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def mark_sent(message: Message, provider: str, provider_message_id: str) -> None:
    message.status = MessageStatus.SENT
    message.provider = provider
    message.provider_message_id = provider_message_id[:120]
    message.sent_at = datetime.now(timezone.utc)
    message.last_error = ""


def mark_retry(message: Message, error: str) -> None:
    """Put it back in the queue, later. Past the attempt ceiling it is parked."""
    message.attempts += 1
    message.last_error = error[:2000]
    if message.attempts >= settings.message_max_attempts:
        message.status = MessageStatus.FAILED
        log.warning("message %s parked after %s attempts", message.id, message.attempts)
        return
    message.status = MessageStatus.QUEUED
    message.send_after = backoff(message.attempts)


def mark_failed(message: Message, error: str) -> None:
    """Refused by the carrier. Retrying would fail identically."""
    message.attempts += 1
    message.last_error = error[:2000]
    message.status = MessageStatus.FAILED
