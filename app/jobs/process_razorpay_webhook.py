"""ProcessRazorpayWebhookJob — work through the stored deliveries.

The HTTP endpoint stores and answers; this does the acting. Splitting them is
what keeps Razorpay from retrying: a delivery is acknowledged as soon as it is
safely on disk, and whatever it means is worked out afterwards, where a failure
is a retry rather than a 500 back to the gateway.

Deliveries are claimed with `FOR UPDATE SKIP LOCKED`, so two workers can run
without either waiting for the other or both taking the same row. Each one is
then processed in its own transaction — one poisonous payload cannot roll back
a batch.

Despite the name this is not Razorpay-specific: it processes whatever is in
`webhook_events`, whichever gateway wrote it. The name is the one the
requirements asked for.
"""

from __future__ import annotations

import logging

from app.db.session import session_factory
from app.jobs._base import JobResult
from app.models.enums import WebhookStatus
from app.services.payment.webhook import handler as webhook_handler

log = logging.getLogger("pms.billing.jobs")

JOB = "ProcessRazorpayWebhookJob"


async def run(limit: int = 50) -> JobResult:
    result = JobResult(job=JOB)

    while result.processed + result.skipped + len(result.failures) < limit:
        # Claim, then release the lock before doing the work: `process_one`
        # takes its own transaction, and holding a lock across it would
        # serialise the workers we just went to the trouble of not blocking.
        async with session_factory() as session:
            async with session.begin():
                batch = await webhook_handler.claim_pending(session, limit=10)
                event_ids = [event.id for event in batch]
        if not event_ids:
            break

        for event_id in event_ids:
            try:
                status = await webhook_handler.process_one(event_id)
            except Exception as exc:  # noqa: BLE001 - never let the sweep die
                result.failures.append(f"{event_id}: {type(exc).__name__}: {exc}")
                log.exception("webhook %s could not be processed", event_id)
                continue
            if status == WebhookStatus.PROCESSED:
                result.processed += 1
            elif status in (WebhookStatus.IGNORED, WebhookStatus.REJECTED):
                result.skipped += 1
            else:
                result.failures.append(f"{event_id}: left as {status}")

    log.info("%s", result)
    return result
