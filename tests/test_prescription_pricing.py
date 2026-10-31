"""What a prescription line costs, and whether this clinic hands it over.

A line is either dispensed here — it carries a unit price, comes out of stock
when the pharmacy hands it over and is billed at the window — or prescribed
only: the patient buys it elsewhere, so it has no price, never moves stock and
never becomes a charge. The price is never typed: a dispensed line tied to a
stock line takes that line's price as it is when the prescription is saved, and
a line typed by hand has none. A clinic that never sells medicine says so once, in its
configuration, and every line it writes starts as prescribed only.
"""

from __future__ import annotations

import uuid
from itertools import count

import pytest

_sku_seq = count(1)


@pytest.fixture
async def clean_default(northgate):
    yield
    await northgate.patch("/v1/tenant/config", json={"prescribe_only_default": False})


async def _stocked_item(signed, *, price: float = 10) -> dict:
    """A medicine on the shelf with a price on it — its own code and name each
    time: a stock code is unique within a clinic, and the same name and strength
    twice is a duplicate to the stock import, and the database is shared."""
    tag = f"{uuid.uuid4().hex[:6]}-{next(_sku_seq)}"
    opened = await signed.post(
        "/v1/inventory/items",
        json={
            "sku": f"RXP-{tag}",
            "name": f"Pricing test tablets {tag}",
            "strength": "250 mg",
            "unit": "tab",
            "category": "medicine",
            "unit_price": price,
        },
    )
    assert opened.status_code == 201, opened.text
    item = opened.json()
    stocked = await signed.post(
        "/v1/inventory/transactions",
        json={"item_id": item["id"], "type": "purchase", "qty": 100, "batch_no": "RXP-B1", "expiry": "2028-01-31"},
    )
    assert stocked.status_code == 201, stocked.text
    return item


async def _visit(signed, name: str = "Rx Pricing Patient") -> dict:
    doctor = (await signed.get("/v1/users/doctors")).json()[0]
    patient = (
        await signed.post(
            "/v1/patients", json={"full_name": name, "dob": "1984-06-01", "sex": "male", "phone": "9770090077"}
        )
    ).json()
    encounter = await signed.post(
        "/v1/encounters",
        json={"patient_id": patient["id"], "doctor_id": doctor["id"], "chief_complaint": "Sore throat"},
    )
    assert encounter.status_code == 201, encounter.text
    return encounter.json()


async def _prescribe(signed, encounter: dict, *lines: dict) -> dict:
    written = await signed.post(f"/v1/encounters/{encounter['id']}/prescriptions", json={"items": list(lines)})
    assert written.status_code == 201, written.text
    return written.json()


async def _on_hand(signed, item_id: str) -> int:
    return next(r["on_hand"] for r in (await signed.get("/v1/inventory/levels")).json() if r["item_id"] == item_id)


async def _dispense(signed, prescription: dict, line: dict, qty: int):
    return await signed.post(
        f"/v1/pharmacy/prescriptions/{prescription['id']}/dispense",
        json={"lines": [{"item_id": line["id"], "qty": qty, "batch_no": "RXP-B1"}]},
    )


async def _pharmacy_unbilled(signed, patient_id: str) -> list[dict]:
    account = (await signed.get(f"/v1/billing/patients/{patient_id}/account")).json()
    return account["departments"]["pharmacy"]["unbilled"]


# --- dispensed here -------------------------------------------------------------------


async def test_a_dispensed_line_takes_the_stock_price_draws_stock_and_bills_at_it(northgate):
    item = await _stocked_item(northgate, price=12.5)
    visit = await _visit(northgate)
    rx = await _prescribe(
        northgate, visit,
        {"medicine": "Pricing test tablets", "qty": 6, "item_id": item["id"], "dispense": True},
    )
    line = rx["items"][0]
    assert line["dispense"] is True
    assert line["unit_price"] == 12.5
    assert rx["status"] == "pending"

    before = await _on_hand(northgate, item["id"])
    handed = await _dispense(northgate, rx, line, 4)
    assert handed.status_code == 200, handed.text
    assert await _on_hand(northgate, item["id"]) == before - 4

    [charge] = await _pharmacy_unbilled(northgate, visit["patient_id"])
    assert charge["qty"] == 4
    assert charge["unit_price"] == 12.5
    assert charge["priced"] is True

    suggested = (
        await northgate.get("/v1/billing/invoices/suggestions", params={"encounter_id": visit["id"]})
    ).json()
    [pharmacy] = [s for s in suggested["lines"] if s["source"] == "pharmacy"]
    assert pharmacy["unit_price"] == 12.5 and pharmacy["qty"] == 4


@pytest.mark.parametrize("sent", [0, 0.01, 999, -5])
async def test_a_price_sent_by_the_client_is_ignored_and_the_stock_price_wins(northgate, sent):
    item = await _stocked_item(northgate, price=7.25)
    visit = await _visit(northgate)
    rx = await _prescribe(
        northgate, visit,
        {"medicine": "Pricing test tablets", "qty": 2, "item_id": item["id"], "unit_price": sent},
    )
    assert rx["items"][0]["unit_price"] == 7.25

    handed = await _dispense(northgate, rx, rx["items"][0], 2)
    assert handed.status_code == 200, handed.text
    [charge] = await _pharmacy_unbilled(northgate, visit["patient_id"])
    assert charge["unit_price"] == 7.25, "billed at the stock price, not the one the client sent"


async def test_a_stock_line_sent_without_a_price_takes_the_stock_price(northgate):
    item = await _stocked_item(northgate, price=7.25)
    rx = await _prescribe(northgate, await _visit(northgate), {"medicine": "Pricing test tablets", "item_id": item["id"]})

    assert rx["items"][0]["dispense"] is True
    assert rx["items"][0]["unit_price"] == 7.25


async def test_a_stock_price_change_before_a_re_save_is_picked_up(northgate):
    item = await _stocked_item(northgate, price=10)
    visit = await _visit(northgate)
    line = {"medicine": "Pricing test tablets", "item_id": item["id"]}
    rx = await _prescribe(northgate, visit, line)
    assert rx["items"][0]["unit_price"] == 10

    repriced = await northgate.patch(f"/v1/inventory/items/{item['id']}", json={"unit_price": 15})
    assert repriced.status_code == 200, repriced.text
    # Nothing saved yet: the line keeps the price it was written at.
    stored = (await northgate.get(f"/v1/prescriptions/{rx['id']}")).json()
    assert stored["items"][0]["unit_price"] == 10

    resaved = await northgate.put(f"/v1/encounters/{visit['id']}/prescription", json={"items": [line]})
    assert resaved.status_code == 200, resaved.text
    assert resaved.json()["items"][0]["unit_price"] == 15


async def test_a_medicine_typed_by_hand_has_no_price_whatever_the_client_sends(northgate):
    visit = await _visit(northgate)
    rx = await _prescribe(northgate, visit, {"medicine": "Compounded cream", "qty": 2, "unit_price": 40})
    line = rx["items"][0]
    assert line["item_id"] is None and line["dispense"] is True
    assert line["unit_price"] is None

    # Dispensed, it is still unpriced at the counter, as before lines carried a price.
    handed = await _dispense(northgate, rx, line, 2)
    assert handed.status_code == 200, handed.text
    [charge] = await _pharmacy_unbilled(northgate, visit["patient_id"])
    assert charge["unit_price"] == 0 and charge["priced"] is False


async def test_a_free_text_medicine_with_no_price_stays_unpriced(northgate):
    rx = await _prescribe(northgate, await _visit(northgate), {"medicine": "Something typed by hand"})

    assert rx["items"][0]["unit_price"] is None


# --- prescribed only ----------------------------------------------------------------


async def test_a_prescribe_only_line_has_no_price_and_is_never_dispensed_or_billed(northgate):
    item = await _stocked_item(northgate, price=10)
    visit = await _visit(northgate)
    rx = await _prescribe(
        northgate, visit,
        {"medicine": "Pricing test tablets", "qty": 5, "item_id": item["id"], "dispense": False, "unit_price": 99},
    )
    line = rx["items"][0]
    assert line["dispense"] is False
    assert line["unit_price"] is None, "prescribe-only stores no price, though its stock line has one"
    assert line["status"] == "prescribe_only"
    assert rx["status"] == "prescribe_only"

    before = await _on_hand(northgate, item["id"])
    refused = await _dispense(northgate, rx, line, 1)
    assert refused.status_code == 409
    assert await _on_hand(northgate, item["id"]) == before

    assert await _pharmacy_unbilled(northgate, visit["patient_id"]) == []
    suggested = (
        await northgate.get("/v1/billing/invoices/suggestions", params={"encounter_id": visit["id"]})
    ).json()
    assert [s for s in suggested["lines"] if s["source"] == "pharmacy"] == []

    waiting = (await northgate.get("/v1/pharmacy/prescriptions")).json()
    assert rx["id"] not in {r["id"] for r in waiting}, "nothing on it for the pharmacy to do"


async def test_a_free_text_medicine_can_be_prescribed_only(northgate):
    rx = await _prescribe(
        northgate, await _visit(northgate), {"medicine": "Imported eye drops", "dispense": False}
    )

    assert rx["items"][0]["dispense"] is False
    assert rx["items"][0]["unit_price"] is None


async def test_a_mixed_prescription_is_finished_when_its_dispensed_lines_are(northgate):
    item = await _stocked_item(northgate)
    visit = await _visit(northgate)
    rx = await _prescribe(
        northgate, visit,
        {"medicine": "Pricing test tablets", "qty": 3, "item_id": item["id"], "dispense": True},
        {"medicine": "Imported eye drops", "dispense": False},
    )
    assert rx["status"] == "pending"
    sold, elsewhere = rx["items"]

    handed = await _dispense(northgate, rx, sold, 3)
    assert handed.status_code == 200, handed.text
    after = handed.json()
    assert after["status"] == "dispensed", "the line bought elsewhere is not waiting on the pharmacy"
    assert after["items"][1]["status"] == "prescribe_only"
    assert after["items"][1]["dispensed_qty"] == 0

    [charge] = await _pharmacy_unbilled(northgate, visit["patient_id"])
    assert charge["ref_id"] == sold["id"]


async def test_rewriting_a_prescription_keeps_each_lines_choice(northgate):
    visit = await _visit(northgate)
    await _prescribe(northgate, visit, {"medicine": "Paracetamol"})

    rewritten = await northgate.put(
        f"/v1/encounters/{visit['id']}/prescription",
        json={"items": [{"medicine": "Paracetamol", "dispense": False}]},
    )
    assert rewritten.status_code == 200, rewritten.text
    assert rewritten.json()["status"] == "prescribe_only"
    assert rewritten.json()["items"][0]["unit_price"] is None


# --- the clinic's default ---------------------------------------------------------------


async def test_the_clinic_default_starts_as_dispensing(northgate):
    assert (await northgate.get("/v1/tenant/config")).json()["prescribe_only_default"] is False


async def test_a_prescribe_only_clinic_writes_lines_the_patient_buys_elsewhere(northgate, clean_default):
    saved = await northgate.patch("/v1/tenant/config", json={"prescribe_only_default": True})
    assert saved.status_code == 200, saved.text
    assert saved.json()["prescribe_only_default"] is True

    item = await _stocked_item(northgate)
    rx = await _prescribe(
        northgate, await _visit(northgate),
        {"medicine": "Paracetamol"},
        {"medicine": "Pricing test tablets", "item_id": item["id"], "dispense": True},
    )
    unsaid, overridden = rx["items"]
    assert unsaid["dispense"] is False and unsaid["unit_price"] is None
    assert overridden["dispense"] is True, "a line that says otherwise overrides the default"
    assert overridden["unit_price"] == 10


async def test_a_doctor_cannot_change_the_clinic_default(northgate_doctor):
    refused = await northgate_doctor.patch("/v1/tenant/config", json={"prescribe_only_default": True})
    assert refused.status_code == 403


# --- rows written before lines had a price -----------------------------------------------


def _pricing_migration():
    """The migration module, loaded by path: `alembic/versions` is not a
    package, and its file name starts with a digit."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0031_prescription_pricing.py"
    spec = importlib.util.spec_from_file_location("prescription_pricing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_a_line_from_before_prices_reads_as_dispensed_and_takes_the_shelf_price(northgate):
    from sqlalchemy import text

    from app.db.session import tenant_session

    item = await _stocked_item(northgate, price=8)
    rx = await _prescribe(
        northgate, await _visit(northgate),
        {"medicine": "Pricing test tablets", "item_id": item["id"]},
        {"medicine": "Typed by hand"},
    )
    tenant_id = uuid.UUID((await northgate.get("/v1/tenant/config")).json()["tenant_id"])

    # As the row stood before the upgrade: no price, and the flag at its column default.
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                "UPDATE prescription_items SET unit_price = NULL, dispense = DEFAULT"
                " WHERE prescription_id = :rx"
            ),
            {"rx": rx["id"]},
        )
        await session.execute(text(_pricing_migration().BACKFILL_PRICES))

    again = (await northgate.get(f"/v1/prescriptions/{rx['id']}")).json()
    stocked, typed = again["items"]
    assert stocked["dispense"] is True and stocked["unit_price"] == 8
    assert typed["dispense"] is True and typed["unit_price"] is None, "nothing to take a price from"
    assert again["status"] == "pending"
