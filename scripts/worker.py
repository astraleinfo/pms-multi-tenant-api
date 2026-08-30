#!/usr/bin/env python
"""Runs the message outbox worker.

    python scripts/worker.py

Safe to run more than one: messages are claimed with FOR UPDATE SKIP LOCKED,
so two workers never send the same message twice.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging import configure_logging  # noqa: E402
from app.services.messaging.worker import run_forever  # noqa: E402


async def main() -> None:
    configure_logging()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: KeyboardInterrupt below is the stop signal instead.
            pass

    await run_forever(stop)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
