"""Every clinic has every module; role permissions are the gate — step 3 of the stack."""

from __future__ import annotations

import pytest

from tests.conftest import NORTHGATE, ROWAN, patients_of, sign_in

#: One read route per operational module.
MODULE_PATHS = {
    "billing": "/v1/billing/invoices",
    "pharmacy": "/v1/pharmacy/prescriptions",
    "inventory": "/v1/inventory/items",
    "lab": "/v1/lab/orders",
}

CORE_PATHS = ("/v1/patients", "/v1/appointments", "/v1/queue", "/v1/encounters", "/v1/prescriptions")


@pytest.mark.parametrize("module", ["billing", "pharmacy", "inventory", "lab"])
async def test_every_module_answers_for_rowan(rowan, module):
    """Rowan (on trial) used to have the lab and nothing else. Now it has all
    of them: its practice admin holds every permission, so every one answers."""
    response = await rowan.get(MODULE_PATHS[module])
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("module", ["lab", "pharmacy", "billing", "inventory"])
async def test_every_module_answers_for_clearview(clearview, module):
    """Clearview never had the lab or the pharmacy switched on. It does not need to."""
    response = await clearview.get(MODULE_PATHS[module])
    assert response.status_code == 200, response.text


async def test_the_core_answers_for_every_clinic(northgate, clearview, rowan):
    for signed in (northgate, clearview, rowan):
        for path in CORE_PATHS:
            assert (await signed.get(path)).status_code == 200, (signed.host, path)


async def test_a_module_write_works_where_it_used_to_be_switched_off(rowan):
    """Not just reads: Rowan can stock an item now, which inventory refused before."""
    created = await rowan.post(
        "/v1/inventory/items",
        json={
            "sku": "EVERY-MODULE-1", "name": "Every-module test gauze",
            "unit": "pack", "category": "consumable", "reorder_level": 2,
        },
    )
    assert created.status_code == 201, created.text


@pytest.mark.parametrize(
    ("account", "path"),
    [
        ("northgate_reception", "/v1/inventory/items"),
        ("northgate_reception", "/v1/lab/orders"),
        ("northgate_reception", "/v1/pharmacy/prescriptions"),
        ("northgate_pharmacist", "/v1/lab/orders"),
        ("northgate_lab", "/v1/pharmacy/prescriptions"),
    ],
)
async def test_a_role_without_the_permission_is_still_refused(client, account, path):
    """Having the module is not the same as being allowed in it."""
    signed = await sign_in(client, account)
    response = await signed.get(path)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "forbidden"


async def test_another_clinics_module_row_is_not_found(northgate, rowan, clearview):
    """Every clinic reaching every module does not mean reaching each other's
    rows: Rowan asking for one of Northgate's invoices, or Clearview for one of
    Northgate's lab orders, finds nothing — not a 403 that would confirm it."""
    invoices = (await northgate.get("/v1/billing/invoices", params={"limit": 1})).json()["items"]
    assert invoices
    response = await rowan.get(f"/v1/billing/invoices/{invoices[0]['id']}")
    assert response.status_code == 404

    orders = (await northgate.get("/v1/lab/orders", params={"limit": 1})).json()["items"]
    assert orders
    response = await clearview.get(f"/v1/lab/orders/{orders[0]['id']}")
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/tenant/features"),
        ("GET", "/v1/tenant/feature-catalog"),
        ("POST", "/v1/tenant/features/billing/purchase"),
        ("POST", "/v1/tenant/features/billing/deactivate"),
        ("GET", "/v1/catalog/features"),
    ],
)
async def test_the_entitlement_routes_are_gone_from_the_clinic(rowan, method, path):
    response = await (rowan.get(path) if method == "GET" else rowan.post(path, json={}))
    assert response.status_code in (404, 405), (path, response.status_code)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/platform/tenants/{tid}/features"),
        ("PUT", "/v1/platform/tenants/{tid}/features/billing"),
        ("GET", "/v1/platform/catalog/features"),
        ("PATCH", "/v1/platform/catalog/features/billing"),
    ],
)
async def test_the_entitlement_routes_are_gone_from_the_console(console, method, path):
    tenants = (await console.get("/v1/platform/tenants")).json()
    url = path.format(tid=tenants[0]["id"])
    if method == "GET":
        response = await console.get(url)
    elif method == "PUT":
        response = await console.put(url, json={"status": "active"})
    else:
        response = await console.patch(url, json={"price_monthly": 1})
    assert response.status_code in (404, 405), (url, response.status_code)


async def test_no_clinic_route_answers_feature_not_enabled(rowan, clearview):
    """Walk every parameter-free GET a clinic can call: whatever each answers,
    it is never the old entitlement refusal."""
    from app.core import errors
    from app.main import app

    assert not hasattr(errors, "FeatureNotEnabled")

    paths = sorted(
        path
        for path, operations in app.openapi()["paths"].items()
        if "get" in operations
        and path.startswith("/v1/")
        and "{" not in path
        and not path.startswith(("/v1/platform", "/v1/public"))
    )
    assert "/v1/billing/invoices" in paths
    for signed in (rowan, clearview):
        for path in paths:
            response = await signed.get(path)
            assert "feature_not_enabled" not in response.text, (signed.host, path)


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


async def test_setting_the_first_password_completes_an_invitation(client, northgate):
    """An invited row has no hash at all, so the first password is also what
    makes the account real. Without this the invitation is a dead end."""
    created = await northgate.post(
        "/v1/users",
        json={"name": "Marguerite Vane", "email": "m.vane@northgate.clinic", "role": "receptionist"},
    )
    assert created.status_code == 201, created.text
    invited = created.json()
    assert invited["status"] == "invited"

    credentials = {"email": "m.vane@northgate.clinic", "password": "her-first-password"}
    refused = await client.post("/v1/auth/login", headers={"Host": NORTHGATE}, json=credentials)
    assert refused.status_code == 401
    assert "invitation" in refused.json()["error"]["message"].lower()

    activated = await northgate.post(
        f"/v1/users/{invited['id']}/password", json={"password": "her-first-password"}
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == "active"

    assert (
        await client.post("/v1/auth/login", headers={"Host": NORTHGATE}, json=credentials)
    ).status_code == 200


async def test_a_new_password_ends_the_sessions_the_old_one_opened(client, northgate):
    """Otherwise a reset after a suspected theft leaves the thief signed in."""
    created = await northgate.post(
        "/v1/users",
        json={
            "name": "Duncan Reeve", "email": "d.reeve@northgate.clinic",
            "role": "receptionist", "password": "the-first-password",
        },
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]

    theirs = await client.post(
        "/v1/auth/login",
        headers={"Host": NORTHGATE},
        json={"email": "d.reeve@northgate.clinic", "password": "the-first-password"},
    )
    assert theirs.status_code == 200
    held = client.cookies.get("pms_refresh")

    reset = await northgate.post(
        f"/v1/users/{user_id}/password", json={"password": "the-second-password"}
    )
    assert reset.status_code == 200

    client.cookies.set("pms_refresh", held, domain="localhost", path="/")
    stale = await client.post("/v1/auth/refresh", headers={"Host": NORTHGATE})
    assert stale.status_code == 401

    assert (
        await client.post(
            "/v1/auth/login",
            headers={"Host": NORTHGATE},
            json={"email": "d.reeve@northgate.clinic", "password": "the-second-password"},
        )
    ).status_code == 200


async def test_an_admin_setting_their_own_password_stays_signed_in(client, northgate):
    """The one sign-in spared is the browser doing the resetting — otherwise an
    admin would sign themselves out in the act of rotating their own password."""
    created = await northgate.post(
        "/v1/users",
        json={
            "name": "Harriet Blaine", "email": "h.blaine@northgate.clinic",
            "role": "tenant_admin", "password": "the-first-password",
        },
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]

    signed_in = await client.post(
        "/v1/auth/login",
        headers={"Host": NORTHGATE},
        json={"email": "h.blaine@northgate.clinic", "password": "the-first-password"},
    )
    assert signed_in.status_code == 200
    held = client.cookies.get("pms_refresh")

    rotated = await client.post(
        f"/v1/users/{user_id}/password",
        headers={"Host": NORTHGATE, "Authorization": f"Bearer {signed_in.json()['access_token']}"},
        json={"password": "the-second-password"},
    )
    assert rotated.status_code == 200

    client.cookies.set("pms_refresh", held, domain="localhost", path="/")
    assert (await client.post("/v1/auth/refresh", headers={"Host": NORTHGATE})).status_code == 200


async def test_a_password_reset_is_not_an_unlock(client, northgate):
    """Locking is an administrative decision; a reset must not undo it."""
    created = await northgate.post(
        "/v1/users",
        json={
            "name": "Sylvia Doran", "email": "s.doran@northgate.clinic",
            "role": "receptionist", "password": "the-first-password",
        },
    )
    user_id = created.json()["id"]
    await northgate.patch(f"/v1/users/{user_id}/status", json={"status": "locked"})

    reset = await northgate.post(
        f"/v1/users/{user_id}/password", json={"password": "the-second-password"}
    )
    assert reset.status_code == 200
    assert reset.json()["status"] == "locked"

    still_out = await client.post(
        "/v1/auth/login",
        headers={"Host": NORTHGATE},
        json={"email": "s.doran@northgate.clinic", "password": "the-second-password"},
    )
    assert still_out.status_code == 423


async def test_a_short_password_is_refused(northgate):
    users = (await northgate.get("/v1/users")).json()
    someone = next(u for u in users if u["role"] == "receptionist")
    response = await northgate.post(f"/v1/users/{someone['id']}/password", json={"password": "short"})
    assert response.status_code == 422


async def test_only_a_role_that_manages_accounts_may_set_a_password(northgate, northgate_doctor):
    users = (await northgate.get("/v1/users")).json()
    someone = next(u for u in users if u["role"] == "receptionist")
    response = await northgate_doctor.post(
        f"/v1/users/{someone['id']}/password", json={"password": "not-yours-to-set"}
    )
    assert response.status_code == 403


async def test_another_clinics_admin_cannot_reach_the_account(northgate, clearview):
    """Someone else's row is not found, never forbidden — a 403 would confirm it."""
    users = (await northgate.get("/v1/users")).json()
    someone = next(u for u in users if u["role"] == "receptionist")
    response = await clearview.post(
        f"/v1/users/{someone['id']}/password", json={"password": "another-clinics-try"}
    )
    assert response.status_code == 404


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
