"""The billing worker's loop.

A timer, not a queue. Billing work is daily-to-hourly and idempotent, so there
is nothing to gain from a broker: the sweep finds whatever is due, and running
it more often than necessary costs a few empty queries. That is the same
reasoning as `messaging/worker.py`'s sweep, minus the Redis nudge — nothing
here needs to happen within seconds.

Two cadences:

*   **every tick** (`BILLING_POLL_SECONDS`) — webhooks, because a payment that
    has been captured should settle its invoice promptly, and invoice emails,
    because an invoice raised a minute ago should go out a minute later.
*   **once a day, per clinic's own midnight** — renewals, the billing sweep,
    the invoice backfill and the gateway reconciliation. Each is idempotent, so
    the day marker is an optimisation rather than a correctness property: a
    restart that loses it costs one extra harmless pass.

Nothing in the loop is allowed to die. An exception in one job is logged and
the next tick runs anyway, because a billing worker that stops is a month of
invoices nobody raised.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from app.core.config import settings
from app.jobs import (
    generate_monthly_invoices,
    process_billing,
    process_razorpay_webhook,
    process_subscription_renewals,
    send_invoice_email,
    sync_razorpay_subscription,
)

log = logging.getLogger("pms.billing.jobs")

#: Run on every tick — the work that should feel immediate.
FAST_JOBS = (process_razorpay_webhook.run, send_invoice_email.run)
#: Run once a day — the work the calendar drives.
DAILY_JOBS = (
    process_subscription_renewals.run,
    generate_monthly_invoices.run,
    process_billing.run,
    sync_razorpay_subscription.run,
)


async def tick(*, daily: bool) -> None:
    """One pass. Every job is isolated: one failing does not skip the rest."""
    jobs = FAST_JOBS + (DAILY_JOBS if daily else ())
    for job in jobs:
        try:
            await job()
        except Exception:  # noqa: BLE001 - a job that throws must not stop billing
            log.exception("billing job %s raised", getattr(job, "__module__", job))


async def run_forever(stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    if not settings.billing_enabled:
        log.warning("billing is disabled; the worker has nothing to do")
        return

    log.info("billing worker started (poll %ss)", settings.billing_poll_seconds)
    last_daily: date | None = None
    try:
        while not stop.is_set():
            # UTC decides when the daily pass runs; each clinic's own date is
            # what the jobs bill against, so a clinic east of UTC is not billed
            # a day early by this marker.
            today = datetime.now(timezone.utc).date()
            daily = last_daily != today
            await tick(daily=daily)
            if daily:
                last_daily = today
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.billing_poll_seconds)
            except asyncio.TimeoutError:
                continue
    finally:
        log.info("billing worker stopped")
