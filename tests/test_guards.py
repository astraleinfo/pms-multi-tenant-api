"""Feature entitlement and role permissions — steps 3 and 4 of the stack."""

from __future__ import annotations

import pytest

from tests.conftest import ROWAN, patients_of, sign_in


async def test_a_module_that_was_never_bought_is_refused(rowan):
    """Rowan is on trial with lab only: billing, pharmacy and inventory are off."""
    for path in ("/v1/billing/invoices", "/v1/pharmacy/prescriptions", "/v1/inventory/items"):
        response = await rowan.get(path)
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "feature_not_enabled"


async def test_a_module_that_was_bought_is_reachable(rowan):
    response = await rowan.get("/v1/lab/orders")
    assert response.status_code == 200


async def test_core_modules_are_never_gated(rowan):
    for path in ("/v1/patients", "/v1/appointments", "/v1/queue", "/v1/encounters", "/v1/prescriptions"):
        assert (await rowan.get(path)).status_code == 200, path


async def test_buying_a_module_opens_it_and_dropping_it_closes_it_again(rowan):
    assert (await rowan.get("/v1/billing/invoices")).status_code == 403

    bought = await rowan.post("/v1/tenant/features/billing/purchase", json={})
    assert bought.status_code == 200
    assert bought.json()["status"] == "active"

    assert (await rowan.get("/v1/billing/invoices")).status_code == 200

    dropped = await rowan.post("/v1/tenant/features/billing/deactivate", json=None)
    assert dropped.status_code == 200
    assert (await rowan.get("/v1/billing/invoices")).status_code == 403


async def test_core_modules_cannot_be_switched_off(rowan):
    response = await rowan.post("/v1/tenant/features/patient/deactivate", json=None)
    assert response.status_code == 409


async def test_entitlement_is_per_clinic(rowan, northgate):
    """Northgate bought pharmacy; Rowan did not. Same code, same route."""
    assert (await northgate.get("/v1/pharmacy/prescriptions")).status_code == 200
    assert (await rowan.get("/v1/pharmacy/prescriptions")).status_code == 403


@pytest.mark.parametrize(
    ("account", "path", "allowed"),
    [
        ("northgate_doctor", "/v1/encounters", True),
        ("northgate_doctor", "/v1/patients", True),
    ],
)
async def test_a_doctor_can_reach_the_clinical_core(client, account, path, allowed):
    signed = await sign_in(client, account)
    assert ((await signed.get(path)).status_code == 200) is allowed


async def test_a_receptionist_cannot_write_a_consultation(client):
    signed = await sign_in(client, "northgate_admin")
    patients = await patients_of(signed)

    reception = await client.post(
        "/v1/auth/login",
        headers={"Host": "northgate.localhost"},
        json={"email": "front@northgate.clinic", "password": "front-2026"},
    )
    token = reception.json()["access_token"]

    response = await client.post(
        "/v1/encounters",
        headers={"Host": "northgate.localhost", "Authorization": f"Bearer {token}"},
        json={"patient_id": patients[0]["id"], "chief_complaint": "Should not be allowed"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_a_pharmacist_cannot_verify_lab_results(client):
    pharmacy = await client.post(
        "/v1/auth/login",
        headers={"Host": "northgate.localhost"},
        json={"email": "pharmacy@northgate.clinic", "password": "pharmacy-2026"},
    )
    token = pharmacy.json()["access_token"]
    orders = await client.get(
        "/v1/lab/orders", headers={"Host": "northgate.localhost", "Authorization": f"Bearer {token}"}
    )
    assert orders.status_code == 403


async def test_an_invited_account_cannot_sign_in(client):
    response = await client.post(
        "/v1/auth/login",
        headers={"Host": "northgate.localhost"},
        json={"email": "owen@northgate.clinic", "password": "owen-2026"},
    )
    assert response.status_code == 401
    assert "invitation" in response.json()["error"]["message"].lower()


async def test_nobody_can_lock_themselves_out(northgate):
    me = (await northgate.get("/v1/auth/me")).json()["user"]
    response = await northgate.patch(f"/v1/users/{me['id']}/status", json={"status": "locked"})
    assert response.status_code == 409


async def test_locking_someone_ends_their_sessions(client, northgate):
    """Their access token still parses; the account behind it no longer works."""
    victim = await client.post(
        "/v1/auth/login",
        headers={"Host": "northgate.localhost"},
        json={"email": "f.munro@northgate.clinic", "password": "fmunro-2026"},
    )
    token = victim.json()["access_token"]
    user_id = victim.json()["user"]["id"]

    assert (
        await client.get(
            "/v1/patients", headers={"Host": "northgate.localhost", "Authorization": f"Bearer {token}"}
        )
    ).status_code == 200

    await northgate.patch(f"/v1/users/{user_id}/status", json={"status": "locked"})

    after = await client.get(
        "/v1/patients", headers={"Host": "northgate.localhost", "Authorization": f"Bearer {token}"}
    )
    assert after.status_code == 401

    await northgate.patch(f"/v1/users/{user_id}/status", json={"status": "active"})


async def test_a_suspended_clinic_stops_answering(console, client, rowan):
    tenants = (await console.get("/v1/platform/tenants")).json()
    target = next(t for t in tenants if t["slug"] == "rowan")

    await console.patch(f"/v1/platform/tenants/{target['id']}/status", json={"status": "suspended"})

    signed_in = await rowan.get("/v1/patients")
    assert signed_in.status_code == 401

    login = await client.post(
        "/v1/auth/login",
        headers={"Host": ROWAN},
        json={"email": "owner@rowanskin.co", "password": "owner-2026"},
    )
    assert login.status_code in (401, 404)

    await console.patch(f"/v1/platform/tenants/{target['id']}/status", json={"status": "trial"})
    assert (await rowan.get("/v1/patients")).status_code == 200
