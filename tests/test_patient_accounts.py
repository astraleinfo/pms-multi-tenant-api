"""Paying at the counter, and paying afterwards.

The window hands medicine over and asks for money; the front desk asks for
whatever is still owed. Both read the same account, and each counter is settled
on its own — paying for your medicine is not paying for your scan.
"""

from __future__ import annotations

from itertools import count

from app.core.permissions import Permission, Role, role_has
from tests.conftest import audit_lines


async def _doctor(signed) -> dict:
    return (await signed.get("/v1/users/doctors")).json()[0]


async def _new_patient(signed, name: str) -> dict:
    response = await signed.post(
        "/v1/patients",
        json={"full_name": name, "dob": "1990-02-02", "sex": "female", "phone": "+44 7700 900123"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _encounter(signed, patient: dict, doctor: dict, complaint: str) -> dict:
    response = await signed.post(
        "/v1/encounters",
        json={
            "patient_id": patient["id"],
            "doctor_id": doctor["id"],
            "date": "2026-09-03",
            "chief_complaint": complaint,
            "notes": "",
            "diagnosis": "",
            "clinical_ext": {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


_sku_seq = count(1)


async def _stocked_item(signed) -> dict:
    """A stock line with something on the shelf and a price on it.

    Its own code each time: these run against one seeded database, and a stock
    code is unique within a clinic.
    """
    opened = await signed.post(
        "/v1/inventory/items",
        json={
            "sku": f"ACC-{next(_sku_seq):04d}",
            "name": "Account test tablets",
            "strength": "500 mg",
            "unit": "tab",
            "category": "medicine",
            "unit_price": 10,
        },
    )
    assert opened.status_code == 201, opened.text
    item = opened.json()

    stocked = await signed.post(
        "/v1/inventory/transactions",
        json={
            "item_id": item["id"],
            "type": "purchase",
            "qty": 500,
            "batch_no": "ACC-B1",
            "expiry": "2028-01-31",
        },
    )
    assert stocked.status_code == 201, stocked.text
    return item


async def _dispensed(clinician, window, doctor, patient, item, qty: int = 6) -> dict:
    """A prescription, dispensed — the state the window is in when it asks for
    money.

    Two roles, because that is how it happens: a clinician writes it, and the
    pharmacy hands it over. A doctor has no `pharmacy:dispense`, which is worth
    the test being explicit about rather than quietly 403ing.
    """
    encounter = await _encounter(clinician, patient, doctor, "Account test")
    written = await clinician.post(
        f"/v1/encounters/{encounter['id']}/prescriptions",
        json={
            "items": [
                {
                    "medicine": "Account test tablets",
                    "strength": "500 mg",
                    "qty": qty,
                    "item_id": item["id"],
                }
            ]
        },
    )
    assert written.status_code == 201, written.text
    prescription = written.json()
    line = prescription["items"][0]

    handed = await window.post(
        f"/v1/pharmacy/prescriptions/{prescription['id']}/dispense",
        json={"lines": [{"item_id": line["id"], "qty": qty, "batch_no": "ACC-B1"}]},
    )
    assert handed.status_code == 200, handed.text
    return {"encounter": encounter, "prescription": prescription, "line": line}


# --- what is owed ------------------------------------------------------------


async def test_a_new_patient_owes_nothing(northgate):
    patient = await _new_patient(northgate, "Owes Nothing")
    account = (await northgate.get(f"/v1/billing/patients/{patient['id']}/account")).json()

    assert account["total_due"] == 0
    assert set(account["departments"]) == {"clinic", "lab", "pharmacy"}
    assert all(d["unbilled"] == [] for d in account["departments"].values())


async def test_dispensing_puts_a_charge_on_the_pharmacy_counter(northgate, northgate_doctor):
    patient = await _new_patient(northgate, "Pharmacy Charge")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)
    await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=6)

    account = (await northgate.get(f"/v1/billing/patients/{patient['id']}/account")).json()
    pharmacy = account["departments"]["pharmacy"]

    assert len(pharmacy["unbilled"]) == 1
    charge = pharmacy["unbilled"][0]
    assert charge["qty"] == 6
    assert charge["unit_price"] == 10.0
    assert charge["total"] == 60.0
    assert charge["priced"] is True
    # Nothing has been invoiced yet, so all of it is on the unbilled side.
    assert pharmacy["outstanding"] == 0
    assert pharmacy["due"] == 60.0


async def test_the_clinic_counter_reflects_a_doctors_own_fee(northgate, northgate_doctor):
    patient = await _new_patient(northgate, "Doctor Fee Account")
    doctor = await _doctor(northgate)

    await northgate.patch(f"/v1/pricing/doctors/{doctor['id']}", json={"consultation_fee": 777.0})
    try:
        await _encounter(northgate_doctor, patient, doctor, "Doctor's own fee")

        account = (await northgate.get(f"/v1/billing/patients/{patient['id']}/account")).json()
        clinic = account["departments"]["clinic"]
        assert len(clinic["unbilled"]) == 1
        charge = clinic["unbilled"][0]
        assert charge["unit_price"] == 777.0
        assert charge["priced"] is True
        assert clinic["due"] == charge["total"]
    finally:
        await northgate.patch(f"/v1/pricing/doctors/{doctor['id']}", json={"consultation_fee": None})


async def test_a_visit_charges_each_counter_separately(northgate, northgate_doctor):
    patient = await _new_patient(northgate, "Three Counters")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)

    made = await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=4)
    await northgate_doctor.post(
        f"/v1/encounters/{made['encounter']['id']}/lab-orders",
        json={"tests": [{"code": "CBCT", "name": "Cone beam CT", "panel": "Imaging"}]},
    )

    account = (await northgate.get(f"/v1/billing/patients/{patient['id']}/account")).json()
    assert account["departments"]["pharmacy"]["due"] == 40.0
    assert account["departments"]["lab"]["due"] == 3500.0, "the CBCT's own tariff"
    assert account["departments"]["clinic"]["due"] > 0, "the consultation itself"
    assert account["total_due"] == round(
        sum(d["due"] for d in account["departments"].values()), 2
    )


# --- billing one counter -----------------------------------------------------


async def test_a_counter_bills_only_its_own_charges(northgate, northgate_doctor):
    patient = await _new_patient(northgate, "One Counter")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)
    made = await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=5)
    await northgate_doctor.post(
        f"/v1/encounters/{made['encounter']['id']}/lab-orders",
        json={"tests": [{"code": "BW", "name": "Bitewing radiographs", "panel": "Imaging"}]},
    )

    invoice = (
        await northgate.post(
            f"/v1/billing/patients/{patient['id']}/invoices",
            json={"department": "pharmacy", "issue": True},
        )
    ).json()

    assert invoice["department"] == "pharmacy"
    assert [line["source"] for line in invoice["line_items"]] == ["pharmacy"]
    assert invoice["totals"]["total"] == 50.0

    account = (await northgate.get(f"/v1/billing/patients/{patient['id']}/account")).json()
    # The pharmacy's charge has moved from unbilled to outstanding…
    assert account["departments"]["pharmacy"]["unbilled"] == []
    assert account["departments"]["pharmacy"]["outstanding"] == 50.0
    # …and the lab's is untouched.
    assert account["departments"]["lab"]["due"] == 350.0


async def test_compose_invoice_does_not_resuggest_a_pharmacy_settled_item(northgate, northgate_doctor):
    """§3.12 Compose Invoice must not offer a line the pharmacy counter has
    already billed and been paid for — and voiding that invoice must put it
    straight back on offer, the same as any other counter charge."""
    patient = await _new_patient(northgate, "No Double Bill")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)
    made = await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=6)
    encounter_id = made["encounter"]["id"]

    # Before the counter has billed anything, the pharmacy line is suggested.
    before = (
        await northgate.get("/v1/billing/invoices/suggestions", params={"encounter_id": encounter_id})
    ).json()
    assert any(line["source"] == "pharmacy" for line in before["lines"])
    assert before["already_billed"] == []

    # The pharmacy counter bills and takes payment for it, the way the
    # dispensing window actually does — raise then settle.
    raised = await northgate.post(
        f"/v1/billing/patients/{patient['id']}/invoices",
        json={"department": "pharmacy", "issue": True},
    )
    assert raised.status_code == 201, raised.text
    invoice = raised.json()
    settled = await northgate.post(
        f"/v1/billing/patients/{patient['id']}/payments",
        json={"department": "pharmacy", "amount": 60.0, "method": "cash"},
    )
    assert settled.status_code == 200, settled.text

    # Now Compose Invoice for the same visit must not offer it again.
    after = (
        await northgate.get("/v1/billing/invoices/suggestions", params={"encounter_id": encounter_id})
    ).json()
    assert not any(line["source"] == "pharmacy" for line in after["lines"])
    assert len(after["already_billed"]) == 1
    billed = after["already_billed"][0]
    assert billed["qty"] == 6
    assert billed["invoices"][0]["invoice_status"] == "paid"

    # Voiding the invoice is how a mistaken charge is undone — the item goes
    # straight back to being suggested, exactly as if it had never been billed.
    voided = await northgate.post(f"/v1/billing/invoices/{invoice['id']}/void")
    assert voided.status_code == 200, voided.text

    reopened = (
        await northgate.get("/v1/billing/invoices/suggestions", params={"encounter_id": encounter_id})
    ).json()
    assert reopened["already_billed"] == []
    pharmacy_line = next(line for line in reopened["lines"] if line["source"] == "pharmacy")
    assert pharmacy_line["qty"] == 6


async def test_the_same_charge_is_never_billed_twice(northgate, northgate_doctor):
    """The point of a line that names what it bills."""
    patient = await _new_patient(northgate, "Billed Once")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)
    await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=3)

    first = await northgate.post(
        f"/v1/billing/patients/{patient['id']}/invoices", json={"department": "pharmacy"}
    )
    assert first.status_code == 201

    again = await northgate.post(
        f"/v1/billing/patients/{patient['id']}/invoices", json={"department": "pharmacy"}
    )
    assert again.status_code == 409
    assert "nothing left to bill" in again.json()["error"]["message"]


async def test_dispensing_the_rest_puts_the_rest_on_the_counter(northgate, northgate_doctor):
    """A partly dispensed line is a partial charge; the rest follows the rest."""
    patient = await _new_patient(northgate, "Half Then Half")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)

    encounter = await _encounter(northgate_doctor, patient, doctor, "Two halves")
    prescription = (
        await northgate_doctor.post(
            f"/v1/encounters/{encounter['id']}/prescriptions",
            json={
                "items": [
                    {"medicine": "Account test tablets", "qty": 10, "item_id": item["id"]}
                ]
            },
        )
    ).json()
    line = prescription["items"][0]

    await northgate.post(
        f"/v1/pharmacy/prescriptions/{prescription['id']}/dispense",
        json={"lines": [{"item_id": line["id"], "qty": 4, "batch_no": "ACC-B1"}]},
    )
    await northgate.post(
        f"/v1/billing/patients/{patient['id']}/invoices", json={"department": "pharmacy"}
    )

    charges = (
        await northgate.get(
            f"/v1/billing/patients/{patient['id']}/charges", params={"department": "pharmacy"}
        )
    ).json()
    assert charges == [], "the four handed over are on an invoice"

    await northgate.post(
        f"/v1/pharmacy/prescriptions/{prescription['id']}/dispense",
        json={"lines": [{"item_id": line["id"], "qty": 6, "batch_no": "ACC-B1"}]},
    )
    charges = (
        await northgate.get(
            f"/v1/billing/patients/{patient['id']}/charges", params={"department": "pharmacy"}
        )
    ).json()
    assert len(charges) == 1
    assert charges[0]["qty"] == 6, "only the six just handed over"


async def test_voiding_an_invoice_puts_its_charges_back(northgate, northgate_doctor):
    patient = await _new_patient(northgate, "Voided Back")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)
    await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=2)

    invoice = (
        await northgate.post(
            f"/v1/billing/patients/{patient['id']}/invoices", json={"department": "pharmacy"}
        )
    ).json()
    await northgate.post(f"/v1/billing/invoices/{invoice['id']}/void")

    charges = (
        await northgate.get(
            f"/v1/billing/patients/{patient['id']}/charges", params={"department": "pharmacy"}
        )
    ).json()
    assert len(charges) == 1 and charges[0]["qty"] == 2


async def test_the_window_can_bill_just_what_it_handed_over(northgate, northgate_doctor):
    """`ref_ids` is the dispensing window billing this hand-over, not every
    thing the pharmacy has ever been owed for."""
    patient = await _new_patient(northgate, "Just This One")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)
    first = await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=2)
    await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=7)

    invoice = (
        await northgate.post(
            f"/v1/billing/patients/{patient['id']}/invoices",
            json={"department": "pharmacy", "ref_ids": [first["line"]["id"]]},
        )
    ).json()
    assert invoice["totals"]["total"] == 20.0

    left = (
        await northgate.get(
            f"/v1/billing/patients/{patient['id']}/charges", params={"department": "pharmacy"}
        )
    ).json()
    assert len(left) == 1 and left[0]["qty"] == 7


# --- paying at the counter ---------------------------------------------------


async def test_one_amount_clears_that_counter(northgate, northgate_doctor):
    patient = await _new_patient(northgate, "Pays At Window")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)
    await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=6)

    await northgate.post(
        f"/v1/billing/patients/{patient['id']}/invoices",
        json={"department": "pharmacy", "issue": True},
    )
    paid = await northgate.post(
        f"/v1/billing/patients/{patient['id']}/payments",
        json={"department": "pharmacy", "amount": 60, "method": "cash"},
    )
    assert paid.status_code == 200, paid.text
    body = paid.json()
    assert body["remaining"] == 0
    assert body["invoices"][0]["status"] == "paid"

    account = (await northgate.get(f"/v1/billing/patients/{patient['id']}/account")).json()
    assert account["departments"]["pharmacy"]["due"] == 0


async def test_paying_the_pharmacy_leaves_the_lab_owed(northgate, northgate_doctor):
    """The whole point of separate counters."""
    patient = await _new_patient(northgate, "Pharmacy Not Lab")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)
    made = await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=5)
    await northgate_doctor.post(
        f"/v1/encounters/{made['encounter']['id']}/lab-orders",
        json={"tests": [{"code": "BW", "name": "Bitewing radiographs", "panel": "Imaging"}]},
    )

    for department in ("pharmacy", "lab"):
        await northgate.post(
            f"/v1/billing/patients/{patient['id']}/invoices",
            json={"department": department, "issue": True},
        )

    await northgate.post(
        f"/v1/billing/patients/{patient['id']}/payments",
        json={"department": "pharmacy", "amount": 50, "method": "upi"},
    )

    account = (await northgate.get(f"/v1/billing/patients/{patient['id']}/account")).json()
    assert account["departments"]["pharmacy"]["due"] == 0
    assert account["departments"]["lab"]["due"] == 350.0


async def test_part_payment_leaves_the_rest_owed(northgate, northgate_doctor):
    patient = await _new_patient(northgate, "Pays Some")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)
    await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=10)

    await northgate.post(
        f"/v1/billing/patients/{patient['id']}/invoices",
        json={"department": "pharmacy", "issue": True},
    )
    body = (
        await northgate.post(
            f"/v1/billing/patients/{patient['id']}/payments",
            json={"department": "pharmacy", "amount": 40, "method": "cash"},
        )
    ).json()
    assert body["remaining"] == 60.0
    assert body["invoices"][0]["status"] == "partially_paid"


async def test_one_amount_spreads_across_that_counters_invoices(northgate, northgate_doctor):
    """Oldest first — the patient hands over one sum, not one per invoice."""
    patient = await _new_patient(northgate, "Two Invoices")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)

    first = await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=3)
    await northgate.post(
        f"/v1/billing/patients/{patient['id']}/invoices",
        json={"department": "pharmacy", "issue": True, "ref_ids": [first["line"]["id"]]},
    )
    second = await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=4)
    await northgate.post(
        f"/v1/billing/patients/{patient['id']}/invoices",
        json={"department": "pharmacy", "issue": True, "ref_ids": [second["line"]["id"]]},
    )

    body = (
        await northgate.post(
            f"/v1/billing/patients/{patient['id']}/payments",
            json={"department": "pharmacy", "amount": 70, "method": "card"},
        )
    ).json()
    assert len(body["invoices"]) == 2
    assert all(invoice["status"] == "paid" for invoice in body["invoices"])
    assert body["remaining"] == 0


async def test_more_than_the_counter_is_owed_is_refused(northgate, northgate_doctor):
    patient = await _new_patient(northgate, "Overpays")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)
    await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=2)
    await northgate.post(
        f"/v1/billing/patients/{patient['id']}/invoices",
        json={"department": "pharmacy", "issue": True},
    )

    refused = await northgate.post(
        f"/v1/billing/patients/{patient['id']}/payments",
        json={"department": "pharmacy", "amount": 500, "method": "cash"},
    )
    assert refused.status_code == 409
    assert "more than" in refused.json()["error"]["message"]


async def test_paying_a_counter_that_is_owed_nothing_is_refused(northgate):
    patient = await _new_patient(northgate, "Owes The Lab Nothing")
    refused = await northgate.post(
        f"/v1/billing/patients/{patient['id']}/payments",
        json={"department": "lab", "amount": 10, "method": "cash"},
    )
    assert refused.status_code == 409


async def test_a_counter_payment_is_recorded(northgate, northgate_doctor):
    patient = await _new_patient(northgate, "Audited Payment")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)
    await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=2)
    await northgate.post(
        f"/v1/billing/patients/{patient['id']}/invoices",
        json={"department": "pharmacy", "issue": True},
    )
    await northgate.post(
        f"/v1/billing/patients/{patient['id']}/payments",
        json={"department": "pharmacy", "amount": 20, "method": "cash"},
    )

    trail = await audit_lines(northgate.tenant_id, action="invoice.payment")
    assert any(row["meta"].get("department") == "pharmacy" for row in trail)


# --- who may take it ---------------------------------------------------------


def test_every_clinical_role_can_take_money_at_a_counter():
    """The counter takes the money, so the roles that staff a counter must be
    able to bill and settle. A pharmacist who cannot invoice cannot ask for
    payment as the medicine is handed over, which is the whole feature."""
    for role in (Role.DOCTOR, Role.PHARMACIST, Role.LAB_TECH, Role.RECEPTIONIST):
        assert role_has(role.value, Permission.BILLING_READ), role
        assert role_has(role.value, Permission.BILLING_WRITE), role


async def test_a_clinician_settles_their_own_consultation(northgate, northgate_doctor):
    """The refusal this replaces was a real 403: a doctor could see the account
    and not act on it, so the counter button on their screen always failed."""
    patient = await _new_patient(northgate, "Clinician Collects")
    doctor = await _doctor(northgate)
    await _encounter(northgate_doctor, patient, doctor, "Seen and paid for")

    raised = await northgate_doctor.post(
        f"/v1/billing/patients/{patient['id']}/invoices",
        json={"department": "clinic", "issue": True},
    )
    assert raised.status_code == 201, raised.text

    took = await northgate_doctor.post(
        f"/v1/billing/patients/{patient['id']}/payments",
        json={"department": "clinic", "amount": raised.json()["totals"]["total"], "method": "cash"},
    )
    assert took.status_code == 200, took.text
    assert took.json()["remaining"] == 0


# --- the final settlement ----------------------------------------------------


async def test_money_taken_at_the_window_leaves_the_total_and_stays_visible(
    northgate, northgate_doctor
):
    """The desk's question at the door.

    The pharmacy was paid when the medicine was handed over, so what is left is
    the consultation — the pharmacy is not subtracted from the total, it was
    never in it. But it still reports what it took, or a counter that is square
    looks identical to one that never charged anything.
    """
    patient = await _new_patient(northgate, "Paid At The Window")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)
    await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=6)

    before = (await northgate.get(f"/v1/billing/patients/{patient['id']}/account")).json()
    assert before["departments"]["pharmacy"]["due"] == 60.0
    assert before["departments"]["pharmacy"]["paid"] == 0
    consultation = before["departments"]["clinic"]["due"]
    assert consultation > 0

    await northgate.post(
        f"/v1/billing/patients/{patient['id']}/invoices",
        json={"department": "pharmacy", "issue": True},
    )
    await northgate.post(
        f"/v1/billing/patients/{patient['id']}/payments",
        json={"department": "pharmacy", "amount": 60, "method": "cash"},
    )

    after = (await northgate.get(f"/v1/billing/patients/{patient['id']}/account")).json()
    pharmacy = after["departments"]["pharmacy"]
    assert pharmacy["due"] == 0
    assert pharmacy["paid"] == 60.0
    assert pharmacy["invoices"] == []

    assert after["total_due"] == consultation
    assert after["departments"]["clinic"]["due"] == consultation


async def test_a_voided_bill_counts_as_neither_owed_nor_paid(northgate, northgate_doctor):
    patient = await _new_patient(northgate, "Voided Counter")
    doctor = await _doctor(northgate)
    item = await _stocked_item(northgate)
    await _dispensed(northgate_doctor, northgate, doctor, patient, item, qty=4)

    invoice = (
        await northgate.post(
            f"/v1/billing/patients/{patient['id']}/invoices",
            json={"department": "pharmacy", "issue": True},
        )
    ).json()
    voided = await northgate.post(f"/v1/billing/invoices/{invoice['id']}/void", json=None)
    assert voided.status_code == 200, voided.text

    account = (await northgate.get(f"/v1/billing/patients/{patient['id']}/account")).json()
    pharmacy = account["departments"]["pharmacy"]
    assert pharmacy["paid"] == 0
    # voiding put the charge back, unbilled — it is owed again, not forgotten
    assert pharmacy["due"] == 40.0
    assert len(pharmacy["unbilled"]) == 1


# --- the boundaries ----------------------------------------------------------


async def test_another_clinics_patient_has_no_account_here(northgate, clearview):
    theirs = (await clearview.get("/v1/patients", params={"limit": 1})).json()["items"][0]
    assert (
        await northgate.get(f"/v1/billing/patients/{theirs['id']}/account")
    ).status_code == 404


async def test_every_clinic_can_read_its_own_patients_accounts(rowan):
    """Rowan never had billing switched on. Every clinic has it now: its
    practice admin reads a patient's account like any other clinic's would."""
    patient = (await rowan.get("/v1/patients", params={"limit": 1})).json()["items"][0]
    account = await rowan.get(f"/v1/billing/patients/{patient['id']}/account")
    assert account.status_code == 200, account.text
