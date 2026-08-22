"""The core loop end to end, and the state machines that hold it together."""

from __future__ import annotations

from tests.conftest import patients_of, sign_in


async def _doctor(signed) -> dict:
    return (await signed.get("/v1/users/doctors")).json()[0]


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
                "phone": "+44 7700 900555",
                "clinical_ext": {"bruxism": True, "ortho_history": "None"},
            },
        )
    ).json()
    assert patient["mrn"].startswith("NG-")
    assert patient["clinical_ext"]["bruxism"] is True

    # 3.5 — booking, with the availability check behind it.
    booking = {
        "patient_id": patient["id"],
        "doctor_id": doctor["id"],
        "date": "2026-09-15",
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
                "date": "2026-09-15",
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

    frozen = await northgate.patch(f"/v1/encounters/{encounter['id']}", json={"notes": "changed"})
    assert frozen.status_code == 409, "a signed consultation is not editable"

    # 3.8 — prescribing is core.
    items = (await northgate.get("/v1/inventory/items")).json()
    stock_item = next(i for i in items if i["name"].startswith("Ibuprofen"))
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
    ).json()
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

    # 3.13 — the history composition sees all of it, in one place.
    history = (await northgate.get(f"/v1/patients/{patient['id']}/history")).json()
    kinds = {event["kind"] for event in history}
    assert {"registration", "appointment", "queue", "encounter", "prescription", "invoice", "payment"} <= kinds


async def test_a_walk_in_needs_no_appointment(northgate):
    doctor = await _doctor(northgate)
    patient = (await patients_of(northgate))[0]

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


async def test_tokens_are_per_clinic_per_day(northgate, clearview):
    """Two clinics issuing tokens on the same day do not collide."""
    ours = [q["token_no"] for q in (await northgate.get("/v1/queue")).json()]
    theirs = [q["token_no"] for q in (await clearview.get("/v1/queue")).json()]
    assert ours and theirs
    assert min(ours) == 1 and min(theirs) == 1


async def test_the_queue_state_machine_refuses_impossible_moves(northgate):
    doctor = await _doctor(northgate)
    patient = (await patients_of(northgate))[1]
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


async def test_analytics_follows_entitlements_and_clinical_type(northgate, clearview, rowan):
    dental = (await northgate.get("/v1/analytics/dashboard")).json()
    assert dental["core"]["patients"] > 0
    assert dental["clinical"]["type"] == "dental"
    assert {"billing", "pharmacy", "lab", "inventory"} <= set(dental["modules"])

    eyes = (await clearview.get("/v1/analytics/dashboard")).json()
    assert eyes["clinical"]["type"] == "ophthalmology"
    assert "avg_iop_right" in eyes["clinical"]

    skin = (await rowan.get("/v1/analytics/dashboard")).json()
    assert skin["clinical"]["type"] == "dermatology"
    assert "pharmacy" not in skin["modules"], "a module that was not bought produces no panel"
    assert "lab" in skin["modules"]


async def test_analytics_is_computed_from_the_rows_it_describes(northgate):
    """Every panel the reports page renders, and nothing a clinic did not buy."""
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
    assert {"pharmacy", "lab", "inventory", "billing"} <= set(modules)
    assert modules["billing"]["billed"] >= modules["billing"]["paid"]
    assert modules["lab"]["total"] >= modules["lab"]["verified"]
    assert isinstance(modules["inventory"]["below_reorder"], list)

    assert body["clinical"]["type"] == "dental"
    assert "breakdown" in body["clinical"]


async def test_analytics_hides_what_a_clinic_never_bought(rowan):
    body = (await rowan.get("/v1/analytics/dashboard")).json()
    assert set(body["modules"]) == {"lab"}, "only the module Rowan actually has"
    assert body["clinical"]["type"] == "dermatology"
    assert "advanced_reports" not in body["modules"]


async def test_advanced_reports_unlocks_the_cohort_panels(clearview):
    """Clearview bought advanced reports; the extra panels come with it."""
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
