"""Patient messaging: provider switching, the outbox, and its isolation.

Nothing here reaches a network. The console provider is the default precisely
so a test run can never text a real person, and the Twilio adapter is exercised
against a stubbed transport rather than Twilio itself.
"""

from __future__ import annotations

import pytest

from app.schemas.clinical import to_e164

from tests.conftest import patients_of


async def _enable_console(signed) -> dict:
    response = await signed.patch(
        "/v1/messages/config", json={"provider": "console", "enabled": True, "sender_id": "CLINIC"}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _patient_with_phone(signed) -> dict:
    for patient in await patients_of(signed, limit=50):
        if patient.get("phone"):
            return patient
    pytest.skip("no seeded patient has a phone number")


# --------------------------------------------------------------------- config


async def test_config_starts_disabled_and_lists_providers(northgate):
    body = (await northgate.get("/v1/messages/config")).json()
    assert body["enabled"] is False
    assert body["provider"] == "console"
    assert "twilio" in body["available_providers"]


async def test_switching_provider_is_one_field(northgate):
    await _enable_console(northgate)
    body = (
        await northgate.patch(
            "/v1/messages/config",
            json={
                "provider": "twilio",
                "account_sid": "AC" + "0" * 30,
                "auth_token": "super-secret-token",
                "sender_id": "+15550001111",
            },
        )
    ).json()
    assert body["provider"] == "twilio"
    assert body["auth_token_set"] is True
    # The token is stored, but never handed back in any shape.
    assert "super-secret-token" not in str(body)


async def test_auth_token_is_never_returned(northgate):
    await northgate.patch(
        "/v1/messages/config",
        json={"provider": "twilio", "account_sid": "AC" + "1" * 30,
              "auth_token": "leak-me-if-you-can", "sender_id": "+15550002222"},
    )
    body = (await northgate.get("/v1/messages/config")).json()
    assert "leak-me-if-you-can" not in str(body)
    assert body["auth_token_set"] is True


async def test_enabling_twilio_without_credentials_is_refused(northgate):
    response = await northgate.patch(
        "/v1/messages/config",
        json={"provider": "twilio", "enabled": True, "sender_id": "+15550003333",
              "account_sid": "", "auth_token": ""},
    )
    assert response.status_code == 409


async def test_only_an_admin_can_change_the_provider(northgate_doctor):
    response = await northgate_doctor.patch("/v1/messages/config", json={"provider": "twilio"})
    assert response.status_code == 403


# --------------------------------------------------------------------- sending


async def test_disabled_clinic_queues_nothing(northgate):
    await northgate.patch("/v1/messages/config", json={"enabled": False})
    patient = await _patient_with_phone(northgate)
    response = await northgate.post(
        "/v1/messages", json={"patient_id": patient["id"], "template": "follow_up"}
    )
    assert response.status_code == 409


async def test_follow_up_is_queued_and_listed(northgate):
    await _enable_console(northgate)
    patient = await _patient_with_phone(northgate)

    created = await northgate.post(
        "/v1/messages", json={"patient_id": patient["id"], "template": "follow_up"}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "queued"
    assert body["to_address"] == to_e164(patient["phone"])
    assert patient["full_name"].split(" ")[0] in body["body"]

    listed = (await northgate.get("/v1/messages", params={"patient_id": patient["id"]})).json()
    assert any(m["id"] == body["id"] for m in listed["items"])


async def test_custom_message_needs_a_body(northgate):
    await _enable_console(northgate)
    patient = await _patient_with_phone(northgate)
    response = await northgate.post(
        "/v1/messages", json={"patient_id": patient["id"], "template": "custom", "body": "   "}
    )
    assert response.status_code == 409


async def test_patient_without_a_phone_is_refused(northgate):
    await _enable_console(northgate)
    without = [p for p in await patients_of(northgate, limit=50) if not p.get("phone")]
    if not without:
        pytest.skip("every seeded patient has a phone number")
    response = await northgate.post(
        "/v1/messages", json={"patient_id": without[0]["id"], "template": "follow_up"}
    )
    assert response.status_code == 409


async def test_booking_queues_a_confirmation(northgate):
    await _enable_console(northgate)
    patient = await _patient_with_phone(northgate)
    doctors = (await northgate.get("/v1/users/doctors")).json()

    booking = await northgate.post(
        "/v1/appointments",
        json={
            "patient_id": patient["id"], "doctor_id": doctors[0]["id"],
            "date": "2027-03-04", "time": "11:15", "duration_min": 20,
            "reason": "messaging test", "created_via": "front_desk",
        },
    )
    assert booking.status_code == 201, booking.text

    messages = (await northgate.get("/v1/messages", params={"patient_id": patient["id"]})).json()
    confirmations = [
        m for m in messages["items"]
        if m["template"] == "booking_confirmation" and m["appointment_id"] == booking.json()["id"]
    ]
    assert len(confirmations) == 1
    assert "11:15" in confirmations[0]["body"]


# ------------------------------------------------------------------- isolation


async def test_one_clinic_cannot_read_anothers_messages(northgate, clearview):
    await _enable_console(northgate)
    patient = await _patient_with_phone(northgate)
    created = (
        await northgate.post(
            "/v1/messages", json={"patient_id": patient["id"], "template": "follow_up"}
        )
    ).json()

    theirs = (await clearview.get("/v1/messages")).json()
    assert all(m["id"] != created["id"] for m in theirs["items"])

    assert (await clearview.post(f"/v1/messages/{created['id']}/retry")).status_code == 404


async def test_provider_settings_are_per_clinic(northgate, clearview):
    await northgate.patch(
        "/v1/messages/config",
        json={"provider": "twilio", "account_sid": "AC" + "9" * 30,
              "auth_token": "northgate-only", "sender_id": "+15550009999"},
    )
    theirs = (await clearview.get("/v1/messages/config")).json()
    assert theirs["provider"] == "console"
    assert theirs["auth_token_set"] is False


# ---------------------------------------------------------------------- worker


async def test_worker_delivers_and_respects_tenant_binding(northgate):
    """The worker sweeps per clinic, so row-level security applies to it too."""
    from app.services.messaging.worker import drain

    await _enable_console(northgate)
    patient = await _patient_with_phone(northgate)
    queued = (
        await northgate.post(
            "/v1/messages", json={"patient_id": patient["id"], "template": "follow_up"}
        )
    ).json()

    assert await drain() >= 1

    after = (await northgate.get("/v1/messages", params={"patient_id": patient["id"]})).json()
    sent = next(m for m in after["items"] if m["id"] == queued["id"])
    assert sent["status"] == "sent"
    assert sent["provider"] == "console"
    assert sent["sent_at"]


async def test_a_refused_send_is_parked_not_retried(northgate, monkeypatch):
    """A carrier that says "no" is final: parked on the first attempt rather
    than spun through the retry budget.

    The API refuses to enable a provider with no credentials, so that state is
    unreachable over HTTP — this drives the adapter boundary directly instead
    of reaching for a network the suite must never touch.
    """
    from app.services.messaging import worker
    from app.services.messaging.providers.base import SendRefused

    await _enable_console(northgate)
    patient = await _patient_with_phone(northgate)
    queued = (
        await northgate.post(
            "/v1/messages", json={"patient_id": patient["id"], "template": "follow_up"}
        )
    ).json()

    class Refusing:
        name = "console"

        async def send(self, _outbound):
            raise SendRefused("not a mobile number")

    monkeypatch.setattr(worker, "build_provider", lambda _config: Refusing())
    await worker.drain()

    after = (await northgate.get("/v1/messages", params={"patient_id": patient["id"]})).json()
    parked = next(m for m in after["items"] if m["id"] == queued["id"])
    assert parked["status"] == "failed"
    assert parked["attempts"] == 1  # refused, so no second attempt
    assert "not a mobile number" in parked["last_error"]

    # A failed message can be put back by hand.
    retried = await northgate.post(f"/v1/messages/{queued['id']}/retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "queued"


async def test_a_transient_failure_is_retried_with_backoff(northgate, monkeypatch):
    """Unreachable carrier: deferred, not parked, and not on the first attempt."""
    from app.services.messaging import worker
    from app.services.messaging.providers.base import SendRetryable

    await _enable_console(northgate)
    patient = await _patient_with_phone(northgate)
    queued = (
        await northgate.post(
            "/v1/messages", json={"patient_id": patient["id"], "template": "follow_up"}
        )
    ).json()

    class Flaky:
        name = "console"

        async def send(self, _outbound):
            raise SendRetryable("carrier unreachable")

    monkeypatch.setattr(worker, "build_provider", lambda _config: Flaky())
    await worker.drain()

    after = (await northgate.get("/v1/messages", params={"patient_id": patient["id"]})).json()
    deferred = next(m for m in after["items"] if m["id"] == queued["id"])
    assert deferred["status"] == "queued"  # back in the queue, not parked
    assert deferred["attempts"] == 1


# ------------------------------------------------------------------ reschedule


async def _book(signed, date: str, time: str, reason: str) -> dict:
    doctors = (await signed.get("/v1/users/doctors")).json()
    patient = await _patient_with_phone(signed)
    response = await signed.post(
        "/v1/appointments",
        json={
            "patient_id": patient["id"], "doctor_id": doctors[0]["id"],
            "date": date, "time": time, "duration_min": 20,
            "reason": reason, "created_via": "front_desk",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_booking_queues_no_reminder(northgate):
    await _enable_console(northgate)
    booking = await _book(northgate, "2027-04-08", "09:40", "no reminder")

    messages = (await northgate.get("/v1/messages", params={"limit": 0})).json()["items"]
    mine = [m for m in messages if m["appointment_id"] == booking["id"]]
    assert [m["template"] for m in mine] == ["booking_confirmation"]


async def test_a_reminder_cannot_be_sent_by_hand(northgate):
    await _enable_console(northgate)
    patient = await _patient_with_phone(northgate)
    response = await northgate.post(
        "/v1/messages", json={"patient_id": patient["id"], "template": "appointment_reminder"}
    )
    assert response.status_code == 409


async def test_moving_an_appointment_tells_the_patient_both_slots(northgate):
    await _enable_console(northgate)
    booking = await _book(northgate, "2027-11-09", "09:20", "reschedule notice")

    moved = await northgate.patch(
        f"/v1/appointments/{booking['id']}", json={"date": "2027-11-16", "time": "14:40"}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["date"] == "2027-11-16"
    assert moved.json()["time"] == "14:40"

    messages = (await northgate.get("/v1/messages", params={"limit": 0})).json()["items"]
    notice = next(
        m for m in messages
        if m["appointment_id"] == booking["id"] and m["template"] == "appointment_rescheduled"
    )
    # The message is only useful if it names where it moved from and to.
    assert "09 Nov 2027" in notice["body"] and "09:20" in notice["body"]
    assert "16 Nov 2027" in notice["body"] and "14:40" in notice["body"]


async def test_an_appointment_can_be_moved_twice(northgate):
    """The old uniqueness rule made the second move impossible."""
    await _enable_console(northgate)
    booking = await _book(northgate, "2028-02-07", "11:00", "moved twice")

    first = await northgate.patch(f"/v1/appointments/{booking['id']}", json={"time": "12:00"})
    assert first.status_code == 200, first.text
    second = await northgate.patch(f"/v1/appointments/{booking['id']}", json={"time": "13:00"})
    assert second.status_code == 200, second.text

    messages = (await northgate.get("/v1/messages", params={"limit": 0})).json()["items"]
    notices = [
        m for m in messages
        if m["appointment_id"] == booking["id"] and m["template"] == "appointment_rescheduled"
    ]
    assert len(notices) == 2


async def test_moving_onto_a_taken_slot_is_refused(northgate):
    await _enable_console(northgate)
    doctors = (await northgate.get("/v1/users/doctors")).json()
    # two patients: one patient is only ever booked once a day
    first_patient, second_patient = (await patients_of(northgate, limit=2))[:2]
    common = {"doctor_id": doctors[0]["id"], "duration_min": 20, "created_via": "front_desk"}
    await northgate.post(
        "/v1/appointments",
        json={**common, "patient_id": first_patient["id"], "date": "2028-03-03", "time": "09:00", "reason": "first"},
    )
    second = (
        await northgate.post(
            "/v1/appointments",
            json={**common, "patient_id": second_patient["id"], "date": "2028-03-03", "time": "15:00", "reason": "second"},
        )
    ).json()

    clash = await northgate.patch(f"/v1/appointments/{second['id']}", json={"time": "09:00"})
    assert clash.status_code == 409


async def test_moving_to_the_same_slot_is_refused(northgate):
    await _enable_console(northgate)
    booking = await _book(northgate, "2028-04-11", "10:20", "no-op move")
    same = await northgate.patch(f"/v1/appointments/{booking['id']}", json={"time": "10:20"})
    assert same.status_code == 409


async def test_a_completed_appointment_cannot_be_moved(northgate):
    await _enable_console(northgate)
    booking = await _book(northgate, "2028-05-05", "09:00", "already seen")
    await northgate.patch(f"/v1/appointments/{booking['id']}/status", json={"status": "checked_in"})

    response = await northgate.patch(f"/v1/appointments/{booking['id']}", json={"time": "11:00"})
    assert response.status_code == 409


async def test_cancelling_tells_the_patient(northgate):
    await _enable_console(northgate)
    booking = await _book(northgate, "2028-06-14", "16:00", "cancellation notice")

    await northgate.patch(f"/v1/appointments/{booking['id']}/status", json={"status": "cancelled"})

    messages = (await northgate.get("/v1/messages", params={"limit": 0})).json()["items"]
    notice = next(
        m for m in messages
        if m["appointment_id"] == booking["id"] and m["template"] == "appointment_cancelled"
    )
    assert notice["status"] in ("queued", "sent")
    assert "cancelled" in notice["body"].lower()


async def test_another_clinic_cannot_move_our_appointment(northgate, clearview):
    await _enable_console(northgate)
    booking = await _book(northgate, "2028-07-19", "09:40", "isolation")
    response = await clearview.patch(f"/v1/appointments/{booking['id']}", json={"time": "10:40"})
    assert response.status_code == 404
