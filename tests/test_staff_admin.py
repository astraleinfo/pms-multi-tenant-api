"""A practice admin editing, removing and releasing colleagues' accounts."""

from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.services import mailer
from tests.conftest import NORTHGATE


@pytest.fixture(autouse=True)
def outbox(monkeypatch) -> list[mailer.Email]:
    sent: list[mailer.Email] = []

    async def capture(email: mailer.Email) -> None:
        sent.append(email)

    monkeypatch.setattr(mailer, "send", capture)
    return sent


async def _new_colleague(admin, *, role="receptionist", password: str | None = None) -> dict:
    tag = uuid.uuid4().hex[:8]
    body = {"name": f"Temp {tag}", "email": f"temp.{tag}@example.com", "role": role}
    if password:
        body["password"] = password
    response = await admin.post("/v1/users", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _staff(admin) -> dict[str, dict]:
    return {u["id"]: u for u in (await admin.get("/v1/users")).json()}


async def test_an_admin_corrects_a_name_role_and_speciality(northgate):
    person = await _new_colleague(northgate)
    response = await northgate.patch(f"/v1/users/{person['id']}", json={
        "name": "Corrected Name", "role": "doctor", "speciality": "Orthodontics",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["name"], body["role"], body["speciality"], body["initials"]) == (
        "Corrected Name", "doctor", "Orthodontics", "CN",
    )
    cleared = await northgate.patch(f"/v1/users/{person['id']}", json={"speciality": ""})
    assert cleared.json()["speciality"] in (None, "")


async def test_a_new_email_must_be_free_and_reinvites_someone_still_invited(northgate, outbox):
    person = await _new_colleague(northgate)
    taken = next(u for u in (await _staff(northgate)).values() if u["id"] != person["id"])
    clash = await northgate.patch(f"/v1/users/{person['id']}", json={"email": taken["email"].upper()})
    assert clash.status_code == 409

    fresh = f"moved.{uuid.uuid4().hex[:8]}@example.com"
    before = len(outbox)
    moved = await northgate.patch(f"/v1/users/{person['id']}", json={"email": fresh})
    assert moved.status_code == 200 and moved.json()["email"] == fresh
    assert [e.to for e in outbox[before:]] == [fresh], "the invitation follows the new address"


async def test_an_admin_cannot_change_their_own_role(northgate):
    me = northgate.user["id"]
    response = await northgate.patch(f"/v1/users/{me}", json={"role": "doctor"})
    assert response.status_code == 409
    assert "your own role" in response.json()["error"]["message"]


async def test_an_account_that_recorded_nothing_can_be_deleted(northgate):
    person = await _new_colleague(northgate)
    response = await northgate.delete(f"/v1/users/{person['id']}")
    assert response.status_code == 204, response.text
    assert person["id"] not in await _staff(northgate)
    assert (await northgate.delete(f"/v1/users/{person['id']}")).status_code == 404


async def test_someone_whose_name_is_on_records_is_locked_not_deleted(northgate, northgate_doctor):
    doctor_id = northgate_doctor.user["id"]
    response = await northgate.delete(f"/v1/users/{doctor_id}")
    assert response.status_code == 409
    error = response.json()["error"]
    assert "Lock the account instead" in error["message"]
    assert error["details"]["records"], "it says what they recorded"
    assert doctor_id in await _staff(northgate)


async def test_an_admin_cannot_delete_themselves(northgate):
    assert (await northgate.delete(f"/v1/users/{northgate.user['id']}")).status_code == 409


async def test_a_doctor_cannot_edit_or_delete_colleagues(northgate, northgate_doctor):
    person = await _new_colleague(northgate)
    assert (await northgate_doctor.patch(f"/v1/users/{person['id']}", json={"name": "Nope"})).status_code == 403
    assert (await northgate_doctor.delete(f"/v1/users/{person['id']}")).status_code == 403
    await northgate.delete(f"/v1/users/{person['id']}")


async def test_wrong_passwords_lock_an_account_and_an_admin_unlocks_it(northgate, client):
    password = "a-long-enough-password-2026"
    person = await _new_colleague(northgate, password=password)

    for _ in range(settings.max_failed_logins):
        await client.post("/v1/auth/login", headers={"Host": NORTHGATE},
                          json={"email": person["email"], "password": "wrong-wrong-wrong"})
    locked = (await _staff(northgate))[person["id"]]
    assert locked["status"] == "locked" and locked["failed_attempts"] >= settings.max_failed_logins

    refused = await client.post("/v1/auth/login", headers={"Host": NORTHGATE},
                                json={"email": person["email"], "password": password})
    assert refused.status_code in (401, 403, 423)

    released = await northgate.patch(f"/v1/users/{person['id']}/status", json={"status": "active"})
    assert released.status_code == 200
    assert (released.json()["status"], released.json()["failed_attempts"]) == ("active", 0)

    back = await client.post("/v1/auth/login", headers={"Host": NORTHGATE},
                             json={"email": person["email"], "password": password})
    assert back.status_code == 200, back.text
