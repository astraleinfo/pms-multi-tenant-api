"""The tariff, the two counters that bill separately, and the files a lab
order collects.

Between them these cover the four decisions those features rest on: a price is
the clinic's to set, a scan carries its own amount, an invoice belongs to the
counter that raised it, and a report is tenant-owned data reached through its
order.
"""

from __future__ import annotations

import pytest

from tests.conftest import audit_lines, sign_in


async def _doctor(signed) -> dict:
    return (await signed.get("/v1/users/doctors")).json()[0]


async def _patient(signed) -> dict:
    return (await signed.get("/v1/patients", params={"limit": 1})).json()["items"][0]


# --- the tariff (§3.12) ------------------------------------------------------


async def test_a_clinic_starts_with_a_tariff_it_can_read(northgate):
    prices = (await northgate.get("/v1/pricing/services")).json()
    kinds = {row["kind"] for row in prices}
    assert {"consultation", "procedure", "lab"} <= kinds

    consultation = next(row for row in prices if row["kind"] == "consultation")
    assert consultation["code"] == "CONSULTATION"
    assert consultation["price"] > 0


async def test_each_scan_carries_its_own_amount(northgate):
    """The point of a per-code tariff: a cone beam CT is not a bitewing."""
    lab = {row["code"]: row["price"] for row in (await northgate.get(
        "/v1/pricing/services", params={"kind": "lab"}
    )).json()}

    assert lab["CBCT"] != lab["BW"]
    assert len({*lab.values()}) > 1


async def test_only_an_admin_may_set_a_price(northgate, northgate_doctor):
    """Reading a price is everyone's business; deciding one is the admin's."""
    assert (await northgate_doctor.get("/v1/pricing/services")).status_code == 200

    refused = await northgate_doctor.post(
        "/v1/pricing/services",
        json={"kind": "procedure", "code": "WHITEN", "name": "Whitening", "price": 4000},
    )
    assert refused.status_code == 403

    allowed = await northgate.post(
        "/v1/pricing/services",
        json={"kind": "procedure", "code": "WHITEN", "name": "Whitening", "price": 4000},
    )
    assert allowed.status_code == 201
    assert allowed.json()["price"] == 4000


async def test_a_price_can_be_edited_and_retired(northgate):
    created = (
        await northgate.post(
            "/v1/pricing/services",
            json={"kind": "other", "code": "REPORT", "name": "Medical report", "price": 250},
        )
    ).json()

    edited = await northgate.patch(
        f"/v1/pricing/services/{created['id']}", json={"price": 400, "active": False}
    )
    assert edited.status_code == 200
    assert edited.json()["price"] == 400
    assert edited.json()["active"] is False

    only_offered = (
        await northgate.get("/v1/pricing/services", params={"active": True})
    ).json()
    assert created["id"] not in {row["id"] for row in only_offered}

    assert (await northgate.delete(f"/v1/pricing/services/{created['id']}")).status_code == 204
    remaining = (await northgate.get("/v1/pricing/services")).json()
    assert created["id"] not in {row["id"] for row in remaining}


async def test_the_same_code_cannot_be_priced_twice(northgate):
    body = {"kind": "procedure", "code": "DUP", "name": "Duplicate", "price": 100}
    assert (await northgate.post("/v1/pricing/services", json=body)).status_code == 201

    again = await northgate.post("/v1/pricing/services", json=body)
    assert again.status_code == 409
    assert "already" in again.json()["error"]["message"]


async def test_two_clinics_price_the_same_test_differently(northgate, clearview):
    """The tariff is tenant-owned, which is the whole point of it being a row."""
    ours = {r["code"] for r in (await northgate.get("/v1/pricing/services", params={"kind": "lab"})).json()}
    theirs = {r["code"] for r in (await clearview.get("/v1/pricing/services", params={"kind": "lab"})).json()}

    # Each clinic is priced for the tests its own discipline can order.
    assert "CBCT" in ours and "CBCT" not in theirs
    assert "OCT" in theirs and "OCT" not in ours


async def test_importing_the_lab_catalogue_only_adds_what_is_missing(rowan):
    before = (await rowan.get("/v1/pricing/services", params={"kind": "lab"})).json()

    added = await rowan.post(
        "/v1/pricing/services/import-lab-catalogue", json={"price": 999}
    )
    assert added.status_code == 201
    assert added.json() == [], "everything was already priced by the seed"

    after = (await rowan.get("/v1/pricing/services", params={"kind": "lab"})).json()
    assert len(after) == len(before)
    assert {r["price"] for r in after} == {r["price"] for r in before}


async def test_setting_a_price_is_recorded(northgate):
    created = (
        await northgate.post(
            "/v1/pricing/services",
            json={"kind": "other", "code": "AUDITED", "name": "Audited price", "price": 10},
        )
    ).json()

    trail = await audit_lines(northgate.tenant_id, action="pricing.create")
    assert any(row["entity_id"] == created["id"] for row in trail)


# --- what the visit suggests -------------------------------------------------


async def test_a_suggested_line_quotes_the_tariff_not_a_number_in_the_code(northgate):
    encounter = (await northgate.get("/v1/encounters", params={"limit": 1})).json()["items"][0]
    lines = (
        await northgate.get("/v1/billing/invoices/suggestions", params={"encounter_id": encounter["id"]})
    ).json()["lines"]

    consultation = next(line for line in lines if line["source"] == "consultation")
    tariff = next(
        row
        for row in (await northgate.get("/v1/pricing/services", params={"kind": "consultation"})).json()
        if row["code"] == "CONSULTATION"
    )
    assert consultation["unit_price"] == tariff["price"]
    assert consultation["priced"] is True
    assert consultation["department"] == "clinic"


async def test_a_lab_line_is_priced_by_its_own_code(northgate, northgate_doctor):
    patient = await _patient(northgate)
    doctor = await _doctor(northgate)
    encounter = (
        await northgate_doctor.post(
            "/v1/encounters",
            json={
                "patient_id": patient["id"],
                "doctor_id": doctor["id"],
                "date": "2026-09-01",
                "chief_complaint": "Priced lab line",
                "notes": "",
                "diagnosis": "",
                "clinical_ext": {},
            },
        )
    ).json()
    await northgate_doctor.post(
        f"/v1/encounters/{encounter['id']}/lab-orders",
        json={"tests": [{"code": "CBCT", "name": "Cone beam CT", "panel": "Imaging"}]},
    )

    lines = (
        await northgate.get("/v1/billing/invoices/suggestions", params={"encounter_id": encounter["id"]})
    ).json()["lines"]
    lab_line = next(line for line in lines if line["source"] == "lab")
    cbct = next(
        row
        for row in (await northgate.get("/v1/pricing/services", params={"kind": "lab"})).json()
        if row["code"] == "CBCT"
    )
    assert lab_line["unit_price"] == cbct["price"]
    assert lab_line["department"] == "lab"


async def test_an_unpriced_line_says_so_rather_than_guessing(northgate, northgate_doctor):
    """Zero and honest beats a plausible number nobody agreed to."""
    consultation = next(
        row
        for row in (await northgate.get("/v1/pricing/services", params={"kind": "consultation"})).json()
        if row["code"] == "CONSULTATION"
    )
    await northgate.patch(f"/v1/pricing/services/{consultation['id']}", json={"active": False})
    try:
        encounter = (await northgate.get("/v1/encounters", params={"limit": 1})).json()["items"][0]
        lines = (
            await northgate.get(
                "/v1/billing/invoices/suggestions", params={"encounter_id": encounter["id"]}
            )
        ).json()["lines"]
        line = next(line for line in lines if line["source"] == "consultation")
        assert line["unit_price"] == 0
        assert line["priced"] is False
    finally:
        await northgate.patch(f"/v1/pricing/services/{consultation['id']}", json={"active": True})


# --- lab and pharmacy bill separately ---------------------------------------


async def test_an_invoice_of_lab_lines_belongs_to_the_lab(northgate):
    patient = await _patient(northgate)
    invoice = (
        await northgate.post(
            "/v1/billing/invoices",
            json={
                "patient_id": patient["id"],
                "line_items": [
                    {"description": "Lab — Cone beam CT", "source": "lab", "qty": 1, "unit_price": 3500}
                ],
                "issue": True,
            },
        )
    ).json()
    assert invoice["department"] == "lab"


async def test_an_invoice_of_dispensed_medicine_belongs_to_the_pharmacy(northgate):
    patient = await _patient(northgate)
    invoice = (
        await northgate.post(
            "/v1/billing/invoices",
            json={
                "patient_id": patient["id"],
                "line_items": [
                    {"description": "Amoxicillin 500mg", "source": "pharmacy", "qty": 10, "unit_price": 6}
                ],
                "issue": True,
            },
        )
    ).json()
    assert invoice["department"] == "pharmacy"


async def test_a_mixed_invoice_stays_with_the_clinic(northgate):
    patient = await _patient(northgate)
    invoice = (
        await northgate.post(
            "/v1/billing/invoices",
            json={
                "patient_id": patient["id"],
                "line_items": [
                    {"description": "Consultation", "source": "consultation", "qty": 1, "unit_price": 500},
                    {"description": "Lab — FBC", "source": "lab", "qty": 1, "unit_price": 350},
                ],
                "issue": True,
            },
        )
    ).json()
    assert invoice["department"] == "clinic"


async def test_a_department_can_be_named_outright(northgate):
    patient = await _patient(northgate)
    invoice = (
        await northgate.post(
            "/v1/billing/invoices",
            json={
                "patient_id": patient["id"],
                "department": "lab",
                "line_items": [
                    {"description": "Reporting fee", "source": "manual", "qty": 1, "unit_price": 200}
                ],
            },
        )
    ).json()
    assert invoice["department"] == "lab"


async def test_the_ledger_can_be_read_one_counter_at_a_time(northgate):
    patient = await _patient(northgate)
    await northgate.post(
        "/v1/billing/invoices",
        json={
            "patient_id": patient["id"],
            "line_items": [{"description": "Scan", "source": "lab", "qty": 1, "unit_price": 900}],
            "issue": True,
        },
    )

    page = (await northgate.get("/v1/billing/invoices", params={"department": "lab"})).json()
    assert page["total"] >= 1
    assert {row["department"] for row in page["items"]} == {"lab"}

    everything = (await northgate.get("/v1/billing/invoices")).json()
    assert everything["total"] > page["total"]
    # The tab counts describe every counter, whichever one is on screen.
    assert everything["counts"]["department:lab"] == page["total"]


# --- the files a lab order collects ------------------------------------------

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


async def _fresh_order(signed_admin, signed_doctor) -> dict:
    patient = await _patient(signed_admin)
    doctor = await _doctor(signed_admin)
    encounter = (
        await signed_doctor.post(
            "/v1/encounters",
            json={
                "patient_id": patient["id"],
                "doctor_id": doctor["id"],
                "date": "2026-09-02",
                "chief_complaint": "Report upload",
                "notes": "",
                "diagnosis": "",
                "clinical_ext": {},
            },
        )
    ).json()
    orders = (
        await signed_doctor.post(
            f"/v1/encounters/{encounter['id']}/lab-orders",
            json={"tests": [{"code": "OPG", "name": "Orthopantomogram", "panel": "Imaging"}]},
        )
    ).json()
    return orders[0]


async def test_a_report_can_be_uploaded_listed_and_read_back(client, northgate, northgate_doctor):
    order = await _fresh_order(northgate, northgate_doctor)

    uploaded = await northgate.post(
        f"/v1/lab/orders/{order['id']}/attachments",
        files={"file": ("scan.png", PNG_BYTES, "image/png")},
        data={"note": "Panoramic view"},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["filename"] == "scan.png"
    assert body["size_bytes"] == len(PNG_BYTES)
    assert body["note"] == "Panoramic view"

    listed = (await northgate.get(f"/v1/lab/orders/{order['id']}/attachments")).json()
    assert [row["id"] for row in listed] == [body["id"]]

    downloaded = await northgate.get(f"/v1/lab/orders/{order['id']}/attachments/{body['id']}")
    assert downloaded.status_code == 200
    assert downloaded.content == PNG_BYTES
    assert downloaded.headers["content-type"].startswith("image/png")


async def test_a_report_can_be_removed_while_the_order_is_open(northgate, northgate_doctor):
    order = await _fresh_order(northgate, northgate_doctor)
    body = (
        await northgate.post(
            f"/v1/lab/orders/{order['id']}/attachments",
            files={"file": ("draft.pdf", b"%PDF-1.4 draft", "application/pdf")},
        )
    ).json()

    assert (
        await northgate.delete(f"/v1/lab/orders/{order['id']}/attachments/{body['id']}")
    ).status_code == 204
    assert (await northgate.get(f"/v1/lab/orders/{order['id']}/attachments")).json() == []


@pytest.mark.parametrize(
    "name,content,kind",
    [
        ("payload.exe", b"MZ\x90\x00", "application/x-msdownload"),
        ("script.html", b"<script>alert(1)</script>", "text/html"),
    ],
)
async def test_only_the_kinds_a_lab_actually_files_are_accepted(
    northgate, northgate_doctor, name, content, kind
):
    order = await _fresh_order(northgate, northgate_doctor)
    refused = await northgate.post(
        f"/v1/lab/orders/{order['id']}/attachments", files={"file": (name, content, kind)}
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "validation_failed"


async def test_an_empty_file_is_refused(northgate, northgate_doctor):
    order = await _fresh_order(northgate, northgate_doctor)
    refused = await northgate.post(
        f"/v1/lab/orders/{order['id']}/attachments",
        files={"file": ("nothing.pdf", b"", "application/pdf")},
    )
    assert refused.status_code == 422


async def test_a_verified_order_takes_no_more_paper(northgate, northgate_doctor):
    order = await _fresh_order(northgate, northgate_doctor)
    await northgate.post(f"/v1/lab/orders/{order['id']}/start")
    await northgate.post(
        f"/v1/lab/orders/{order['id']}/results",
        json={"results": [{"analyte": "Report", "value": "Normal", "unit": "", "reference": "", "flag": "normal"}]},
    )
    await northgate.patch(f"/v1/lab/orders/{order['id']}", json={"status": "verified"})

    refused = await northgate.post(
        f"/v1/lab/orders/{order['id']}/attachments",
        files={"file": ("late.pdf", b"%PDF-1.4 late", "application/pdf")},
    )
    assert refused.status_code == 409
    assert "verified" in refused.json()["error"]["message"]


async def test_another_clinic_cannot_reach_a_report(northgate, northgate_doctor, rowan, clearview):
    """Rowan runs a lab of its own, so the module guard lets it through and the
    tenant boundary is what answers. Clearview has no lab at all, and never
    gets that far."""
    order = await _fresh_order(northgate, northgate_doctor)
    body = (
        await northgate.post(
            f"/v1/lab/orders/{order['id']}/attachments",
            files={"file": ("ours.pdf", b"%PDF-1.4 ours", "application/pdf")},
        )
    ).json()

    # 404 rather than 403 — a 403 would confirm the row exists.
    assert (
        await rowan.get(f"/v1/lab/orders/{order['id']}/attachments/{body['id']}")
    ).status_code == 404
    assert (await rowan.get(f"/v1/lab/orders/{order['id']}/attachments")).status_code == 404

    # And a clinic without the module is stopped one step earlier.
    assert (await clearview.get(f"/v1/lab/orders/{order['id']}/attachments")).status_code == 403


async def test_uploading_a_report_is_recorded(northgate, northgate_doctor):
    order = await _fresh_order(northgate, northgate_doctor)
    body = (
        await northgate.post(
            f"/v1/lab/orders/{order['id']}/attachments",
            files={"file": ("trail.pdf", b"%PDF-1.4 trail", "application/pdf")},
        )
    ).json()

    trail = await audit_lines(northgate.tenant_id, action="lab.attachment.add")
    entry = next(row for row in trail if row["entity_id"] == body["id"])
    assert entry["meta"]["filename"] == "trail.pdf"


async def test_a_lab_technician_may_file_a_report_and_reception_may_not(client, northgate):
    """The upload asks for `lab:result` — the permission that already governs
    typing an analyte into the same order."""
    from app.core.permissions import Permission, role_has

    assert role_has("lab_tech", Permission.LAB_RESULT)
    assert not role_has("receptionist", Permission.LAB_RESULT)


# --- stock lines and how they are priced (§3.11) -----------------------------


async def test_a_stock_line_can_be_opened_and_starts_at_nothing(northgate):
    created = await northgate.post(
        "/v1/inventory/items",
        json={
            "sku": "med-new-100",
            "name": "Newly stocked syrup",
            "form": "Syrup",
            "strength": "100 ml",
            "unit": "bottle",
            "category": "medicine",
            "reorder_level": 5,
            "unit_price": 90,
        },
    )
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["sku"] == "MED-NEW-100", "codes are matched, so they are stored upper case"
    assert item["unit_price"] == 90

    levels = (await northgate.get("/v1/inventory/levels")).json()
    ours = next(row for row in levels if row["item_id"] == item["id"])
    assert ours["on_hand"] == 0


async def test_two_stock_lines_cannot_share_a_code(northgate):
    body = {"sku": "DUP-SKU", "name": "First", "category": "consumable"}
    assert (await northgate.post("/v1/inventory/items", json=body)).status_code == 201

    again = await northgate.post("/v1/inventory/items", json={**body, "name": "Second"})
    assert again.status_code == 409


async def test_a_price_can_be_a_margin_over_cost(northgate):
    item = (
        await northgate.post(
            "/v1/inventory/items",
            json={
                "sku": "MARGIN-1",
                "name": "Priced on margin",
                "category": "medicine",
                "pricing_mode": "percent",
                "cost_price": 12,
                "margin_pct": 25,
            },
        )
    ).json()
    assert item["unit_price"] == 15.0
    assert item["pricing_mode"] == "percent"


async def test_a_price_can_be_the_mrp_less_a_discount(northgate):
    item = (
        await northgate.post(
            "/v1/inventory/items",
            json={
                "sku": "MRP-1",
                "name": "Priced off MRP",
                "category": "medicine",
                "pricing_mode": "mrp",
                "mrp": 100,
                "margin_pct": 10,
            },
        )
    ).json()
    assert item["unit_price"] == 90.0
    assert item["mrp"] == 100.0


async def test_changing_the_cost_moves_a_margin_price_with_it(northgate):
    item = (
        await northgate.post(
            "/v1/inventory/items",
            json={
                "sku": "MARGIN-2",
                "name": "Follows its cost",
                "category": "medicine",
                "pricing_mode": "percent",
                "cost_price": 10,
                "margin_pct": 50,
            },
        )
    ).json()
    assert item["unit_price"] == 15.0

    edited = (
        await northgate.patch(f"/v1/inventory/items/{item['id']}", json={"cost_price": 20})
    ).json()
    assert edited["unit_price"] == 30.0, "the price follows the mode, not a figure someone retyped"


async def test_a_delivery_can_reprice_the_line_it_delivers(northgate):
    item = (
        await northgate.post(
            "/v1/inventory/items",
            json={
                "sku": "DELIVERY-1",
                "name": "Repriced on arrival",
                "category": "medicine",
                "unit_price": 5,
            },
        )
    ).json()

    movement = await northgate.post(
        "/v1/inventory/transactions",
        json={
            "item_id": item["id"],
            "type": "purchase",
            "qty": 50,
            "batch_no": "DEL-1",
            "expiry": "2027-12-31",
            "pricing_mode": "mrp",
            "mrp": 40,
            "margin_pct": 5,
            "cost_price": 22,
        },
    )
    assert movement.status_code == 201, movement.text

    after = next(
        row for row in (await northgate.get("/v1/inventory/items")).json() if row["id"] == item["id"]
    )
    assert after["unit_price"] == 38.0
    assert after["cost_price"] == 22.0
    assert after["pricing_mode"] == "mrp"


async def test_a_movement_with_no_pricing_leaves_the_price_alone(northgate):
    item = (
        await northgate.post(
            "/v1/inventory/items",
            json={"sku": "QUIET-1", "name": "Left alone", "category": "medicine", "unit_price": 7},
        )
    ).json()

    await northgate.post(
        "/v1/inventory/transactions",
        json={"item_id": item["id"], "type": "purchase", "qty": 5, "batch_no": "Q-1", "expiry": "2027-01-31"},
    )

    after = next(
        row for row in (await northgate.get("/v1/inventory/items")).json() if row["id"] == item["id"]
    )
    assert after["unit_price"] == 7.0


async def test_opening_a_stock_line_needs_the_write_permission(northgate_doctor):
    """A clinician reads stock; the pharmacy changes it."""
    refused = await northgate_doctor.post(
        "/v1/inventory/items", json={"sku": "NOPE-1", "name": "Not allowed", "category": "medicine"}
    )
    assert refused.status_code == 403


async def test_another_clinics_stock_line_cannot_be_edited(northgate, clearview):
    theirs = (await clearview.get("/v1/inventory/items")).json()[0]
    refused = await northgate.patch(f"/v1/inventory/items/{theirs['id']}", json={"unit_price": 1})
    assert refused.status_code == 404


async def test_a_pharmacist_can_open_and_price_a_line(client):
    """The role that actually runs the shelf — not just the practice admin."""
    from app.core.permissions import Permission, role_has

    assert role_has("pharmacist", Permission.INVENTORY_WRITE)
    assert not role_has("lab_tech", Permission.INVENTORY_WRITE)


async def test_the_new_stock_routes_are_behind_the_module(rowan):
    """A clinic without inventory gets 403 from the guard, not a 404 or a row.

    Rowan Skin has not bought it, which is what makes this worth asserting.
    """
    features = {f["feature_key"]: f["status"] for f in (await rowan.get("/v1/tenant/features")).json()}
    if features.get("inventory") == "active":
        pytest.skip("this clinic has bought inventory")

    refused = await rowan.post(
        "/v1/inventory/items", json={"sku": "X-1", "name": "Blocked", "category": "medicine"}
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "feature_not_enabled"


async def test_signing_in_as_a_second_clinic_still_sees_only_its_own_tariff(client):
    northgate = await sign_in(client, "northgate_admin")
    clearview = await sign_in(client, "clearview_admin")

    ours = {row["id"] for row in (await northgate.get("/v1/pricing/services")).json()}
    theirs = {row["id"] for row in (await clearview.get("/v1/pricing/services")).json()}
    assert ours and theirs
    assert ours.isdisjoint(theirs)


async def test_switching_how_a_line_is_priced_re_derives_the_price(northgate):
    """The mode is the decision; the price follows from it."""
    item = (
        await northgate.post(
            "/v1/inventory/items",
            json={
                "sku": "SWITCH-1",
                "name": "Changes its mind",
                "category": "medicine",
                "unit_price": 5,
                "cost_price": 20,
                "mrp": 60,
            },
        )
    ).json()
    assert item["unit_price"] == 5.0

    on_margin = (
        await northgate.patch(
            f"/v1/inventory/items/{item['id']}",
            json={"pricing_mode": "percent", "margin_pct": 40},
        )
    ).json()
    assert on_margin["unit_price"] == 28.0

    off_mrp = (
        await northgate.patch(
            f"/v1/inventory/items/{item['id']}",
            json={"pricing_mode": "mrp", "margin_pct": 20},
        )
    ).json()
    assert off_mrp["unit_price"] == 48.0


async def test_editing_the_rest_of_a_line_leaves_its_price_alone(northgate):
    item = (
        await northgate.post(
            "/v1/inventory/items",
            json={
                "sku": "RENAME-1",
                "name": "Before",
                "category": "consumable",
                "unit_price": 12.5,
                "reorder_level": 3,
            },
        )
    ).json()

    edited = (
        await northgate.patch(
            f"/v1/inventory/items/{item['id']}",
            json={"name": "After", "category": "implant", "reorder_level": 9, "unit": "box"},
        )
    ).json()
    assert edited["name"] == "After"
    assert edited["category"] == "implant"
    assert edited["reorder_level"] == 9
    assert edited["unit"] == "box"
    assert edited["unit_price"] == 12.5, "a rename is not a repricing"


async def test_a_stock_line_is_always_counted_in_something(northgate):
    """Blanking the unit falls back rather than leaving "12 " on every screen
    that quotes a level."""
    item = (
        await northgate.post(
            "/v1/inventory/items",
            json={"sku": "UNITLESS-1", "name": "No unit given", "category": "consumable", "unit": "   "},
        )
    ).json()
    assert item["unit"] == "unit"

    blanked = (
        await northgate.patch(f"/v1/inventory/items/{item['id']}", json={"unit": ""})
    ).json()
    assert blanked["unit"] == "unit"

    named = (
        await northgate.patch(f"/v1/inventory/items/{item['id']}", json={"unit": " box "})
    ).json()
    assert named["unit"] == "box"
