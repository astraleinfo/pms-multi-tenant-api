"""The platform's own settings — a singleton, super-admin only."""

from __future__ import annotations

import uuid as _uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.session import tenant_session
from app.models.tenancy import PlatformSettings


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
