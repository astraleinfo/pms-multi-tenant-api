"""Sign-in behaviour: what it reveals, what it counts, and what it rotates."""

from __future__ import annotations

import pytest

from tests.conftest import CLEARVIEW, CONSOLE, NORTHGATE


async def _login(client, host: str, email: str, password: str):
    return await client.post(
        "/v1/auth/login", headers={"Host": host}, json={"email": email, "password": password}
    )


async def test_an_unknown_address_is_not_a_clinic(client):
    response = await _login(client, "nowhere.localhost", "someone@example.com", "whatever-1234")
    assert response.status_code == 404


async def test_an_unknown_email_says_the_same_as_a_wrong_password(client):
    """Otherwise the form is an address-enumeration oracle."""
    unknown = await _login(client, NORTHGATE, "nobody@northgate.clinic", "whatever-1234")
    wrong = await _login(client, NORTHGATE, "admin@northgate.clinic", "not-the-password")

    assert unknown.status_code == 401
    assert wrong.status_code == 401
    assert unknown.json()["error"]["message"].startswith(
        "That email and password do not match an account here."
    )
    assert wrong.json()["error"]["message"].startswith(
        "That email and password do not match an account here."
    )


async def test_a_credential_from_another_clinic_does_not_work_here(client):
    """The same email exists at both clinics; the password belongs to one."""
    response = await _login(client, CLEARVIEW, "admin@northgate.clinic", "admin-2026")
    assert response.status_code == 401


async def test_five_wrong_passwords_lock_the_account(client):
    email = "n.rahman@clearview.eye"

    for _ in range(5):
        await _login(client, CLEARVIEW, email, "wrong-password")

    locked = await _login(client, CLEARVIEW, email, "nrahman-2026")
    assert locked.status_code == 423
    assert "locked" in locked.json()["error"]["message"].lower()


async def test_an_admin_can_unlock_and_the_counter_resets(client, clearview):
    email = "c.whitfield@clearview.eye"
    for _ in range(5):
        await _login(client, CLEARVIEW, email, "wrong-password")

    users = (await clearview.get("/v1/users")).json()
    locked = next(u for u in users if u["email"] == email)
    assert locked["status"] == "locked"

    unlocked = await clearview.patch(f"/v1/users/{locked['id']}/status", json={"status": "active"})
    assert unlocked.status_code == 200
    assert unlocked.json()["failed_attempts"] == 0

    ok = await _login(client, CLEARVIEW, email, "cwhitfield-2026")
    assert ok.status_code == 200


async def test_the_refresh_token_never_appears_in_the_response_body(client):
    response = await _login(client, NORTHGATE, "admin@northgate.clinic", "admin-2026")
    assert response.status_code == 200
    assert "refresh" not in response.text

    cookie = response.headers.get("set-cookie", "")
    assert "pms_refresh=" in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    assert "path=/" in cookie.lower()


async def test_refresh_rotates_and_reuse_kills_the_chain(client):
    first = await _login(client, NORTHGATE, "admin@northgate.clinic", "admin-2026")
    original = client.cookies.get("pms_refresh")
    assert original

    rotated = await client.post("/v1/auth/refresh", headers={"Host": NORTHGATE})
    assert rotated.status_code == 200
    assert rotated.json()["access_token"] != first.json()["access_token"]

    replacement = client.cookies.get("pms_refresh")
    assert replacement != original

    # Replaying the spent token is treated as theft: the whole chain dies.
    client.cookies.clear()
    client.cookies.set("pms_refresh", original, domain="localhost", path="/v1/auth")
    replayed = await client.post("/v1/auth/refresh", headers={"Host": NORTHGATE})
    assert replayed.status_code == 401

    client.cookies.clear()
    client.cookies.set("pms_refresh", replacement, domain="localhost", path="/v1/auth")
    after = await client.post("/v1/auth/refresh", headers={"Host": NORTHGATE})
    assert after.status_code == 401
    client.cookies.clear()


async def test_a_refresh_cookie_is_refused_at_another_clinics_address(client):
    await _login(client, NORTHGATE, "admin@northgate.clinic", "admin-2026")
    response = await client.post("/v1/auth/refresh", headers={"Host": CLEARVIEW})
    assert response.status_code == 401
    client.cookies.clear()


async def test_me_reports_role_and_permissions_and_no_module_list(northgate):
    body = (await northgate.get("/v1/auth/me")).json()
    assert body["role"] == "tenant_admin"
    assert "patient:read" in body["permissions"]
    assert "tenant:subscription:manage" in body["permissions"]
    assert "tenant:feature:manage" not in body["permissions"]
    # every clinic has every module, so there is nothing to list
    assert "features" not in body


async def test_a_platform_token_is_useless_inside_a_clinic(console, client):
    response = await client.get(
        "/v1/patients", headers={"Host": NORTHGATE, "Authorization": f"Bearer {console.token}"}
    )
    assert response.status_code == 401


async def test_a_clinic_token_is_useless_on_the_console(northgate, client):
    response = await client.get(
        "/v1/platform/tenants",
        headers={"Host": CONSOLE, "Authorization": f"Bearer {northgate.token}"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("host", [NORTHGATE, "localhost", "www.localhost"])
async def test_the_console_only_signs_in_on_its_own_address(client, host):
    """`admin.localhost` and nowhere else.

    Not a clinic subdomain, and not the apex either — the apex is the product's
    public face, and a console living there would mean the marketing site and
    the super-admin sign-in share a door.
    """
    response = await client.post(
        "/v1/platform/auth/login",
        headers={"Host": host},
        json={"email": "ines@astrale.health", "password": "ines-2026"},
    )
    assert response.status_code == 404


async def test_a_tampered_token_is_refused(client, northgate):
    head, payload, signature = northgate.token.split(".")
    forged = f"{head}.{payload}.{signature[:-3]}aaa"
    response = await client.get(
        "/v1/patients", headers={"Host": NORTHGATE, "Authorization": f"Bearer {forged}"}
    )
    assert response.status_code == 401


async def test_logging_out_revokes_the_session(client):
    await _login(client, NORTHGATE, "admin@northgate.clinic", "admin-2026")
    out = await client.post("/v1/auth/logout", headers={"Host": NORTHGATE})
    assert out.status_code == 204

    after = await client.post("/v1/auth/refresh", headers={"Host": NORTHGATE})
    assert after.status_code == 401
    client.cookies.clear()


async def test_a_person_keeps_their_own_interface_state(client, northgate):
    """The tour is per person, not per browser — so it travels with the account
    and a shared machine does not swallow someone else's first run."""
    before = (await northgate.get("/v1/auth/me")).json()["preferences"]
    assert before == {"tour_version_seen": None, "tour_dismissed": False}

    saved = await northgate.patch("/v1/auth/me/preferences", json={"tour_version_seen": 1})
    assert saved.status_code == 200
    assert saved.json()["tour_version_seen"] == 1

    # It is there on the next sign-in, on any device.
    again = await _login(client, NORTHGATE, "admin@northgate.clinic", "admin-2026")
    assert again.json()["preferences"]["tour_version_seen"] == 1

    # And it is that person's alone.
    other = await _login(client, NORTHGATE, "front@northgate.clinic", "front-2026")
    assert other.json()["preferences"]["tour_version_seen"] is None

    await northgate.patch("/v1/auth/me/preferences", json={"tour_version_seen": None})


async def test_preferences_take_only_the_keys_they_know(northgate):
    response = await northgate.patch(
        "/v1/auth/me/preferences", json={"tour_dismissed": True, "smuggled": {"anything": "at all"}}
    )
    assert response.status_code == 422, "an unknown key is refused, not quietly stored"

    ok = await northgate.patch("/v1/auth/me/preferences", json={"tour_dismissed": True})
    assert ok.json() == {"tour_version_seen": None, "tour_dismissed": True}
    await northgate.patch("/v1/auth/me/preferences", json={"tour_dismissed": False})


async def test_preferences_belong_to_the_caller_alone(northgate, clearview):
    """There is no user id in the route, so there is nothing to point elsewhere."""
    await northgate.patch("/v1/auth/me/preferences", json={"tour_version_seen": 2})
    theirs = (await clearview.get("/v1/auth/me")).json()["preferences"]
    assert theirs["tour_version_seen"] is None
    await northgate.patch("/v1/auth/me/preferences", json={"tour_version_seen": None})
