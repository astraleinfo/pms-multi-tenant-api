#!/usr/bin/env python
"""Seed the demo dataset.

    python scripts/seed.py            # wipe and re-seed
    python scripts/seed.py --keep     # add to what is already there

Runs as the migration role, because it writes the platform-owned rows (the
catalogues and the shared field definitions) that the application role is not
allowed to touch. Clinic rows are still written with that clinic bound to the
session, so they go through the same policies as a live request.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db import guard  # noqa: E402,F401 — installs the isolation listeners
from app.seed.loader import seed_all  # noqa: E402


async def main(reset: bool) -> None:
    engine = create_async_engine(settings.migration_database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            counts = await seed_all(session, reset=reset)
    await engine.dispose()
    print("seeded:", ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="do not wipe first")
    args = parser.parse_args()
    asyncio.run(main(reset=not args.keep))
