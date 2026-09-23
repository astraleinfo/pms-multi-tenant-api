"""The platform catalogue — lab tests and medicine suggestions, editable
from the console. Safe to hard-delete: clinical history holds its own copy
of the name, never a foreign key to these rows."""

from __future__ import annotations


async def test_a_super_admin_can_add_edit_and_remove_a_lab_test(console):
    created = await console.post("/v1/platform/catalog/lab-tests", json={
        "code": "TEST-001", "name": "Test Panel", "panel": "General",
    })
    assert created.status_code == 201, created.text
    assert created.json() == {
        "code": "TEST-001", "name": "Test Panel", "panel": "General", "clinical_types": None,
    }

    duplicate = await console.post("/v1/platform/catalog/lab-tests", json={
        "code": "TEST-001", "name": "Again", "panel": "General",
    })
    assert duplicate.status_code == 409

    updated = await console.patch("/v1/platform/catalog/lab-tests/TEST-001", json={"name": "Renamed Panel"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Renamed Panel"

    listed = await console.get("/v1/platform/catalog/lab-tests")
    assert any(t["code"] == "TEST-001" for t in listed.json())

    deleted = await console.delete("/v1/platform/catalog/lab-tests/TEST-001")
    assert deleted.status_code == 204

    listed_after = await console.get("/v1/platform/catalog/lab-tests")
    assert not any(t["code"] == "TEST-001" for t in listed_after.json())


async def test_the_support_desk_cannot_write_lab_tests(console_support):
    create = await console_support.post("/v1/platform/catalog/lab-tests", json={
        "code": "SHOULD-FAIL", "name": "X", "panel": "General",
    })
    assert create.status_code == 403


async def test_deleting_a_lab_test_does_not_touch_history_that_used_it(northgate, console):
    """A lab order copies the test's code, name and panel onto itself —
    deleting the catalogue entry afterwards cannot orphan it."""
    added = await console.post("/v1/platform/catalog/lab-tests", json={
        "code": "CBC-DEL-TEST", "name": "Complete Blood Count", "panel": "Haematology",
    })
    assert added.status_code == 201, added.text

    doctor = (await northgate.get("/v1/users/doctors")).json()[0]
    patient = (await northgate.post("/v1/patients", json={
        "full_name": "Catalog Delete Test Patient", "dob": "1990-01-01", "sex": "female",
    })).json()
    encounter = (await northgate.post("/v1/encounters", json={
        "patient_id": patient["id"], "doctor_id": doctor["id"], "sign": False,
    })).json()
    orders = (await northgate.post(f"/v1/encounters/{encounter['id']}/lab-orders", json={
        "tests": [{"code": "CBC-DEL-TEST", "name": "Complete Blood Count", "panel": "Haematology"}],
    })).json()
    order_id = orders[0]["id"]

    deleted = await console.delete("/v1/platform/catalog/lab-tests/CBC-DEL-TEST")
    assert deleted.status_code == 204, deleted.text

    # unresulted orders are withheld unless asked for explicitly — same rule
    # `test_lab_results_are_withheld_until_verified` (test_workflows.py) checks
    still_there = (
        await northgate.get(
            f"/v1/encounters/{encounter['id']}/lab-results", params={"include_unverified": True}
        )
    ).json()
    assert still_there[0]["id"] == order_id
    assert still_there[0]["test_name"] == "Complete Blood Count"
    assert still_there[0]["test_code"] == "CBC-DEL-TEST"


async def test_a_super_admin_can_add_edit_and_remove_a_medicine(console):
    created = await console.post("/v1/platform/catalog/medicines", json={
        "medicine": "Test Medicine", "strength": "500mg", "dosage": "1 tablet",
        "frequency": "Twice daily", "duration": "5 days",
    })
    assert created.status_code == 201, created.text
    medicine_id = created.json()["id"]
    assert created.json()["medicine"] == "Test Medicine"

    updated = await console.patch(f"/v1/platform/catalog/medicines/{medicine_id}", json={"strength": "250mg"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["strength"] == "250mg"

    listed = await console.get("/v1/platform/catalog/medicines")
    assert any(m["id"] == medicine_id for m in listed.json())

    deleted = await console.delete(f"/v1/platform/catalog/medicines/{medicine_id}")
    assert deleted.status_code == 204

    listed_after = await console.get("/v1/platform/catalog/medicines")
    assert not any(m["id"] == medicine_id for m in listed_after.json())


async def test_the_support_desk_cannot_write_the_catalogue(console_support):
    response = await console_support.post("/v1/platform/catalog/medicines", json={"medicine": "X"})
    assert response.status_code == 403


async def test_a_malformed_medicine_id_is_a_404_not_a_crash(console):
    response = await console.patch("/v1/platform/catalog/medicines/not-a-uuid", json={"strength": "1mg"})
    assert response.status_code == 404
