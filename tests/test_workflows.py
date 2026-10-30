"""The core loop end to end, and the state machines that hold it together."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from tests.conftest import freeze_clinic_clock, patients_of, patients_off_the_board, sign_in

#: Every clinic has every module, so every dashboard carries every panel.
ALL_PANELS = {"pharmacy", "lab", "inventory", "billing", "advanced_reports"}


async def _doctor(signed) -> dict:
    return (await signed.get("/v1/users/doctors")).json()[0]


async def _today(signed) -> date:
    return date.fromisoformat((await signed.get("/v1/tenant")).json()["today"])


async def test_register_book_check_in_consult_prescribe_dispense_bill(client, northgate):
    doctor = await _doctor(northgate)

    # 3.4 — registration. The MRN comes from the clinic's own sequence.
    patient = (
        await northgate.post(
            "/v1/patients",
            json={
                "full_name": "Whole Loop Patient",
                "dob": "1988-04-11",
                "sex": "female",
                "phone": "9770090055",
                "clinical_ext": {"bruxism": True, "ortho_history": "None"},
            },
        )
    ).json()
    assert patient["mrn"].startswith("NG-")
    assert patient["clinical_ext"]["bruxism"] is True

    # 3.5 — booking, with the availability check behind it.
    day = (await _today(northgate) + timedelta(days=60)).isoformat()
    booking = {
        "patient_id": patient["id"],
        "doctor_id": doctor["id"],
        "date": day,
        "time": "10:00",
        "duration_min": 30,
        "reason": "New patient assessment",
    }
    appointment = (await northgate.post("/v1/appointments", json=booking)).json()
    assert appointment["status"] == "scheduled"

    clash = await northgate.post("/v1/appointments", json={**booking, "time": "10:15"})
    assert clash.status_code == 409, "an overlapping booking should be refused"

    availability = (
        await northgate.get(
            "/v1/appointments/availability",
            params={
                "doctor_id": doctor["id"],
                "date": day,
                "time": "10:15",
                "duration_min": 30,
            },
        )
    ).json()
    assert availability["available"] is False
    assert availability["conflicts"][0]["id"] == appointment["id"]

    # 3.6 — check-in issues a token from this clinic's daily sequence.
    entry = (await northgate.post(f"/v1/appointments/{appointment['id']}/checkin")).json()
    assert entry["status"] == "waiting"
    assert entry["token_no"] >= 1

    again = await northgate.post(f"/v1/appointments/{appointment['id']}/checkin")
    assert again.status_code == 409

    for target in ("called", "in_consultation"):
        moved = await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": target})
        assert moved.status_code == 200, moved.text

    # 3.7 — the consultation. Signing it completes the queue entry.
    doctor_client = await sign_in(client, "northgate_doctor")
    encounter = (
        await northgate.post(
            "/v1/encounters",
            json={
                "patient_id": patient["id"],
                "queue_entry_id": entry["id"],
                "doctor_id": doctor["id"],
                "chief_complaint": "Sensitivity, lower right",
                "notes": "Enamel wear noted.",
                "diagnosis": "Dentine hypersensitivity",
                "follow_up_days": 90,
                "clinical_ext": {
                    "pain_scale": 4,
                    "oral_hygiene": "Good",
                    "tooth_chart": {"46": "watch"},
                    "not_a_real_field": "should be dropped",
                },
                "sign": True,
            },
        )
    ).json()
    assert encounter["status"] == "signed"
    assert encounter["clinical_ext"]["tooth_chart"] == {"46": "watch"}
    assert "not_a_real_field" not in encounter["clinical_ext"], "unknown keys are dropped"

    queue_after = (await northgate.get(f"/v1/queue/{entry['id']}")).json()
    assert queue_after["status"] == "completed"

    corrected = await northgate.patch(f"/v1/encounters/{encounter['id']}", json={"notes": "Enamel wear, LR quadrant."})
    assert corrected.status_code == 200, "signed but not yet billed: still open to correction"
    assert corrected.json()["status"] == "signed"

    # 3.8 — prescribing is core.
    items = (await northgate.get("/v1/inventory/items")).json()
    stock_item = next(i for i in items if i["name"].startswith("Ibuprofen"))
    assert "manufacturer" in stock_item
    assert "description" in stock_item
    assert "hsn_code" in stock_item
    prescription = (
        await doctor_client.post(
            f"/v1/encounters/{encounter['id']}/prescriptions",
            json={
                "items": [
                    {
                        "medicine": "Ibuprofen",
                        "strength": "400 mg",
                        "dosage": "1 tablet",
                        "frequency": "Three times daily",
                        "duration": "3 days",
                        "qty": 9,
                        "item_id": stock_item["id"],
                    }
                ]
            },
        )
    ).json()
    assert prescription["status"] == "pending"
    assert prescription["items"][0]["dispensed_qty"] == 0

    # 3.9 and 3.11 — dispensing draws from the one ledger.
    levels_before = {r["item_id"]: r["on_hand"] for r in (await northgate.get("/v1/inventory/levels")).json()}
    batches = (await northgate.get("/v1/inventory/batches")).json()
    batch = next(b for b in batches if b["item_id"] == stock_item["id"] and b["qty"] > 0)

    dispensed = (
        await northgate.post(
            f"/v1/pharmacy/prescriptions/{prescription['id']}/dispense",
            json={"lines": [{"item_id": prescription["items"][0]["id"], "qty": 9, "batch_no": batch["batch_no"]}]},
        )
    ).json()
    assert dispensed["status"] == "dispensed"
    assert dispensed["items"][0]["dispensed_qty"] == 9

    rewrite = await northgate.put(
        f"/v1/encounters/{encounter['id']}/prescription", json={"items": [{"medicine": "Something else"}]}
    )
    assert rewrite.status_code == 409, "once stock has been handed over the prescription stays as it is"

    levels_after = {r["item_id"]: r["on_hand"] for r in (await northgate.get("/v1/inventory/levels")).json()}
    assert levels_after[stock_item["id"]] == levels_before[stock_item["id"]] - 9

    too_much = await northgate.post(
        f"/v1/pharmacy/prescriptions/{prescription['id']}/dispense",
        json={"lines": [{"item_id": prescription["items"][0]["id"], "qty": 1, "batch_no": batch["batch_no"]}]},
    )
    assert too_much.status_code == 409, "nothing is left to dispense"

    # 3.12 — the invoice, composed from the visit.
    suggestions = (
        await northgate.get("/v1/billing/invoices/suggestions", params={"encounter_id": encounter["id"]})
    ).json()["lines"]
    assert any(line["source"] == "consultation" for line in suggestions)
    assert any(line["source"] == "pharmacy" for line in suggestions)

    invoice = (
        await northgate.post(
            "/v1/billing/invoices",
            json={
                "patient_id": patient["id"],
                "encounter_id": encounter["id"],
                "line_items": [
                    {"description": "Consultation", "source": "consultation", "qty": 1, "unit_price": 45},
                    {"description": "Ibuprofen 400 mg", "source": "pharmacy", "qty": 9, "unit_price": 0.2},
                ],
                "issue": True,
            },
        )
    ).json()
    assert invoice["status"] == "issued"
    assert invoice["totals"]["total"] == 46.8

    issued = await northgate.patch(f"/v1/encounters/{encounter['id']}", json={"notes": "corrected after issue"})
    assert issued.status_code == 200, "an unpaid bill leaves the consultation open"

    part = (
        await northgate.post(
            f"/v1/billing/invoices/{invoice['id']}/payments",
            json={"amount": 20, "method": "card", "reference": "CARD-TEST"},
        )
    ).json()
    assert part["status"] == "partially_paid"
    assert part["totals"]["balance"] == 26.8

    overpay = await northgate.post(
        f"/v1/billing/invoices/{invoice['id']}/payments", json={"amount": 100, "method": "cash"}
    )
    assert overpay.status_code == 409

    settled = (
        await northgate.post(
            f"/v1/billing/invoices/{invoice['id']}/payments", json={"amount": 26.8, "method": "cash"}
        )
    ).json()
    assert settled["status"] == "paid"
    assert settled["totals"]["balance"] == 0

    frozen = await northgate.patch(f"/v1/encounters/{encounter['id']}", json={"notes": "changed"})
    assert frozen.status_code == 409, "a paid consultation is not editable"

    # 3.13 — the history composition sees all of it, in one place.
    history = (await northgate.get(f"/v1/patients/{patient['id']}/history")).json()
    kinds = {event["kind"] for event in history}
    assert {"registration", "appointment", "queue", "encounter", "prescription", "invoice", "payment"} <= kinds


async def test_an_appointment_cannot_be_booked_for_a_past_day(northgate):
    doctor = await _doctor(northgate)
    patient = (await patients_of(northgate))[4]
    today = await _today(northgate)
    booking = {"patient_id": patient["id"], "doctor_id": doctor["id"], "time": "11:00", "reason": "Past"}

    past = await northgate.post("/v1/appointments", json={**booking, "date": (today - timedelta(days=1)).isoformat()})
    assert past.status_code == 422

    ahead = await northgate.post("/v1/appointments", json={**booking, "date": (today + timedelta(days=70)).isoformat()})
    assert ahead.status_code == 201, ahead.text
    moved_back = await northgate.patch(
        f"/v1/appointments/{ahead.json()['id']}", json={"date": (today - timedelta(days=2)).isoformat()}
    )
    assert moved_back.status_code == 422


async def test_a_patient_is_booked_once_a_day(northgate):
    doctors = (await northgate.get("/v1/users/doctors")).json()
    patient = (await patients_of(northgate))[5]
    day = await _today(northgate) + timedelta(days=71)
    booking = {"patient_id": patient["id"], "date": day.isoformat(), "reason": "Twice"}

    first = await northgate.post("/v1/appointments", json={**booking, "doctor_id": doctors[0]["id"], "time": "09:00"})
    assert first.status_code == 201, first.text

    # another time, and another clinician, are still the same patient on the same day
    again = await northgate.post("/v1/appointments", json={**booking, "doctor_id": doctors[-1]["id"], "time": "15:00"})
    assert again.status_code == 409

    other_day = await northgate.post(
        "/v1/appointments",
        json={**booking, "doctor_id": doctors[0]["id"], "time": "15:00", "date": (day + timedelta(days=1)).isoformat()},
    )
    assert other_day.status_code == 201
    onto_the_first = await northgate.patch(f"/v1/appointments/{other_day.json()['id']}", json={"date": day.isoformat()})
    assert onto_the_first.status_code == 409

    # a cancelled booking does not hold the day
    await northgate.patch(f"/v1/appointments/{first.json()['id']}/status", json={"status": "cancelled"})
    rebooked = await northgate.post("/v1/appointments", json={**booking, "doctor_id": doctors[0]["id"], "time": "10:00"})
    assert rebooked.status_code == 201, rebooked.text


async def test_a_walk_in_needs_no_appointment(northgate):
    doctor = await _doctor(northgate)
    patient = (await patients_off_the_board(northgate))[0]

    entry = (
        await northgate.post(
            "/v1/queue/walkin",
            json={
                "patient_id": patient["id"],
                "doctor_id": doctor["id"],
                "reason": "Broken filling",
                "priority": "urgent",
            },
        )
    ).json()
    assert entry["appointment_id"] is None
    assert entry["priority"] == "urgent"

    display = (await northgate.get("/v1/queue/display")).json()
    assert any(row["token_no"] == entry["token_no"] for row in display["waiting"])

    shown = next(row for row in display["waiting"] if row["token_no"] == entry["token_no"])
    assert shown["display_name"].endswith(".") or " " not in shown["display_name"], (
        "the public screen shows a first name and an initial, not a full name"
    )
    assert patient["full_name"] not in str(display)


async def test_a_patient_already_in_the_queue_cannot_be_walked_in_again(northgate):
    doctor = await _doctor(northgate)
    patient = (await patients_off_the_board(northgate))[0]
    body = {"patient_id": patient["id"], "doctor_id": doctor["id"], "reason": "Twice"}

    first = await northgate.post("/v1/queue/walkin", json=body)
    assert first.status_code == 201, first.text

    again = await northgate.post("/v1/queue/walkin", json=body)
    assert again.status_code == 409

    # still true once they have been called — they are in the building
    await northgate.patch(f"/v1/queue/{first.json()['id']}/status", json={"status": "called"})
    assert (await northgate.post("/v1/queue/walkin", json=body)).status_code == 409

    # once seen (or gone) they may come back the same day
    await northgate.patch(f"/v1/queue/{first.json()['id']}/status", json={"status": "in_consultation"})
    await northgate.patch(f"/v1/queue/{first.json()['id']}/status", json={"status": "completed"})
    assert (await northgate.post("/v1/queue/walkin", json=body)).status_code == 201


async def test_tokens_are_per_clinic_per_day(northgate, clearview):
    """Two clinics issuing tokens on the same day do not collide."""
    ours = [q["token_no"] for q in (await northgate.get("/v1/queue")).json()]
    theirs = [q["token_no"] for q in (await clearview.get("/v1/queue")).json()]
    assert ours and theirs
    assert min(ours) == 1 and min(theirs) == 1


async def test_the_queue_state_machine_refuses_impossible_moves(northgate):
    doctor = await _doctor(northgate)
    patient = (await patients_off_the_board(northgate))[0]
    entry = (
        await northgate.post(
            "/v1/queue/walkin",
            json={"patient_id": patient["id"], "doctor_id": doctor["id"], "reason": "State machine"},
        )
    ).json()

    straight_to_completed = await northgate.patch(
        f"/v1/queue/{entry['id']}/status", json={"status": "completed"}
    )
    assert straight_to_completed.status_code == 409

    await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": "called"})
    await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": "in_consultation"})
    done = await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": "completed"})
    assert done.status_code == 200

    reopened = await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": "waiting"})
    assert reopened.status_code == 409


async def test_a_doctor_consults_one_patient_at_a_time(northgate):
    """Starting a second consultation while the first is still open is refused;
    calling the next patient is not — the waiting room may be told to come in."""
    doctors = (await northgate.get("/v1/users/doctors")).json()
    doctor = doctors[0]
    first_patient, second_patient = (await patients_off_the_board(northgate))[:2]

    async def walk_in(patient: dict) -> dict:
        response = await northgate.post(
            "/v1/queue/walkin",
            json={"patient_id": patient["id"], "doctor_id": doctor["id"], "reason": "One at a time"},
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def move(entry: dict, status: str):
        return await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": status})

    first, second = await walk_in(first_patient), await walk_in(second_patient)

    assert (await move(first, "called")).status_code == 200
    started = await move(first, "in_consultation")
    assert started.status_code == 200, started.text

    # calling the next one in is fine...
    assert (await move(second, "called")).status_code == 200
    # ...but starting them is not, and the refusal names who is still in the room
    refused = await move(second, "in_consultation")
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert first_patient["full_name"] in error["message"]
    assert f"token {first['token_no']}" in error["message"]
    assert error["details"]["queue_entry_id"] == first["id"]
    assert (await northgate.get(f"/v1/queue/{second['id']}")).json()["status"] == "called"

    # repeating the move on the one in the room is still a no-op, not a clash
    assert (await move(first, "in_consultation")).status_code == 200

    # once the first is done the second can start
    assert (await move(first, "completed")).status_code == 200
    assert (await move(second, "in_consultation")).status_code == 200
    assert (await move(second, "completed")).status_code == 200


async def test_another_doctor_is_not_held_up_by_a_colleagues_consultation(northgate):
    doctors = (await northgate.get("/v1/users/doctors")).json()
    assert len(doctors) >= 2
    today = (await northgate.get("/v1/tenant")).json()["today"]
    queue = (await northgate.get("/v1/queue", params={"date": today})).json()
    colleague = doctors[-1]
    assert any(
        q["doctor_id"] == colleague["id"] and q["status"] == "in_consultation" for q in queue
    ), "the seed has a colleague with someone in the room"

    patient = (await patients_off_the_board(northgate))[0]
    entry = (
        await northgate.post(
            "/v1/queue/walkin",
            json={"patient_id": patient["id"], "doctor_id": doctors[0]["id"], "reason": "In parallel"},
        )
    ).json()
    await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": "called"})
    started = await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": "in_consultation"})
    assert started.status_code == 200, started.text
    assert (await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": "completed"})).status_code == 200


async def test_signing_closes_the_token_even_when_the_encounter_was_opened_without_it(northgate):
    """A consultation started from "New encounter" or the patient record carries
    no queue link. Signing it must still release the patient's token, or they sit
    "in consultation" after being seen and billed."""
    doctor = await _doctor(northgate)
    patients = await patients_off_the_board(northgate)

    async def in_the_room(patient: dict) -> dict:
        entry = (
            await northgate.post(
                "/v1/queue/walkin",
                json={"patient_id": patient["id"], "doctor_id": doctor["id"], "reason": "Unlinked"},
            )
        ).json()
        await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": "called"})
        await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": "in_consultation"})
        return entry

    # signed on creation
    entry = await in_the_room(patients[2])
    encounter = (
        await northgate.post(
            "/v1/encounters",
            json={"patient_id": patients[2]["id"], "chief_complaint": "Unlinked", "sign": True},
        )
    ).json()
    assert encounter["queue_entry_id"] == entry["id"]
    assert (await northgate.get(f"/v1/queue/{entry['id']}")).json()["status"] == "completed"

    # saved as a draft, signed afterwards
    entry = await in_the_room(patients[3])
    draft = (
        await northgate.post(
            "/v1/encounters",
            json={"patient_id": patients[3]["id"], "chief_complaint": "Unlinked draft"},
        )
    ).json()
    assert (await northgate.get(f"/v1/queue/{entry['id']}")).json()["status"] == "in_consultation"
    signed = (await northgate.patch(f"/v1/encounters/{draft['id']}", json={"sign": True})).json()
    assert signed["status"] == "signed"
    assert (await northgate.get(f"/v1/queue/{entry['id']}")).json()["status"] == "completed"


async def test_signing_from_the_queue_takes_the_patient_off_the_board(northgate):
    """What the consultation screen sends when "Record" is pressed on a token:
    the token rides along, and signing — at once, or after a draft — completes
    it, whether the doctor pressed "Start" first or went straight from "called"."""
    doctor = await _doctor(northgate)
    today = (await _today(northgate)).isoformat()

    # the seeded register is shared and runs short of patients off the board,
    # so this test registers its own
    async def register(n: int) -> dict:
        response = await northgate.post(
            "/v1/patients",
            json={"full_name": f"Queue Sign {n} {uuid.uuid4().hex[:6]}", "dob": "1984-02-03",
                  "sex": "female", "phone": "9770090077"},
        )
        assert response.status_code == 201, response.text
        return response.json()

    patients = {n: await register(n) for n in (4, 5, 6)}

    async def on_the_board(patient: dict, *, started: bool) -> dict:
        entry = (
            await northgate.post(
                "/v1/queue/walkin",
                json={"patient_id": patient["id"], "doctor_id": doctor["id"], "reason": "From the queue"},
            )
        ).json()
        await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": "called"})
        if started:
            await northgate.patch(f"/v1/queue/{entry['id']}/status", json={"status": "in_consultation"})
        return entry

    def form(patient: dict, entry: dict, *, sign: bool) -> dict:
        return {
            "patient_id": patient["id"], "queue_entry_id": entry["id"], "doctor_id": doctor["id"],
            "date": today, "chief_complaint": "From the queue", "notes": "", "diagnosis": "",
            "follow_up_days": None, "clinical_ext": {}, "sign": sign,
        }

    async def board_status(entry: dict) -> str:
        queue = (await northgate.get("/v1/queue")).json()
        return next(q["status"] for q in queue if q["id"] == entry["id"])

    # in the room, signed on the spot
    entry = await on_the_board(patients[4], started=True)
    signed = await northgate.post("/v1/encounters", json=form(patients[4], entry, sign=True))
    assert signed.status_code == 201, signed.text
    assert await board_status(entry) == "completed"

    # called but never started
    entry = await on_the_board(patients[5], started=False)
    signed = await northgate.post("/v1/encounters", json=form(patients[5], entry, sign=True))
    assert signed.status_code == 201, signed.text
    assert await board_status(entry) == "completed"

    # a draft first, signed from its own page
    entry = await on_the_board(patients[6], started=True)
    draft = (await northgate.post("/v1/encounters", json=form(patients[6], entry, sign=False))).json()
    assert await board_status(entry) == "in_consultation"
    assert (await northgate.patch(f"/v1/encounters/{draft['id']}", json={"sign": True})).status_code == 200
    assert await board_status(entry) == "completed"


async def test_a_consultation_and_its_prescription_stay_editable_until_billed(northgate):
    patient = (await patients_of(northgate))[6]
    encounter = (
        await northgate.post(
            "/v1/encounters",
            json={"patient_id": patient["id"], "chief_complaint": "Editable", "sign": True},
        )
    ).json()
    url = f"/v1/encounters/{encounter['id']}"
    line = {"medicine": "Typed by hand", "strength": "5 mg", "qty": 2}

    # no prescription yet: replacing creates one
    rx = (await northgate.put(f"{url}/prescription", json={"items": [line]})).json()
    assert [i["medicine"] for i in rx["items"]] == ["Typed by hand"]
    assert rx["doctor_name"], "a prescription carries its prescriber's name, whoever may read the staff list"

    # rewriting it keeps one prescription, with the new lines
    rx = (
        await northgate.put(
            f"{url}/prescription", json={"items": [{**line, "medicine": "Corrected"}, {**line, "medicine": "Added"}]}
        )
    ).json()
    assert [i["medicine"] for i in rx["items"]] == ["Corrected", "Added"]
    listed = (await northgate.get("/v1/prescriptions", params={"patient_id": patient["id"]})).json()
    assert [p["id"] for p in listed["items"] if p["encounter_id"] == encounter["id"]] == [rx["id"]]

    assert (await northgate.patch(url, json={"diagnosis": "Revised"})).json()["diagnosis"] == "Revised"

    # a draft invoice has not gone to the patient
    draft = (
        await northgate.post(
            "/v1/billing/invoices",
            json={
                "patient_id": patient["id"],
                "line_items": [{"description": "Consultation", "source": "consultation", "qty": 1,
                                "unit_price": 10, "ref_id": encounter["id"]}],
                "issue": False,
            },
        )
    ).json()
    assert (await northgate.patch(url, json={"notes": "still open"})).status_code == 200

    # issued from the billing desk, without naming the encounter: still open until paid
    await northgate.post(f"/v1/billing/invoices/{draft['id']}/issue")
    assert (await northgate.patch(url, json={"notes": "issued, unpaid"})).status_code == 200

    # paid: now it is locked
    await northgate.post(f"/v1/billing/invoices/{draft['id']}/payments", json={"amount": 10, "method": "cash"})
    assert (await northgate.patch(url, json={"notes": "too late"})).status_code == 409
    assert (await northgate.put(f"{url}/prescription", json={"items": [line]})).status_code == 409

    # voiding the paid bill opens it again
    await northgate.post(f"/v1/billing/invoices/{draft['id']}/void")
    assert (await northgate.patch(url, json={"notes": "reopened"})).status_code == 200

    # taking every line off removes the prescription
    assert (await northgate.put(f"{url}/prescription", json={"items": []})).json() is None


async def test_lab_results_are_withheld_until_verified(client, northgate):
    doctor = await _doctor(northgate)
    patient = (await patients_of(northgate))[2]

    encounter = (
        await northgate.post(
            "/v1/encounters",
            json={
                "patient_id": patient["id"],
                "doctor_id": doctor["id"],
                "chief_complaint": "Swelling, lower left",
                "sign": False,
            },
        )
    ).json()

    orders = (
        await northgate.post(
            f"/v1/encounters/{encounter['id']}/lab-orders",
            json={"tests": [{"code": "OPG", "name": "Orthopantomogram", "panel": "Imaging"}]},
        )
    ).json()
    order = orders[0]
    assert order["status"] == "ordered"

    lab = await client.post(
        "/v1/auth/login",
        headers={"Host": northgate.host},
        json={"email": "lab@northgate.clinic", "password": "lab-2026"},
    )
    token = lab.json()["access_token"]
    headers = {"Host": northgate.host, "Authorization": f"Bearer {token}"}

    await client.post(f"/v1/lab/orders/{order['id']}/start", headers=headers)
    resulted = await client.post(
        f"/v1/lab/orders/{order['id']}/results",
        headers=headers,
        json={"results": [{"analyte": "38 angulation", "value": "Vertical", "unit": "", "reference": "—"}]},
    )
    assert resulted.status_code == 200
    assert resulted.json()["status"] == "resulted"

    withheld = (await northgate.get(f"/v1/encounters/{encounter['id']}/lab-results")).json()
    assert withheld == [], "unverified results stay out of the patient record"

    in_flight = (
        await northgate.get(
            f"/v1/encounters/{encounter['id']}/lab-results", params={"include_unverified": True}
        )
    ).json()
    assert in_flight[0]["results"] == [], "even then the values themselves are not released"

    verified = await client.patch(
        f"/v1/lab/orders/{order['id']}", headers=headers, json={"status": "verified"}
    )
    assert verified.status_code == 200

    released = (await northgate.get(f"/v1/encounters/{encounter['id']}/lab-results")).json()
    assert released[0]["results"][0]["analyte"] == "38 angulation"


async def test_stock_cannot_go_negative(northgate):
    items = (await northgate.get("/v1/inventory/items")).json()
    batches = (await northgate.get("/v1/inventory/batches")).json()
    batch = next(b for b in batches if b["qty"] > 0)

    response = await northgate.post(
        "/v1/inventory/transactions",
        json={
            "item_id": batch["item_id"],
            "type": "dispense",
            "qty": batch["qty"] + 1000,
            "batch_no": batch["batch_no"],
        },
    )
    assert response.status_code == 409
    assert "left" in response.json()["error"]["message"]
    assert items


async def test_a_purchase_adds_to_the_ledger_and_the_batch(northgate):
    item = (await northgate.get("/v1/inventory/items")).json()[0]
    before = next(
        r for r in (await northgate.get("/v1/inventory/levels")).json() if r["item_id"] == item["id"]
    )

    added = await northgate.post(
        "/v1/inventory/transactions",
        json={
            "item_id": item["id"],
            "type": "purchase",
            "qty": 25,
            "batch_no": "TEST-BATCH-1",
            "expiry": "2029-01-31",
            "note": "Test delivery",
        },
    )
    assert added.status_code == 201
    assert added.json()["qty"] == 25

    after = next(
        r for r in (await northgate.get("/v1/inventory/levels")).json() if r["item_id"] == item["id"]
    )
    assert after["on_hand"] == before["on_hand"] + 25


async def test_expiry_alert_threshold_is_configurable(northgate):
    default = (await northgate.get("/v1/inventory/expiring")).json()

    updated = (await northgate.patch("/v1/tenant/config", json={"expiry_alert_days": 5})).json()
    assert updated["expiry_alert_days"] == 5

    tightened = (await northgate.get("/v1/inventory/expiring")).json()
    assert len(tightened) <= len(default)

    override = (await northgate.get("/v1/inventory/expiring", params={"within_days": 730})).json()
    assert len(override) >= len(tightened)

    await northgate.patch("/v1/tenant/config", json={"expiry_alert_days": 90})


async def test_an_invoice_can_be_voided_but_not_paid_afterwards(northgate):
    patient = (await patients_of(northgate))[0]
    invoice = (
        await northgate.post(
            "/v1/billing/invoices",
            json={
                "patient_id": patient["id"],
                "line_items": [{"description": "Void me", "source": "manual", "qty": 1, "unit_price": 10}],
                "issue": True,
            },
        )
    ).json()

    voided = await northgate.post(f"/v1/billing/invoices/{invoice['id']}/void")
    assert voided.status_code == 200
    assert voided.json()["status"] == "void"

    payment = await northgate.post(
        f"/v1/billing/invoices/{invoice['id']}/payments", json={"amount": 10, "method": "cash"}
    )
    assert payment.status_code == 409


async def test_analytics_has_every_module_and_follows_clinical_type(northgate, clearview, rowan):
    dental = (await northgate.get("/v1/analytics/dashboard")).json()
    assert dental["core"]["patients"] > 0
    assert dental["clinical"]["type"] == "dental"
    assert set(dental["modules"]) == ALL_PANELS

    eyes = (await clearview.get("/v1/analytics/dashboard")).json()
    assert eyes["clinical"]["type"] == "ophthalmology"
    assert "avg_iop_right" in eyes["clinical"]

    skin = (await rowan.get("/v1/analytics/dashboard")).json()
    assert skin["clinical"]["type"] == "dermatology"
    assert set(skin["modules"]) == ALL_PANELS, "every clinic gets every module panel"
    assert set(eyes["modules"]) == ALL_PANELS


async def test_analytics_is_computed_from_the_rows_it_describes(northgate):
    """Every panel the reports page renders."""
    body = (await northgate.get("/v1/analytics/dashboard", params={"days": 30})).json()

    core = body["core"]
    assert core["patients"] > 0
    assert core["encounters"] >= 0
    assert len(core["visits_series"]) == 30
    assert {"date", "value"} == set(core["visits_series"][0])
    assert len(core["attendance_series"]) == 14
    assert {"date", "kept", "missed", "cancelled"} == set(core["attendance_series"][0])
    assert [row["label"] for row in core["queue_funnel"]][:2] == ["Waiting", "Called"]
    assert core["avg_wait_minutes"] >= 0

    # the register agrees with the paged endpoint that counts the same rows
    page = (await northgate.get("/v1/patients", params={"limit": 1})).json()
    assert page["total"] == core["patients"]

    modules = body["modules"]
    assert set(modules) == ALL_PANELS
    assert modules["billing"]["billed"] >= modules["billing"]["paid"]
    assert modules["lab"]["total"] >= modules["lab"]["verified"]
    assert isinstance(modules["inventory"]["below_reorder"], list)

    assert body["clinical"]["type"] == "dental"
    assert "breakdown" in body["clinical"]


async def test_analytics_has_every_panel_even_where_a_module_was_never_on(rowan):
    """Rowan only ever had the lab switched on. Every panel is computed for it now."""
    body = (await rowan.get("/v1/analytics/dashboard")).json()
    assert set(body["modules"]) == ALL_PANELS
    assert body["clinical"]["type"] == "dermatology"
    assert {"age_cohort", "sex_split", "clinicians"} <= set(body["modules"]["advanced_reports"])
    assert "billed" in body["modules"]["billing"], "the practice admin sees the money"


async def test_analytics_is_the_same_shape_for_a_role_without_revenue(client):
    """Redaction takes the money out of the panels; it never takes a panel away."""
    doctor = await sign_in(client, "northgate_doctor")
    body = (await doctor.get("/v1/analytics/dashboard")).json()
    assert body["revenue_visible"] is False
    assert set(body["modules"]) == ALL_PANELS


async def test_advanced_reports_carries_the_cohort_panels(clearview):
    body = (await clearview.get("/v1/analytics/dashboard")).json()
    advanced = body["modules"]["advanced_reports"]

    assert [row["label"] for row in advanced["age_cohort"]] == ["0–17", "18–34", "35–54", "55–74", "75+"]
    assert sum(row["value"] for row in advanced["age_cohort"]) == body["core"]["patients"]
    assert sum(row["value"] for row in advanced["sex_split"]) == body["core"]["patients"]
    assert advanced["clinicians"], "a clinic with doctors reports on them"
    assert {"name", "encounters", "prescriptions", "revenue"} <= set(advanced["clinicians"][0])


async def test_the_register_is_paged_searched_and_sorted_by_the_database(northgate):
    first = (await northgate.get("/v1/patients", params={"limit": 5, "sort": "full_name", "order": "asc"})).json()
    assert len(first["items"]) == 5
    assert first["total"] > 5
    assert first["items"] == sorted(first["items"], key=lambda p: p["full_name"])

    second = (
        await northgate.get(
            "/v1/patients", params={"limit": 5, "offset": 5, "sort": "full_name", "order": "asc"}
        )
    ).json()
    assert second["offset"] == 5
    assert second["total"] == first["total"]
    ids = {p["id"] for p in first["items"]}
    assert ids.isdisjoint({p["id"] for p in second["items"]}), "pages do not overlap"
    assert first["items"][-1]["full_name"] <= second["items"][0]["full_name"]

    named = (await northgate.get("/v1/patients", params={"search": "Rosalind"})).json()
    assert named["total"] >= 1
    assert all("rosalind" in p["full_name"].lower() for p in named["items"])
    assert named["total"] < first["total"], "a search narrows the total, not just the page"

    by_mrn = (await northgate.get("/v1/patients", params={"search": named["items"][0]["mrn"]})).json()
    assert by_mrn["total"] == 1

    nothing = (await northgate.get("/v1/patients", params={"search": "zzz-no-such-patient"})).json()
    assert nothing == {"items": [], "total": 0, "limit": 25, "offset": 0}


async def test_a_search_cannot_reach_across_clinics(northgate, clearview):
    theirs = (await clearview.get("/v1/patients", params={"limit": 1})).json()["items"][0]
    hunted = (await northgate.get("/v1/patients", params={"search": theirs["full_name"]})).json()
    assert hunted["total"] == 0, "another clinic's patient is not findable by name"


async def test_list_rows_name_their_patient(northgate):
    """So a screen never needs the register in order to render a name."""
    for path in ("/v1/appointments", "/v1/queue", "/v1/encounters", "/v1/prescriptions"):
        body = (await northgate.get(path)).json()
        # The queue is a collection; the rest answer with a page around one.
        rows = body if isinstance(body, list) else body["items"]
        assert rows, path
        assert rows[0]["patient_name"], path
        assert rows[0]["patient_mrn"].startswith("NG-"), path

    invoices = (await northgate.get("/v1/billing/invoices")).json()["items"]
    assert invoices[0]["patient_name"]


async def test_checking_in_a_booking_is_refused_for_someone_already_in_the_queue(northgate, monkeypatch):
    doctor = await _doctor(northgate)
    patient = (
        await northgate.post(
            "/v1/patients",
            json={"full_name": "Booked And Walked In", "dob": "1990-01-01", "sex": "female", "phone": "9770090099"},
        )
    ).json()
    # a booking on today at 17:45 is only a plan if the clinic's clock is before it
    day = (await freeze_clinic_clock(monkeypatch, northgate, "08:00")).isoformat()
    booked = await northgate.post(
        "/v1/appointments",
        json={"patient_id": patient["id"], "doctor_id": doctor["id"], "date": day, "time": "17:45",
              "duration_min": 15, "reason": "Booked"},
    )
    assert booked.status_code == 201, booked.text
    walked = await northgate.post(
        "/v1/queue/walkin", json={"patient_id": patient["id"], "doctor_id": doctor["id"], "reason": "Early"}
    )
    assert walked.status_code == 201, walked.text

    refused = await northgate.post(f"/v1/appointments/{booked.json()['id']}/checkin")
    assert refused.status_code == 409


async def test_a_tooth_chart_keeps_mobility_and_fracture_alongside_the_older_states(northgate):
    doctor = await _doctor(northgate)
    patient = (
        await northgate.post(
            "/v1/patients",
            json={"full_name": "Tooth Chart Patient", "dob": "1985-03-02", "sex": "male", "phone": "9770090123"},
        )
    ).json()
    chart = {"11": "mobility", "21": "fracture", "16": "caries", "36": "extract", "46": "watch"}

    made = await northgate.post(
        "/v1/encounters",
        json={
            "patient_id": patient["id"], "doctor_id": doctor["id"], "chief_complaint": "Knock to the front teeth",
            "clinical_ext": {"tooth_chart": chart},
        },
    )
    assert made.status_code == 201, made.text
    assert made.json()["clinical_ext"]["tooth_chart"] == chart

    read_back = (await northgate.get(f"/v1/encounters/{made.json()['id']}")).json()
    assert read_back["clinical_ext"]["tooth_chart"] == chart, "the new states survive a round trip"

    # a chart that is not a map of teeth is not a tooth chart — it is dropped, not stored
    refused = await northgate.post(
        "/v1/encounters",
        json={
            "patient_id": patient["id"], "doctor_id": doctor["id"], "chief_complaint": "Malformed chart",
            "clinical_ext": {"tooth_chart": ["mobility", "fracture"]},
        },
    )
    assert refused.status_code == 201, refused.text
    assert "tooth_chart" not in refused.json()["clinical_ext"]
