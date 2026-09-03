"""The sender.

A loop with two ways to wake: a Redis nudge when something is queued, and a
timed sweep that catches whatever the nudge missed — a message queued while the
worker was down, a retry whose backoff has expired, a nudge lost to a Redis
restart. The sweep is what makes Redis optional rather than load-bearing.

Each message is claimed with `FOR UPDATE SKIP LOCKED` and sent in its own
transaction, so one poisonous message cannot roll back a batch, and two workers
never send the same thing twice.

Row-level security applies to the worker too: it binds `app.tenant_id` per
message before touching anything, exactly like a request does.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import session_factory, set_tenant
from app.models.enums import TenantStatus
from app.models.messaging import Message
from app.models.tenancy import Tenant
from app.services.messaging import outbox
from app.services.messaging.providers import build_provider
from app.services.messaging.providers.base import Outbound, SendRefused, SendRetryable
from app.services.messaging.service import config_for

log = logging.getLogger("pms.messaging")

try:  # redis is optional at import time - the sweep alone still works
    from redis.exceptions import TimeoutError as RedisTimeout
except ImportError:  # pragma: no cover - only when redis is not installed
    class RedisTimeout(Exception):  # type: ignore[no-redef]
        pass


async def deliver_one(session: AsyncSession, message: Message) -> None:
    """Send a single claimed message and record what happened."""
    config = await config_for(session, message.tenant_id)
    try:
        provider = build_provider(config)
        result = await provider.send(
            Outbound(
                to=message.to_address,
                body=message.body,
                channel=str(message.channel),
                sender_id=config.sender_id,
            )
        )
    except SendRefused as exc:
        outbox.mark_failed(message, str(exc))
        log.warning("message %s refused: %s", message.id, exc)
        return
    except SendRetryable as exc:
        outbox.mark_retry(message, str(exc))
        log.info("message %s deferred: %s", message.id, exc)
        return
    except Exception as exc:  # noqa: BLE001 - an adapter bug must not kill the loop
        outbox.mark_retry(message, f"{type(exc).__name__}: {exc}")
        log.exception("message %s raised unexpectedly", message.id)
        return

    outbox.mark_sent(message, str(config.provider), result.provider_message_id)
    log.info("message %s sent via %s", message.id, config.provider)


async def _active_tenant_ids() -> list[uuid.UUID]:
    """Which clinics to sweep. `tenants` is the registry rather than a
    tenant-owned table, so this is the one query that legitimately runs with
    nothing bound."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Tenant.id).where(Tenant.status != TenantStatus.SUSPENDED)
            )
        ).scalars()
        return list(rows)


async def drain(limit: int = 20) -> int:
    """One pass over every clinic. Returns how many messages were handled.

    The sweep is per tenant on purpose. There is no cross-tenant read anywhere
    in this codebase and the worker is not an exception: it binds `app.tenant_id`
    for each clinic in turn, so row-level security and the ORM guard apply to it
    exactly as they do to a request. A suspended clinic is skipped entirely.
    """
    handled = 0
    for tenant_id in await _active_tenant_ids():
        while True:
            async with session_factory() as session:
                async with session.begin():
                    await set_tenant(session, tenant_id)
                    due = await outbox.claim_due(session, limit=limit)
                    ids = [m.id for m in due]
            if not ids:
                break

            for message_id in ids:
                async with session_factory() as work:
                    async with work.begin():
                        await set_tenant(work, tenant_id)
                        row = await work.get(Message, message_id)
                        if row is None:  # pragma: no cover - deleted mid-flight
                            continue
                        await deliver_one(work, row)
                        handled += 1
            if len(ids) < limit:
                break
    return handled


async def run_forever(stop: asyncio.Event | None = None) -> None:
    """Wake on a nudge, and sweep on a timer regardless."""
    stop = stop or asyncio.Event()
    log.info("message worker started (poll %ss)", settings.worker_poll_seconds)

    client = None
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            settings.redis_url,
            # must outlast the blpop block, or every idle wait looks like a fault
            socket_timeout=settings.worker_poll_seconds + 15,
            socket_connect_timeout=10,
        )
        await client.ping()
        log.info("outbox nudges connected: %s", settings.redis_url)
    except Exception as exc:  # noqa: BLE001 - the sweep alone is enough
        log.warning("no outbox nudges (%s); falling back to the sweep alone", exc)
        client = None

    try:
        while not stop.is_set():
            try:
                sent = await drain()
                if sent:
                    continue  # more may be waiting; go straight round again
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("sweep failed; continuing")

            if client is not None:
                try:
                    # Blocks until a nudge or the poll interval, whichever comes
                    # first. An idle interval is the normal case, not a fault.
                    await client.blpop(settings.outbox_stream, timeout=settings.worker_poll_seconds)
                    continue
                except RedisTimeout:
                    continue  # nothing arrived; sweep again
                except Exception as exc:  # noqa: BLE001 - a real connection fault
                    log.warning("nudge channel dropped (%s); sweeping instead", exc)
                    client = None
            await asyncio.sleep(settings.worker_poll_seconds)
    finally:
        if client is not None:
            await client.aclose()
        log.info("message worker stopped")
