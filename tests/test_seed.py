"""The production seed: the catalogues and one super admin, never the demo.

Each test runs as the owner role inside a transaction it rolls back, so the
shared seeded database is the same afterwards. TRUNCATE is transactional in
PostgreSQL, which is what lets one of them start from an empty database.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.security import verify_password
from app.models.enums import PlatformRole
from app.models.identity import PlatformUser, User
from app.models.tenancy import ClinicalType, LabTest, Tenant
from app.seed.loader import seed_production, wipe
from tests.conftest import ROOT

PASSWORD = "a-long-enough-password"


@pytest.fixture
async def owner_session(seeded):
    engine = create_async_engine(settings.migration_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await session.begin()
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


async def _count(session, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


async def test_an_empty_database_gets_the_catalogues_and_one_super_admin(owner_session):
    await wipe(owner_session)

    counts = await seed_production(
        owner_session, name="Asha Rao", email="Asha@Example.com", password=PASSWORD
    )

    assert counts == {"catalogues": 1, "super_admins": 1}
    assert await _count(owner_session, ClinicalType) > 0
    assert await _count(owner_session, LabTest) > 0
    # Nothing from the demo.
    assert await _count(owner_session, Tenant) == 0
    assert await _count(owner_session, User) == 0
    admin = (await owner_session.execute(select(PlatformUser))).scalar_one()
    assert admin.email == "asha@example.com"
    assert admin.role == PlatformRole.SUPER_ADMIN.value
    assert admin.initials == "AR"
    assert verify_password(PASSWORD, admin.password_hash)


async def test_a_second_run_leaves_the_catalogues_alone(owner_session):
    before = await _count(owner_session, LabTest)

    counts = await seed_production(
        owner_session, name="Second Admin", email="second@example.com", password=PASSWORD
    )

    assert counts["catalogues"] == 0
    assert await _count(owner_session, LabTest) == before


async def test_an_existing_email_is_refused(owner_session):
    await seed_production(owner_session, name="Asha Rao", email="asha@example.com", password=PASSWORD)

    with pytest.raises(ValueError, match="already exists"):
        await seed_production(
            owner_session, name="Someone Else", email="ASHA@example.com", password=PASSWORD
        )


def test_the_demo_seed_refuses_production():
    """It truncates every table first; on a live database that is everything."""
    result = subprocess.run(
        [sys.executable, "scripts/seed.py"],
        cwd=ROOT, env={**os.environ, "ENVIRONMENT": "production"},
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    assert result.returncode != 0
    assert "--production" in result.stderr
