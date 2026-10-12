#!/usr/bin/env python
"""Seed the database.

    python scripts/seed.py            # wipe and re-seed the demo dataset
    python scripts/seed.py --keep     # add the demo to what is already there

    python scripts/seed.py --production --email you@example.com --name "Your Name"
                                      # a real deployment: the catalogues and
                                      # one super admin, nothing else

The demo truncates every table first, so it refuses to run with
ENVIRONMENT=production. --production never deletes anything: the catalogues go
into an empty database only, and running it again adds another super admin.
It asks for the password, or reads SUPERADMIN_PASSWORD when there is no
terminal to ask on.

Runs as the migration role, because it writes the platform-owned rows (the
catalogues and the shared field definitions) that the application role is not
allowed to touch. Clinic rows are still written with that clinic bound to the
session, so they go through the same policies as a live request.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db import guard  # noqa: E402,F401 — installs the isolation listeners
from app.seed.loader import seed_all, seed_production  # noqa: E402

#: The same floor the API puts on a password a person chooses.
MIN_PASSWORD_LENGTH = 12


def _password() -> str:
    password = os.environ.get("SUPERADMIN_PASSWORD", "")
    if not password and sys.stdin.isatty():
        password = getpass.getpass("Super admin password: ")
        if getpass.getpass("Again: ") != password:
            sys.exit("The two passwords differ.")
    if not password:
        sys.exit("No password: run it on a terminal, or set SUPERADMIN_PASSWORD.")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"The password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return password


async def main(args: argparse.Namespace) -> None:
    if not settings.migration_database_url:
        sys.exit("MIGRATION_DATABASE_URL is not set: the seed runs as the owner role.")
    if args.production:
        password = _password()
    elif settings.is_production:
        sys.exit("The demo seed wipes every table and refuses ENVIRONMENT=production. "
                 "Use --production.")

    engine = create_async_engine(settings.migration_database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                if args.production:
                    counts = await seed_production(
                        session, name=args.name, email=args.email, password=password
                    )
                else:
                    counts = await seed_all(session, reset=not args.keep)
    except ValueError as exc:
        sys.exit(str(exc))
    finally:
        await engine.dispose()
    print("seeded:", ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="do not wipe first (demo only)")
    parser.add_argument("--production", action="store_true",
                        help="the catalogues and one super admin; no demo data, no wipe")
    parser.add_argument("--email", help="the super admin's email (with --production)")
    parser.add_argument("--name", help="the super admin's full name (with --production)")
    args = parser.parse_args()
    if args.production and not (args.email and args.name):
        parser.error("--production needs --email and --name")
    if args.production and args.keep:
        parser.error("--keep is for the demo; --production never wipes")
    asyncio.run(main(args))
