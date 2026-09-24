"""The platform's own settings — a singleton, super-admin only."""

from __future__ import annotations

import uuid as _uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.session import tenant_session
from app.models.enums import Currency
from app.models.tenancy import PlatformSettings

_PLATFORM_SETTINGS_ID = _uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _reset_platform_settings() -> None:
    async with tenant_session(None) as session:
        row = await session.get(PlatformSettings, _PLATFORM_SETTINGS_ID)
        if row is not None:
            row.support_email = "support@astrale.health"
            row.support_phone = "+91 80 4718 2200"
            row.support_hours = "Monday to Saturday, 9am – 7pm IST"
            row.default_day_start = "09:00"
            row.default_day_end = "17:30"
            row.default_slot_minutes = 30
            row.default_duration = 30
            row.default_tax_pct = 0
            row.default_payment_terms_days = 14
            row.default_expiry_alert_days = 90
            row.default_currency = Currency.INR


@pytest.fixture(autouse=True)
async def reset_platform_settings():
    """`platform_settings` is a real singleton row in the (session-scoped)
    test database — nothing reseeds it between tests, so a PATCH in one test
    would otherwise leak into every test that runs after it. Reset to the
    documented defaults before *and* after each test in this file, the same
    way `reset_rate_limiter` in conftest.py resets its own piece of shared
    state — a teardown-only reset would still leak this file's last test's
    changes into every other test module that runs afterwards."""
    await _reset_platform_settings()
    yield
    await _reset_platform_settings()


async def test_the_settings_row_exists_with_documented_defaults(console):
    response = await console.get("/v1/platform/settings")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["support_email"] == "support@astrale.health"
    assert body["default_currency"] == "INR"
    assert body["default_day_start"] == "09:00"


async def test_only_one_row_can_ever_exist():
    """The check constraint, not application discipline, is what makes this
    a singleton — proved by trying to break it directly at the database."""
    async with tenant_session(None) as session:
        session.add(PlatformSettings(id=_uuid.uuid4()))
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_a_super_admin_can_change_a_setting(console):
    changed = await console.patch("/v1/platform/settings", json={"support_phone": "+91 80 0000 0000"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["support_phone"] == "+91 80 0000 0000"

    read_back = await console.get("/v1/platform/settings")
    assert read_back.json()["support_phone"] == "+91 80 0000 0000"


async def test_the_support_desk_cannot_read_or_write_settings(console_support):
    read = await console_support.get("/v1/platform/settings")
    assert read.status_code == 403

    write = await console_support.patch("/v1/platform/settings", json={"support_phone": "+91 80 1111 1111"})
    assert write.status_code == 403


async def test_settings_need_a_console_session(client):
    from tests.conftest import CONSOLE

    response = await client.get("/v1/platform/settings", headers={"Host": CONSOLE})
    assert response.status_code == 401


async def test_a_bad_currency_is_refused(console):
    response = await console.patch("/v1/platform/settings", json={"default_currency": "XYZ"})
    assert response.status_code == 422


async def test_public_settings_needs_no_session(client, console):
    from tests.conftest import NORTHGATE

    await console.patch("/v1/platform/settings", json={"support_email": "help@astrale.health"})

    response = await client.get("/v1/public/settings", headers={"Host": NORTHGATE})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "support_email": "help@astrale.health",
        "support_phone": "+91 80 4718 2200",
        "support_hours": "Monday to Saturday, 9am – 7pm IST",
    }
    assert "default_currency" not in body, "only the support fields are public"


async def test_a_new_tenants_config_follows_the_platform_default(console):
    import uuid as _uuid

    from app.db.session import tenant_session
    from app.services.tenants import get_config

    await console.patch("/v1/platform/settings", json={"default_day_start": "08:00", "default_currency": "USD"})

    created = await console.post("/v1/platform/tenants", json={
        "name": "Platform Defaults Test Clinic",
        "slug": "platform-defaults-test",
        "clinical_type": "dental",
        "admin_name": "Test Admin",
        "admin_email": "admin@platform-defaults-test.example",
    })
    assert created.status_code == 201, created.text
    tenant_id = _uuid.UUID(created.json()["tenant"]["id"])

    async with tenant_session(tenant_id) as session:
        config = await get_config(session, tenant_id)
        assert config.day_start == "08:00"
        assert config.currency.value == "USD"


async def test_an_earlier_tenant_keeps_its_own_config_after_the_default_changes(console, northgate):
    """northgate already exists (seeded), with day_start "08:30" per
    seed_data.json. Changing the platform default must not retroactively
    touch it."""
    import uuid as _uuid

    from app.db.session import tenant_session
    from app.services.tenants import get_config

    me = (await northgate.get("/v1/auth/me")).json()
    tenant_id = _uuid.UUID(me["tenant_id"])

    await console.patch("/v1/platform/settings", json={"default_day_start": "11:00"})

    async with tenant_session(tenant_id) as session:
        config = await get_config(session, tenant_id)
        assert config.day_start != "11:00"
