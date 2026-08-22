"""Alembic environment.

Migrations run as the *owner* role (`MIGRATION_DATABASE_URL`), which is not the
role the application uses. That separation is deliberate: the application role
can read and write rows but cannot alter a table or drop a policy.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import settings
from app.db.base import Base
from app.models import *  # noqa: F401,F403 — populate Base.metadata

config = context.config
# Alembic keeps this in a configparser, where `%` starts an interpolation. A
# percent-encoded password (`%40` for `@`) would raise before we ever connect,
# so double it here; configparser turns `%%` back into `%` on the way out.
config.set_main_option("sqlalchemy.url", settings.migration_database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.migration_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
