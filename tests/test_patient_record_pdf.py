"""The printable patient record — a document built from the record, not a print of the screen.

Same technique as `test_documents.py`: render the real PDF over HTTP and read
back the text reportlab actually put on the page.
"""

from __future__ import annotations

from tests.conftest import patients_of, sign_in
from tests.test_consultation_pdf import _embedded_images, _text
from tests.test_pricing_and_files import PNG_BYTES


async def _a_patient_with_lab_and_billing(signed) -> dict:
    """A seeded patient whose history has both a lab order and an invoice."""
    for patient in await patients_of(signed, limit=200):
        kinds = {e["kind"] for e in (await signed.get(f"/v1/patients/{patient['id']}/history")).json()}
        if {"lab", "invoice"} <= kinds:
            return patient
    raise AssertionError("the seed has no patient with both a lab order and an invoice")


async def test_the_record_prints_the_clinic_the_patient_and_their_history(northgate):
    patient = await _a_patient_with_lab_and_billing(northgate)

    response = await northgate.get(f"/v1/patients/{patient['id']}/record.pdf")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    text = _text(response.content)

    assert "Northgate" in text
    assert "PATIENT RECORD" in text
    assert patient["full_name"] in text
    assert patient["mrn"] in text
    for section in ("DETAILS", "CONSULTATIONS", "LAB ORDERS"):
        assert section in text
    # a clinical record: billing is printed on invoices, not here
    assert "ACCOUNT" not in text
    assert "INV-" not in text
    assert "Page 1 of" in text


async def test_a_role_without_the_lab_is_not_printed_its_results(client, northgate):
    """Reception cannot read the lab — and the paper follows the role."""
    patient = await _a_patient_with_lab_and_billing(northgate)
    reception = await sign_in(client, "northgate_reception")

    response = await reception.get(f"/v1/patients/{patient['id']}/record.pdf")
    assert response.status_code == 200, response.text
    text = _text(response.content)
    assert "LAB ORDERS" not in text
    assert "CONSULTATIONS" in text


async def test_the_record_carries_the_clinics_logo_once_it_sets_one(northgate):
    patient = (await patients_of(northgate))[0]

    plain = await northgate.get(f"/v1/patients/{patient['id']}/record.pdf")
    assert plain.status_code == 200, plain.text
    assert len(_embedded_images(plain.content)) == 0  # the initials are drawn, not embedded

    uploaded = await northgate.post(
        "/v1/tenant/config/logo", files={"file": ("mark.png", PNG_BYTES, "image/png")}
    )
    assert uploaded.status_code == 200, uploaded.text
    try:
        for url in (
            f"/v1/patients/{patient['id']}/record.pdf",
            "/v1/analytics/report.pdf",
        ):
            response = await northgate.get(url)
            assert response.status_code == 200, response.text
            assert len(_embedded_images(response.content)) == 1, url
    finally:
        await northgate.delete("/v1/tenant/config/logo")


async def test_another_clinics_patient_cannot_be_printed(northgate, clearview):
    patient = (await patients_of(northgate))[0]
    response = await clearview.get(f"/v1/patients/{patient['id']}/record.pdf")
    assert response.status_code == 404
