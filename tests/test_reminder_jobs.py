"""Appointment reminders and their Celery jobs.

Future reminders wait as `scheduled` with no job; only today's are in the
broker. The broker itself is the `reminder_broker` fixture — what would have
been published and revoked — and the jobs' own logic is driven directly, the
way a worker would run it.

"Today" is moved with `reminders.now`, and every time here is PMS time —
Asia/Kolkata, which is also Northgate's timezone — so a 10:00 slot has its
reminder due at 10:00 the day before.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from app.core.config import settings
from app.db.session import session_factory
from app.schemas.common import zone
from app.services.messaging import reminders
from tests.conftest import patients_of


def _at(value: str) -> datetime:
    """A wall-clock time on the PMS's clock."""
    return datetime.fromisoformat(value).replace(tzinfo=zone(settings.pms_timezone))


@pytest.fixture
def clock(monkeypatch):
    """Set the reminders' clock: `clock("2029-03-13 08:00")`."""

    def move(value: str) -> None:
        monkeypatch.setattr(reminders, "now", lambda: _at(value))

    return move


async def _enable(signed) -> None:
    response = await signed.patch(
        "/v1/messages/config", json={"provider": "console", "enabled": True, "sender_id": "CLINIC"}
    )
    assert response.status_code == 200, response.text


async def _book(signed, date: str, time: str) -> dict:
    await _enable(signed)
    doctors = (await signed.get("/v1/users/doctors")).json()
    patient = next((p for p in await patients_of(signed, limit=50) if p.get("phone")), None)
    if patient is None:
        pytest.skip("no seeded patient has a phone number")
    response = await signed.post(
        "/v1/appointments",
        json={
            "patient_id": patient["id"], "doctor_id": doctors[-1]["id"],
            "date": date, "time": time, "duration_min": 15,
            "reason": "reminder jobs", "created_via": "front_desk",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _reminder(signed, appointment_id: str) -> dict | None:
    messages = (await signed.get("/v1/messages", params={"limit": 0})).json()["items"]
    return next(
        (
            m for m in messages
            if m["appointment_id"] == appointment_id and m["template"] == "appointment_reminder"
        ),
        None,
    )


# --------------------------------------------------------------- scheduling


async def test_a_future_reminder_stays_scheduled_with_no_job(northgate, clock, reminder_broker):
    clock("2029-03-01 08:00")
    booking = await _book(northgate, "2029-03-20", "10:00")
    reminder = await _reminder(northgate, booking["id"])

    assert reminder["status"] == "scheduled"
    assert reminder["job_id"] is None
    assert reminder_broker.jobs_for(reminder["id"]) == []


async def test_a_reminder_due_today_is_queued_at_once(northgate, clock, reminder_broker):
    clock("2029-03-13 08:00")
    booking = await _book(northgate, "2029-03-14", "10:00")
    reminder = await _reminder(northgate, booking["id"])

    # Due at 10:00 today: straight to the broker, not waiting for midnight.
    assert reminder["status"] == "queued"
    assert reminder["job_id"]
    await reminders.drain()
    [job] = [p for p in reminder_broker.published if p["message_id"] == reminder["id"]]
    assert job["job_id"] == reminder["job_id"]
    assert job["eta"] == _at("2029-03-13 10:00")


async def test_midnight_queues_today_and_leaves_the_future_alone(
    northgate, clock, reminder_broker
):
    clock("2029-03-01 08:00")
    today = await _book(northgate, "2029-03-16", "09:30")
    later = await _book(northgate, "2029-03-17", "09:30")

    clock("2029-03-15 00:00")
    assert await reminders.promote(session_factory) >= 1

    due = await _reminder(northgate, today["id"])
    assert due["status"] == "queued"
    assert reminder_broker.jobs_for(due["id"]) == [due["job_id"]]

    waiting = await _reminder(northgate, later["id"])
    assert waiting["status"] == "scheduled"
    assert waiting["job_id"] is None

    # Running it again finds nothing new to do for this reminder.
    await reminders.promote(session_factory)
    assert reminder_broker.jobs_for(due["id"]) == [due["job_id"]]


# ------------------------------------------------------------- rescheduling


async def test_moving_a_queued_reminder_to_another_day_revokes_its_job(
    northgate, clock, reminder_broker
):
    clock("2029-03-21 08:00")
    booking = await _book(northgate, "2029-03-22", "11:00")
    before = await _reminder(northgate, booking["id"])
    assert before["status"] == "queued"

    moved = await northgate.patch(f"/v1/appointments/{booking['id']}", json={"date": "2029-03-29"})
    assert moved.status_code == 200, moved.text
    await reminders.drain()

    after = await _reminder(northgate, booking["id"])
    assert after["id"] == before["id"]  # the same reminder, moved
    assert after["status"] == "scheduled"
    assert after["job_id"] is None
    assert before["job_id"] in reminder_broker.revoked
    assert "29 Mar 2029" in after["body"]


async def test_moving_a_reminder_onto_today_gives_it_a_new_job(northgate, clock, reminder_broker):
    clock("2029-03-23 08:00")
    booking = await _book(northgate, "2029-03-24", "11:00")
    first = await _reminder(northgate, booking["id"])

    await northgate.patch(f"/v1/appointments/{booking['id']}", json={"time": "15:00"})
    await reminders.drain()

    second = await _reminder(northgate, booking["id"])
    assert second["status"] == "queued"
    assert second["job_id"] and second["job_id"] != first["job_id"]
    assert first["job_id"] in reminder_broker.revoked
    assert reminder_broker.jobs_for(second["id"]) == [first["job_id"], second["job_id"]]


# ------------------------------------------------------ cancelling, deleting


async def test_cancelling_the_appointment_revokes_the_reminder_job(
    northgate, clock, reminder_broker
):
    clock("2029-03-25 08:00")
    booking = await _book(northgate, "2029-03-26", "12:00")
    queued = await _reminder(northgate, booking["id"])

    response = await northgate.patch(
        f"/v1/appointments/{booking['id']}/status", json={"status": "cancelled"}
    )
    assert response.status_code == 200, response.text
    await reminders.drain()

    assert (await _reminder(northgate, booking["id"]))["status"] == "cancelled"
    assert queued["job_id"] in reminder_broker.revoked


async def test_deleting_a_reminder_revokes_its_job_and_removes_it(
    northgate, clock, reminder_broker
):
    clock("2029-03-27 08:00")
    booking = await _book(northgate, "2029-03-28", "12:00")
    queued = await _reminder(northgate, booking["id"])

    response = await northgate.delete(f"/v1/messages/{queued['id']}")
    assert response.status_code == 204, response.text
    await reminders.drain()

    assert await _reminder(northgate, booking["id"]) is None
    assert queued["job_id"] in reminder_broker.revoked


# ------------------------------------------------------------------ the job


async def test_the_job_sends_only_while_the_row_names_it(northgate, clock):
    clock("2029-03-30 08:00")
    booking = await _book(northgate, "2029-03-31", "10:00")
    reminder = await _reminder(northgate, booking["id"])
    tenant_id, message_id = uuid.UUID(northgate.tenant_id), uuid.UUID(reminder["id"])

    # A job the row no longer names (a reschedule, a sweep) stands down...
    stale = await reminders.send(session_factory, tenant_id, message_id, str(uuid.uuid4()))
    assert stale == "superseded"
    assert (await _reminder(northgate, booking["id"]))["status"] == "queued"

    # ...the one it does name sends...
    assert await reminders.send(session_factory, tenant_id, message_id, reminder["job_id"]) == "sent"
    sent = await _reminder(northgate, booking["id"])
    assert sent["status"] == "sent" and sent["sent_at"]

    # ...and a second delivery of that same job finds nothing left to do.
    again = await reminders.send(session_factory, tenant_id, message_id, reminder["job_id"])
    assert again == "skipped"

    # Gone out: it is the record of what the patient got, and cannot be deleted.
    assert (await northgate.delete(f"/v1/messages/{reminder['id']}")).status_code == 409


async def test_the_outbox_loop_leaves_a_job_owned_reminder_alone(northgate, clock, monkeypatch):
    from app.services.messaging import outbox, worker

    clock("2029-04-02 08:00")
    booking = await _book(northgate, "2029-04-03", "10:00")
    reminder = await _reminder(northgate, booking["id"])

    class AfterItWasDue(datetime):
        @classmethod
        def now(cls, tz=None):
            return _at("2029-04-02 11:00")

    # Even an hour after it was due, the outbox loop does not claim it.
    monkeypatch.setattr(outbox, "datetime", AfterItWasDue)
    await worker.drain()
    assert (await _reminder(northgate, booking["id"]))["status"] == "queued"
    assert reminder["job_id"]


async def test_the_sweep_gives_a_lost_job_a_new_one(northgate, clock, reminder_broker):
    clock("2029-04-04 08:00")
    booking = await _book(northgate, "2029-04-05", "10:00")
    first = await _reminder(northgate, booking["id"])

    # Ten minutes after it was due, and still queued: its job is gone.
    clock("2029-04-04 10:10")
    assert await reminders.sweep(session_factory) >= 1

    second = await _reminder(northgate, booking["id"])
    assert second["status"] == "queued"
    assert second["job_id"] != first["job_id"]
    [job] = [p for p in reminder_broker.published if p["job_id"] == second["job_id"]]
    assert job["eta"] is None  # overdue, so it runs now


async def test_the_sweep_rescues_a_reminder_a_missed_midnight_left(northgate, clock):
    clock("2029-04-06 08:00")
    booking = await _book(northgate, "2029-04-08", "10:00")
    assert (await _reminder(northgate, booking["id"]))["status"] == "scheduled"

    # Its day came and the midnight run never did; its time has now passed.
    clock("2029-04-07 10:10")
    await reminders.sweep(session_factory)

    rescued = await _reminder(northgate, booking["id"])
    assert rescued["status"] == "queued" and rescued["job_id"]


async def test_midnight_is_midnight_on_the_pms_clock(clock):
    assert settings.pms_timezone == "Asia/Kolkata"
    clock("2029-04-10 23:59")
    assert reminders.end_of_today() == _at("2029-04-11 00:00")
