"""A booking's start must not be behind the clinic's clock.

The clock is the clinic's, and it is frozen by replacing the one function the
request context reads it through, so none of this depends on the hour the
suite happens to run at.
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import count

import pytest

from app.core.errors import ValidationFailed
from app.services.scheduling import PAST_START_GRACE_MIN, require_start_not_past
from tests.conftest import freeze_clinic_clock

_phones = count(9770091000)


async def _tenant(signed) -> dict:
    return (await signed.get("/v1/tenant")).json()


async def _fresh_patient(signed) -> dict:
    """One patient per booking: a patient is booked once a day, and the
    database is shared across the suite."""
    response = await signed.post(
        "/v1/patients",
        json={"full_name": "Clock Patient", "dob": "1990-02-02", "sex": "female", "phone": str(next(_phones))},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _book(signed, day: date, time: str, *, doctor: dict | None = None):
    doctor = doctor or (await signed.get("/v1/users/doctors")).json()[0]
    patient = await _fresh_patient(signed)
    return await signed.post(
        "/v1/appointments",
        json={
            "patient_id": patient["id"], "doctor_id": doctor["id"], "date": day.isoformat(),
            "time": time, "duration_min": 5, "reason": "Clock",
        },
    )


# --- the check on its own ------------------------------------------------------


def test_the_check_only_looks_at_the_clock_on_today():
    today = date(2026, 10, 30)
    # a later day is fine at any hour; an earlier one never is
    require_start_not_past(day=today + timedelta(days=1), time="00:00", today=today, now="22:00")
    with pytest.raises(ValidationFailed):
        require_start_not_past(day=today - timedelta(days=1), time="23:55", today=today, now="00:05")
    # on today: the start of the current slot and anything later pass
    require_start_not_past(day=today, time="22:00", today=today, now="22:00")
    require_start_not_past(day=today, time="22:30", today=today, now="22:00")
    with pytest.raises(ValidationFailed, match="09:00 has already passed"):
        require_start_not_past(day=today, time="09:00", today=today, now="22:00")


def test_a_start_a_few_minutes_old_is_the_slot_just_begun():
    today = date(2026, 10, 30)
    edge = f"{(22 * 60 - PAST_START_GRACE_MIN) // 60:02d}:{(22 * 60 - PAST_START_GRACE_MIN) % 60:02d}"
    require_start_not_past(day=today, time=edge, today=today, now="22:00")
    with pytest.raises(ValidationFailed):
        require_start_not_past(day=today, time="21:44", today=today, now="22:00")


# --- over the API -------------------------------------------------------------


async def test_a_past_time_today_is_refused_on_booking(northgate, monkeypatch):
    today = await freeze_clinic_clock(monkeypatch, northgate, "22:00")

    response = await _book(northgate, today, "09:00")
    assert response.status_code == 422, response.text
    assert "already passed" in response.json()["error"]["message"]


async def test_a_later_time_today_is_accepted(northgate, monkeypatch):
    today = await freeze_clinic_clock(monkeypatch, northgate, "20:00")

    response = await _book(northgate, today, "21:00")
    assert response.status_code == 201, response.text


async def test_booking_for_now_still_works(northgate, monkeypatch):
    """The walk-in-at-the-desk case: the current time, and a start a few
    minutes old (the slot that has just begun), are both accepted."""
    today = await freeze_clinic_clock(monkeypatch, northgate, "22:10")

    now = await _book(northgate, today, "22:10")
    assert now.status_code == 201, now.text
    just_begun = await _book(northgate, today, "22:00")
    assert just_begun.status_code == 201, just_begun.text
    an_hour_ago = await _book(northgate, today, "21:00")
    assert an_hour_ago.status_code == 422


async def test_moving_an_appointment_into_the_past_is_refused(northgate, monkeypatch):
    today = await freeze_clinic_clock(monkeypatch, northgate, "22:00")
    booked = await _book(northgate, today + timedelta(days=80), "10:00")
    assert booked.status_code == 201, booked.text
    uid = booked.json()["id"]

    earlier_today = await northgate.patch(f"/v1/appointments/{uid}", json={"date": today.isoformat(), "time": "09:00"})
    assert earlier_today.status_code == 422, earlier_today.text

    # the same date, a time already gone, once it is on today
    later = await northgate.patch(f"/v1/appointments/{uid}", json={"date": today.isoformat(), "time": "22:30"})
    assert later.status_code == 200, later.text
    back = await northgate.patch(f"/v1/appointments/{uid}", json={"time": "08:00"})
    assert back.status_code == 422, back.text


async def test_a_past_appointment_can_still_change_status(northgate, monkeypatch):
    """Booked at 05:00 for 06:10, and by evening it is history that still has to
    be closed out: the clock only guards the start, never an edit that leaves it."""
    today = await freeze_clinic_clock(monkeypatch, northgate, "05:00")
    booked = await _book(northgate, today, "06:10")
    assert booked.status_code == 201, booked.text
    uid = booked.json()["id"]

    await freeze_clinic_clock(monkeypatch, northgate, "22:00")
    confirmed = await northgate.patch(f"/v1/appointments/{uid}/status", json={"status": "confirmed"})
    assert confirmed.status_code == 200, confirmed.text
    checked_in = await northgate.post(f"/v1/appointments/{uid}/checkin")
    assert checked_in.status_code == 200, checked_in.text
    done = await northgate.patch(f"/v1/appointments/{uid}/status", json={"status": "completed"})
    assert done.status_code == 200, done.text
