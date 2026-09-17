#!/usr/bin/env python
"""Runs the platform billing worker.

    python scripts/billing_worker.py            # the loop
    python scripts/billing_worker.py <JobName>  # one job, once

Safe to run more than one. Every job is idempotent — cycles, invoices, payments
and webhook deliveries are each protected by a unique index — and webhook
deliveries are claimed with FOR UPDATE SKIP LOCKED, so two workers share the
queue rather than duplicating it.

Running a single job by name is the operator's tool: re-raising a month's
invoices after a failed run, or resending one clinic's invoice email, without
waiting for the loop to come round.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging import configure_logging  # noqa: E402
from app.jobs import JOBS  # noqa: E402
from app.jobs.runner import run_forever  # noqa: E402


async def once(name: str) -> int:
    job = JOBS.get(name)
    if job is None:
        print(f"No such job: {name}\n\nAvailable:\n  " + "\n  ".join(sorted(JOBS)))
        return 2
    result = await job()
    print(result)
    return 0 if result.ok else 1


async def main() -> int:
    configure_logging()
    if len(sys.argv) > 1:
        return await once(sys.argv[1])

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: KeyboardInterrupt below is the stop signal instead.
            pass

    await run_forever(stop)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
