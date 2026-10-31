"""Follow-up calls: the call list, what each outcome does to the booking, the
history it leaves, and the availability check in front of a move."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta

from tests.conftest import freeze_clinic_clock, sign_in


async def _today(signed) -> date:
    return date.fromisoformat((await signed.get("/v1/tenant")).json()["today"])


async def _doctors(signed) -> list[dict]:
    return (await signed.get("/v1/users/doctors")).json()


async def _patient(signed, phone: str = "98450 12345") -> dict:
    """A patient of the test's own, with a name nothing else will match."""
    # letters only: a digit run in the search can match another row's phone number
    name = "Follow Up " + "".join(chr(ord("a") + int(c, 16)) for c in uuid.uuid4().hex[:8])
    response = await signed.post(
        "/v1/patients",
        json={"full_name": name, "dob": "1990-02-03", "sex": "female", "phone": phone},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _book(signed, patient: dict, doctor: dict, day: date, time: str, minutes: int = 30) -> dict:
    response = await signed.post(
        "/v1/appointments",
        json={
            "patient_id": patient["id"],
            "doctor_id": doctor["id"],
            "date": day.isoformat(),
            "time": time,
            "duration_min": minutes,
            "reason": "Review",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _row(signed, patient: dict, **params) -> dict | None:
    page = (
        await signed.get("/v1/follow-ups", params={"days": 60, "search": patient["full_name"], **params})
    ).json()
    return page["items"][0] if page["items"] else None


async def _call(signed, appointment: dict, **body):
    return await signed.post(f"/v1/follow-ups/{appointment['id']}/calls", json=body)


async def test_the_list_shows_upcoming_bookings_with_the_number_to_ring(northgate):
    today = await _today(northgate)
    doctor = (await _doctors(northgate))[0]
    patient = await _patient(northgate, phone="90000 11111")
    booking = await _book(northgate, patient, doctor, today + timedelta(days=30), "13:00")

    page = (await northgate.get("/v1/follow-ups", params={"days": 60, "search": patient["full_name"]})).json()
    assert page["total"] == 1
    row = page["items"][0]
    assert row["appointment"]["id"] == booking["id"]
    assert row["patient_phone"] == "9000011111"
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["last_call"] is None
    assert page["counts"]["pending"] == 1
    assert set(page["counts"]) == {
        "pending", "confirmed", "reschedule_required", "no_answer", "declined", "call_back",
    }

    # The search can be a phone number — what someone ringing back gives you.
    by_phone = (await northgate.get("/v1/follow-ups", params={"days": 60, "search": "90000 11111"})).json()
    assert booking["id"] in {r["appointment"]["id"] for r in by_phone["items"]}

    # Outside the window it is not on the list at all.
    near = (await northgate.get("/v1/follow-ups", params={"days": 7, "search": patient["full_name"]})).json()
    assert near["total"] == 0


async def test_a_follow_up_booked_months_out_is_findable(northgate):
    """A consultation's "follow-up in 90 days" becomes a booking that far out.
    The call list has to reach it, a year ahead at most."""
    today = await _today(northgate)
    doctor = (await _doctors(northgate))[0]
    patient = await _patient(northgate)
    response = await northgate.post(
        "/v1/appointments",
        json={
            "patient_id": patient["id"],
            "doctor_id": doctor["id"],
            "date": (today + timedelta(days=120)).isoformat(),
            "time": "13:00",
            "duration_min": 30,
            "reason": "Follow-up",
        },
    )
    assert response.status_code == 201, response.text
    booking = response.json()

    year = (await northgate.get("/v1/follow-ups", params={"days": 365, "search": patient["full_name"]})).json()
    assert [r["appointment"]["id"] for r in year["items"]] == [booking["id"]]
    assert year["items"][0]["appointment"]["reason"] == "Follow-up"

    near = (await northgate.get("/v1/follow-ups", params={"days": 60, "search": patient["full_name"]})).json()
    assert near["total"] == 0

    too_far = await northgate.get("/v1/follow-ups", params={"days": 367})
    assert too_far.status_code == 422


async def test_no_answer_is_logged_and_another_attempt_can_confirm(northgate):
    today = await _today(northgate)
    doctor = (await _doctors(northgate))[0]
    patient = await _patient(northgate)
    booking = await _book(northgate, patient, doctor, today + timedelta(days=31), "13:00")

    first = await _call(northgate, booking, outcome="no_answer", notes="Rang out")
    assert first.status_code == 201, first.text
    row = first.json()
    assert row["status"] == "no_answer"
    assert row["attempts"] == 1
    assert row["appointment"]["status"] == "scheduled"
    assert row["last_call"]["notes"] == "Rang out"
    assert row["last_call"]["called_by_name"] == northgate.user["name"]

    second = await _call(northgate, booking, outcome="confirmed")
    assert second.status_code == 201, second.text
    assert second.json()["status"] == "confirmed"
    assert second.json()["attempts"] == 2
    assert second.json()["appointment"]["status"] == "confirmed"

    history = (await northgate.get(f"/v1/follow-ups/{booking['id']}/calls")).json()
    assert [c["outcome"] for c in history] == ["confirmed", "no_answer"], "newest first"
    assert all(c["called_at"] for c in history)

    assert await _row(northgate, patient, status="confirmed") is not None
    assert await _row(northgate, patient, status="no_answer") is None


async def test_a_later_no_answer_does_not_take_back_a_confirmation(northgate):
    today = await _today(northgate)
    doctor = (await _doctors(northgate))[0]
    patient = await _patient(northgate)
    booking = await _book(northgate, patient, doctor, today + timedelta(days=32), "13:00")

    await _call(northgate, booking, outcome="confirmed")
    row = (await _call(northgate, booking, outcome="no_answer")).json()
    assert row["status"] == "confirmed"
    assert row["appointment"]["status"] == "confirmed"


async def test_rescheduling_on_the_call_moves_the_booking_and_confirms_it(northgate):
    today = await _today(northgate)
    doctor = (await _doctors(northgate))[0]
    day = today + timedelta(days=33)
    patient = await _patient(northgate)
    booking = await _book(northgate, patient, doctor, day, "13:00")
    other = await _book(northgate, await _patient(northgate), doctor, day, "15:00")

    # The slot went while the patient was deciding: refused, and nothing logged.
    taken = await _call(
        northgate, booking, outcome="rescheduled",
        new_slot={"date": day.isoformat(), "time": "15:00"},
    )
    assert taken.status_code == 409, taken.text
    assert (await _row(northgate, patient))["attempts"] == 0

    moved = await _call(
        northgate, booking, outcome="rescheduled", notes="Prefers afternoons",
        new_slot={"date": day.isoformat(), "time": "16:00"},
    )
    assert moved.status_code == 201, moved.text
    row = moved.json()
    assert row["appointment"]["time"] == "16:00"
    assert row["appointment"]["status"] == "confirmed"
    assert row["status"] == "confirmed"
    assert row["last_call"]["outcome"] == "rescheduled"
    assert row["last_call"]["moved_from"] == f"{day.isoformat()} 13:00"
    assert row["last_call"]["moved_to"] == f"{day.isoformat()} 16:00"

    # The old slot is free again; the other booking is untouched.
    assert (await northgate.get(f"/v1/appointments/{other['id']}")).json()["time"] == "15:00"
    free = await northgate.get(
        "/v1/appointments/availability",
        params={"doctor_id": doctor["id"], "date": day.isoformat(), "time": "13:00"},
    )
    assert free.json()["available"] is True


async def test_a_slot_offered_on_the_phone_must_be_ahead_and_inside_the_day(northgate):
    today = await _today(northgate)
    doctor = (await _doctors(northgate))[0]
    patient = await _patient(northgate)
    booking = await _book(northgate, patient, doctor, today + timedelta(days=34), "13:00")

    for slot in (
        {"date": (today - timedelta(days=1)).isoformat(), "time": "13:00"},
        {"date": (today + timedelta(days=34)).isoformat(), "time": "23:00"},
        {"date": (today + timedelta(days=34)).isoformat(), "time": "06:00"},
    ):
        refused = await _call(northgate, booking, outcome="rescheduled", new_slot=slot)
        assert refused.status_code == 422, (slot, refused.text)

    assert (await _row(northgate, patient))["attempts"] == 0


async def test_a_patient_here_today_can_move_to_another_time_today(northgate, monkeypatch):
    """The front-desk case: the patient is visiting today but needs a different
    time. One booking, moved in place — no second appointment, no old slot held."""
    today = await freeze_clinic_clock(monkeypatch, northgate, "08:00")
    doctor = (await _doctors(northgate))[0]
    patient = await _patient(northgate)
    booking = await _book(northgate, patient, doctor, today, "16:05", minutes=5)

    moved = await _call(
        northgate, booking, outcome="rescheduled", notes="Running late",
        new_slot={"date": today.isoformat(), "time": "17:10"},
    )
    assert moved.status_code == 201, moved.text
    row = moved.json()
    assert row["appointment"]["id"] == booking["id"]
    assert row["appointment"]["time"] == "17:10"
    assert row["appointment"]["status"] == "confirmed"
    assert row["status"] == "confirmed"
    assert row["last_call"]["moved_from"] == f"{today.isoformat()} 16:05"
    assert row["last_call"]["moved_to"] == f"{today.isoformat()} 17:10"

    # still the patient's only booking that day, and the old slot is free again
    diary = (await northgate.get("/v1/appointments", params={"date": today.isoformat()})).json()["items"]
    assert [a["id"] for a in diary if a["patient_id"] == patient["id"]] == [booking["id"]]
    free = await northgate.get(
        "/v1/appointments/availability",
        params={"doctor_id": doctor["id"], "date": today.isoformat(), "time": "16:05", "duration_min": 5},
    )
    assert free.json()["available"] is True


async def test_a_same_day_move_onto_a_taken_time_is_refused_and_changes_nothing(northgate, monkeypatch):
    today = await freeze_clinic_clock(monkeypatch, northgate, "08:00")
    doctor = (await _doctors(northgate))[0]
    patient = await _patient(northgate)
    booking = await _book(northgate, patient, doctor, today, "16:10", minutes=5)
    await _book(northgate, await _patient(northgate), doctor, today, "17:20", minutes=5)

    taken = await _call(
        northgate, booking, outcome="rescheduled", new_slot={"date": today.isoformat(), "time": "17:20"},
    )
    assert taken.status_code == 409, taken.text

    row = await _row(northgate, patient)
    assert row["attempts"] == 0
    assert row["appointment"]["time"] == "16:10"
    assert row["appointment"]["status"] == "scheduled"


async def test_a_same_day_move_follows_the_diary_rule_for_a_time_that_has_gone(northgate, monkeypatch):
    """One rule for the desk and the call list: a start earlier than the
    current minute is refused, the current minute is accepted — the same as
    booking it from the diary."""
    today = await freeze_clinic_clock(monkeypatch, northgate, "08:00")
    doctor = (await _doctors(northgate))[0]
    patient = await _patient(northgate)
    booking = await _book(northgate, patient, doctor, today, "16:15", minutes=5)

    await freeze_clinic_clock(monkeypatch, northgate, "17:00")
    gone = await _call(
        northgate, booking, outcome="rescheduled", new_slot={"date": today.isoformat(), "time": "16:30"},
    )
    assert gone.status_code == 422, gone.text
    assert "already passed" in gone.json()["error"]["message"]
    row = await _row(northgate, patient)
    assert row["attempts"] == 0
    assert row["appointment"]["time"] == "16:15"

    # 16:55 began five minutes ago: refused, no grace
    five_ago = await _call(
        northgate, booking, outcome="rescheduled", new_slot={"date": today.isoformat(), "time": "16:55"},
    )
    assert five_ago.status_code == 422, five_ago.text

    # 17:00 is the current minute: accepted
    now_slot = await _call(
        northgate, booking, outcome="rescheduled", new_slot={"date": today.isoformat(), "time": "17:00"},
    )
    assert now_slot.status_code == 201, now_slot.text
    assert now_slot.json()["appointment"]["time"] == "17:00"


async def test_a_booking_that_is_no_longer_open_cannot_be_moved_from_the_call_list(northgate, monkeypatch):
    today = await freeze_clinic_clock(monkeypatch, northgate, "08:00")
    doctor = (await _doctors(northgate))[0]
    slot = {"date": today.isoformat(), "time": "17:35"}

    cancelled = await _book(northgate, await _patient(northgate), doctor, today, "16:20", minutes=5)
    assert (await northgate.patch(f"/v1/appointments/{cancelled['id']}/status", json={"status": "cancelled"})).status_code == 200
    refused = await _call(northgate, cancelled, outcome="rescheduled", new_slot=slot)
    assert refused.status_code == 409, refused.text

    arrived = await _book(northgate, await _patient(northgate), doctor, today, "16:25", minutes=5)
    assert (await northgate.post(f"/v1/appointments/{arrived['id']}/checkin")).status_code == 200
    refused = await _call(northgate, arrived, outcome="rescheduled", new_slot=slot)
    assert refused.status_code == 409, refused.text
    assert (await northgate.get(f"/v1/appointments/{arrived['id']}")).json()["time"] == "16:25"


async def test_the_body_has_to_fit_the_outcome(northgate):
    today = await _today(northgate)
    doctor = (await _doctors(northgate))[0]
    booking = await _book(northgate, await _patient(northgate), doctor, today + timedelta(days=35), "13:00")

    no_slot = await _call(northgate, booking, outcome="rescheduled")
    assert no_slot.status_code == 422
    stray_slot = await _call(
        northgate, booking, outcome="confirmed",
        new_slot={"date": (today + timedelta(days=36)).isoformat(), "time": "13:00"},
    )
    assert stray_slot.status_code == 422
    stray_cancel = await _call(northgate, booking, outcome="no_answer", cancel_appointment=True)
    assert stray_cancel.status_code == 422


async def test_declined_can_let_the_slot_go_and_stays_on_the_list(northgate):
    today = await _today(northgate)
    doctor = (await _doctors(northgate))[0]
    patient = await _patient(northgate)
    booking = await _book(northgate, patient, doctor, today + timedelta(days=37), "13:00")

    response = await _call(northgate, booking, outcome="declined", notes="Moving away", cancel_appointment=True)
    assert response.status_code == 201, response.text
    assert response.json()["appointment"]["status"] == "cancelled"
    assert response.json()["status"] == "declined"

    # Rung about, so still on the morning's list — and finished with.
    assert (await _row(northgate, patient))["status"] == "declined"
    again = await _call(northgate, booking, outcome="confirmed")
    assert again.status_code == 409


async def test_needing_to_reschedule_takes_back_an_earlier_yes(northgate):
    today = await _today(northgate)
    doctor = (await _doctors(northgate))[0]
    patient = await _patient(northgate)
    booking = await _book(northgate, patient, doctor, today + timedelta(days=38), "13:00")

    await _call(northgate, booking, outcome="confirmed")
    row = (await _call(northgate, booking, outcome="reschedule_required", notes="Will ring back")).json()
    assert row["status"] == "reschedule_required"
    assert row["appointment"]["status"] == "scheduled"
    assert await _row(northgate, patient, status="reschedule_required") is not None


async def test_moving_a_confirmed_booking_from_the_diary_reopens_its_follow_up(northgate):
    today = await _today(northgate)
    doctor = (await _doctors(northgate))[0]
    day = today + timedelta(days=39)
    patient = await _patient(northgate)
    booking = await _book(northgate, patient, doctor, day, "13:00")

    await _call(northgate, booking, outcome="confirmed")
    moved = await northgate.patch(f"/v1/appointments/{booking['id']}", json={"time": "14:00"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["status"] == "scheduled"
    assert (await _row(northgate, patient))["status"] == "pending", "they agreed to a slot it is no longer in"


async def test_a_confirmed_booking_checks_in_like_any_other(northgate):
    today = await _today(northgate)
    doctor = (await _doctors(northgate))[0]
    booking = await _book(northgate, await _patient(northgate), doctor, today + timedelta(days=40), "13:00")

    await _call(northgate, booking, outcome="confirmed")
    entry = await northgate.post(f"/v1/appointments/{booking['id']}/checkin")
    assert entry.status_code == 200, entry.text
    assert (await northgate.get(f"/v1/appointments/{booking['id']}")).json()["status"] == "checked_in"


async def test_who_may_use_the_call_list(client, northgate):
    today = await _today(northgate)
    doctor = (await _doctors(northgate))[0]
    booking = await _book(northgate, await _patient(northgate), doctor, today + timedelta(days=41), "13:00")

    reception = await sign_in(client, "northgate_reception")
    assert (await reception.get("/v1/follow-ups")).status_code == 200
    logged = await _call(reception, booking, outcome="call_back", notes="After 5pm")
    assert logged.status_code == 201, logged.text
    assert logged.json()["last_call"]["called_by_name"] == reception.user["name"]

    clinician = await sign_in(client, "northgate_doctor")
    assert (await clinician.get("/v1/follow-ups")).status_code == 200

    pharmacist = await sign_in(client, "northgate_pharmacist")
    assert (await pharmacist.get("/v1/follow-ups")).status_code == 403
    assert (await _call(pharmacist, booking, outcome="confirmed")).status_code == 403


async def test_another_clinic_cannot_see_or_ring_about_a_booking(northgate, clearview):
    today = await _today(northgate)
    doctor = (await _doctors(northgate))[0]
    patient = await _patient(northgate)
    booking = await _book(northgate, patient, doctor, today + timedelta(days=42), "13:00")

    assert (await clearview.get(f"/v1/follow-ups/{booking['id']}/calls")).status_code == 404
    assert (await _call(clearview, booking, outcome="confirmed")).status_code == 404
    # nor move it
    moved = await _call(
        clearview, booking, outcome="rescheduled",
        new_slot={"date": (today + timedelta(days=42)).isoformat(), "time": "14:00"},
    )
    assert moved.status_code == 404
    assert (await northgate.get(f"/v1/appointments/{booking['id']}")).json()["time"] == "13:00"
    assert await _row(clearview, patient) is None
    assert (await _row(northgate, patient))["status"] == "pending"


async def test_two_desks_booking_the_same_slot_at_once_cannot_both_win(northgate):
    """The final check is a read and the booking is a write; the diary lock is
    what stops both requests reading "free" before either has written."""
    today = await _today(northgate)
    doctor = (await _doctors(northgate))[0]
    day = today + timedelta(days=43)
    first, second = await _patient(northgate), await _patient(northgate)

    def booking(patient: dict) -> dict:
        return {
            "patient_id": patient["id"], "doctor_id": doctor["id"], "date": day.isoformat(),
            "time": "13:00", "duration_min": 30, "reason": "Race",
        }

    responses = await asyncio.gather(
        northgate.post("/v1/appointments", json=booking(first)),
        northgate.post("/v1/appointments", json=booking(second)),
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
