"""The queue socket (§3.6).

A socket is a door into a clinic, so it is checked exactly like a request: the
token names a clinic, the address names a clinic, and they have to agree. What
travels over it is a nudge to re-read the queue, never a copy of it.
"""

from __future__ import annotations

import pytest

from app.main import app
from tests.conftest import CLEARVIEW, NORTHGATE, patients_of
from tests.ws import ASGIWebSocket, SocketClosed


def socket(token: str, host: str = NORTHGATE) -> ASGIWebSocket:
    return ASGIWebSocket(app, f"/v1/ws/queue?access_token={token}", {"host": host})


async def test_a_socket_needs_a_real_token(seeded):
    with pytest.raises(SocketClosed) as exc:
        async with socket("nonsense"):
            pass
    assert exc.value.code == 4401


async def test_a_socket_is_refused_at_another_clinics_address(northgate):
    with pytest.raises(SocketClosed) as exc:
        async with socket(northgate.token, host=CLEARVIEW):
            pass
    assert exc.value.code == 4401


async def test_a_socket_opens_and_names_its_clinic(northgate):
    async with socket(northgate.token) as ws:
        hello = await ws.receive_json()
    assert hello["type"] == "hello"
    assert hello["tenant"] == northgate.tenant_id
    assert hello["expires_in"] > 0


async def test_a_walk_in_reaches_the_open_screens(northgate):
    """The point of the whole thing: the desk adds someone, the waiting-room
    screen hears about it without asking."""
    patient = (await patients_of(northgate))[0]
    doctor = (await northgate.get("/v1/users/doctors")).json()[0]

    async with socket(northgate.token) as ws:
        assert (await ws.receive_json())["type"] == "hello"

        created = await northgate.post(
            "/v1/queue/walkin",
            json={"patient_id": patient["id"], "doctor_id": doctor["id"], "reason": "Socket test"},
        )
        assert created.status_code == 201
        entry = created.json()

        event = await ws.receive_json()
        assert event["type"] == "queue.walk_in"
        assert event["entry_id"] == entry["id"]
        assert event["token_no"] == entry["token_no"]

        moved = await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": "called"})
        assert moved.status_code == 200

        event = await ws.receive_json()
        assert event["type"] == "queue.status_changed"
        assert event["status"] == "called"


async def test_signing_a_consultation_announces_the_queue_moving_on(northgate):
    patient = (await patients_of(northgate))[3]
    doctor = (await northgate.get("/v1/users/doctors")).json()[0]

    entry = (
        await northgate.post(
            "/v1/queue/walkin",
            json={"patient_id": patient["id"], "doctor_id": doctor["id"], "reason": "Consult and sign"},
        )
    ).json()
    await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": "called"})
    await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": "in_consultation"})

    async with socket(northgate.token) as ws:
        assert (await ws.receive_json())["type"] == "hello"

        signed = await northgate.post(
            "/v1/encounters",
            json={
                "patient_id": patient["id"],
                "queue_entry_id": entry["id"],
                "doctor_id": doctor["id"],
                "chief_complaint": "Socket announcement",
                "sign": True,
            },
        )
        assert signed.status_code == 201

        event = await ws.receive_json()
        assert event["type"] == "queue.status_changed"
        assert event["status"] == "completed"


async def test_one_clinics_events_never_reach_another(northgate, clearview):
    patient = (await patients_of(northgate))[0]
    doctor = (await northgate.get("/v1/users/doctors")).json()[0]

    async with socket(clearview.token, host=CLEARVIEW) as eavesdropper:
        assert (await eavesdropper.receive_json())["type"] == "hello"

        created = await northgate.post(
            "/v1/queue/walkin",
            json={"patient_id": patient["id"], "doctor_id": doctor["id"], "reason": "Not yours"},
        )
        assert created.status_code == 201

        await eavesdropper.expect_silence()


async def test_the_socket_carries_no_patient_data(northgate):
    """No name, no reason, no clinical detail — clients re-read `GET /v1/queue`,
    which is where the guards are."""
    patient = (await patients_of(northgate))[1]
    doctor = (await northgate.get("/v1/users/doctors")).json()[0]

    async with socket(northgate.token) as ws:
        await ws.receive_json()
        await northgate.post(
            "/v1/queue/walkin",
            json={
                "patient_id": patient["id"],
                "doctor_id": doctor["id"],
                "reason": "Something private",
            },
        )
        event = await ws.receive_json()

    assert set(event) == {"type", "at", "entry_id", "token_no", "status", "queue_date"}
    assert patient["full_name"] not in str(event)
    assert "Something private" not in str(event)


async def test_the_socket_is_gated_on_the_same_permission_as_the_queue(client):
    """Every clinical role may read the queue, so every clinical role may watch
    it — the socket asks for `queue:read` and nothing else."""
    from app.core.permissions import Permission, Role, role_has

    watchers = [r for r in Role if role_has(r.value, Permission.QUEUE_READ)]
    assert len(watchers) == len(list(Role))

    response = await client.post(
        "/v1/auth/login",
        headers={"Host": NORTHGATE},
        json={"email": "pharmacy@northgate.clinic", "password": "pharmacy-2026"},
    )
    async with socket(response.json()["access_token"]) as ws:
        assert (await ws.receive_json())["type"] == "hello"
