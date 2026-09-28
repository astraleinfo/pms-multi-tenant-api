"""What a module costs is the platform's to decide, and a super admin's alone.

The catalogue is shared by every clinic, so a price change is one row that
every clinic reads at once — which is why the desk (`support`) can switch a
module on for a clinic but cannot decide what it is charged, and why no clinic
account of any role can reach the route at all.
"""

from __future__ import annotations

import pytest

from tests.conftest import sign_in

URL = "/v1/platform/catalog/features/{key}"


async def _price_of(signed, key: str, *, console: bool) -> float:
    path = "/v1/platform/catalog/features" if console else "/v1/catalog/features"
    rows = (await signed.get(path)).json()
    return next(row["price_monthly"] for row in rows if row["key"] == key)


async def _platform_audit(action: str) -> list[dict]:
    """Platform-level lines name no clinic, so they are read with none bound."""
    from sqlalchemy import select

    from app.db.session import tenant_session
    from app.models.tenancy import AuditEvent

    async with tenant_session(None) as session:
        rows = (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.action == action, AuditEvent.tenant_id.is_(None))
                .order_by(AuditEvent.at.desc())
            )
        ).scalars().all()
        return [{"entity_id": r.entity_id, "actor_label": r.actor_label, "meta": r.meta} for r in rows]


async def test_a_super_admin_sets_a_price_and_every_clinic_reads_it(console, northgate, clearview):
    before = await _price_of(console, "billing", console=True)
    try:
        response = await console.patch(URL.format(key="billing"), json={"price_monthly": 3249.5})
        assert response.status_code == 200, response.text
        assert response.json()["price_monthly"] == 3249.5

        # One row, so every clinic sees the change at once.
        assert await _price_of(console, "billing", console=True) == 3249.5
        assert await _price_of(northgate, "billing", console=False) == 3249.5
        assert await _price_of(clearview, "billing", console=False) == 3249.5

        trail = await _platform_audit("platform.feature_price")
        assert trail[0]["entity_id"] == "billing"
        assert trail[0]["meta"] == {"from": before, "to": 3249.5}
        assert "ines@astrale.health" in trail[0]["actor_label"]
    finally:
        await console.patch(URL.format(key="billing"), json={"price_monthly": before})


async def test_a_module_can_be_made_free(console):
    before = await _price_of(console, "lab", console=True)
    try:
        response = await console.patch(URL.format(key="lab"), json={"price_monthly": 0})
        assert response.status_code == 200
        assert response.json()["price_monthly"] == 0
    finally:
        await console.patch(URL.format(key="lab"), json={"price_monthly": before})


async def test_the_support_desk_cannot_change_a_price(console, console_support):
    before = await _price_of(console, "pharmacy", console=True)

    refused = await console_support.patch(URL.format(key="pharmacy"), json={"price_monthly": 1})
    assert refused.status_code == 403

    # ...but still reads the catalogue it sells from.
    assert await _price_of(console_support, "pharmacy", console=True) == before


@pytest.mark.parametrize("account", ["northgate_admin", "northgate_doctor", "clearview_admin"])
async def test_no_clinic_account_can_change_a_price(client, console, account):
    before = await _price_of(console, "inventory", console=True)
    signed = await sign_in(client, account)

    refused = await signed.patch(URL.format(key="inventory"), json={"price_monthly": 1})
    assert refused.status_code in (401, 403)
    assert await _price_of(console, "inventory", console=True) == before


async def test_nobody_signed_in_is_asked_to_sign_in(client):
    from tests.conftest import CONSOLE

    response = await client.patch(
        URL.format(key="billing"), json={"price_monthly": 1}, headers={"Host": CONSOLE}
    )
    assert response.status_code == 401


async def test_a_core_module_carries_no_price(console):
    response = await console.patch(URL.format(key="patient"), json={"price_monthly": 500})
    assert response.status_code == 409
    assert await _price_of(console, "patient", console=True) == 0


async def test_an_unknown_module_is_not_found(console):
    response = await console.patch(URL.format(key="teleport"), json={"price_monthly": 500})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"price_monthly": -1},
        {"price_monthly": 100_000_000},
        {"price_monthly": "free"},
        {},
        {"price_monthly": 100, "is_core": True},
    ],
)
async def test_a_price_that_is_not_a_price_is_refused(console, body):
    before = await _price_of(console, "billing", console=True)
    response = await console.patch(URL.format(key="billing"), json=body)
    assert response.status_code == 422
    assert await _price_of(console, "billing", console=True) == before


async def test_the_clinic_catalogue_needs_a_clinic_session(client):
    from tests.conftest import NORTHGATE

    response = await client.get("/v1/catalog/features", headers={"Host": NORTHGATE})
    assert response.status_code == 401
