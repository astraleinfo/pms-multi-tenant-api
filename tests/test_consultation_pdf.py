"""The printable consultation record's letterhead and patient details.

Same technique as `test_documents.py`: render the real PDF over HTTP and read
back the text reportlab actually put on the page.
"""

from __future__ import annotations

import io

from tests.conftest import sign_in
from tests.test_pricing_and_files import PNG_BYTES


def _text(pdf: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)


def _embedded_images(pdf: bytes) -> list:
    from pypdf import PdfReader

    return PdfReader(io.BytesIO(pdf)).pages[0].images


async def _a_doctor(signed) -> dict:
    return (await signed.get("/v1/users/doctors")).json()[0]


async def _a_signed_consultation(signed, *, clinical_ext: dict | None = None) -> dict:
    """Register a patient and sign one consultation for them. Returns the encounter."""
    doctor = await _a_doctor(signed)
    patient = (
        await signed.post(
            "/v1/patients",
            json={
                "full_name": "Consultation PDF Patient",
                "dob": "1988-04-11",
                "sex": "female",
                "phone": "9770090055",
                "blood_group": "O+",
                "clinical_ext": clinical_ext or {},
            },
        )
    ).json()
    encounter = (
        await signed.post(
            "/v1/encounters",
            json={
                "patient_id": patient["id"],
                "doctor_id": doctor["id"],
                "chief_complaint": "Routine check",
                "diagnosis": "Test diagnosis for the printed record",
                "sign": True,
            },
        )
    ).json()
    return encounter


async def test_the_letterhead_prints_the_clinics_address_and_phone(northgate):
    config = (await northgate.get("/v1/tenant/config")).json()
    encounter = await _a_signed_consultation(northgate)

    response = await northgate.get(f"/v1/encounters/{encounter['id']}/consultation-pdf")
    assert response.status_code == 200, response.text
    text = _text(response.content)

    assert config["contact_phone"] in text
    assert config["address"] in text


async def test_the_letterhead_prints_the_clinics_hours(northgate):
    config = (await northgate.get("/v1/tenant/config")).json()
    encounter = await _a_signed_consultation(northgate)

    text = _text(
        (await northgate.get(f"/v1/encounters/{encounter['id']}/consultation-pdf")).content
    )

    assert config["day_start"] in text
    assert config["day_end"] in text


async def test_the_patient_grid_prints_weight_when_recorded(northgate):
    encounter = await _a_signed_consultation(northgate, clinical_ext={"weight": 68.5})

    text = _text(
        (await northgate.get(f"/v1/encounters/{encounter['id']}/consultation-pdf")).content
    )

    assert "68.5" in text
    assert "WEIGHT" in text


async def test_the_patient_grid_has_no_weight_row_left_blank_looking_odd(northgate):
    """No weight recorded is not a crash and not a blank hole — same
    treatment as every other optional field on this grid."""
    encounter = await _a_signed_consultation(northgate)

    response = await northgate.get(f"/v1/encounters/{encounter['id']}/consultation-pdf")
    assert response.status_code == 200, response.text
    text = _text(response.content)

    assert "WEIGHT" in text


async def test_the_letterhead_has_no_logo_when_the_clinic_has_not_set_one(northgate):
    encounter = await _a_signed_consultation(northgate)

    response = await northgate.get(f"/v1/encounters/{encounter['id']}/consultation-pdf")
    assert response.status_code == 200, response.text
    assert len(_embedded_images(response.content)) == 0


async def test_the_letterhead_prints_the_clinics_logo_once_it_sets_one(northgate):
    uploaded = await northgate.post(
        "/v1/tenant/config/logo", files={"file": ("mark.png", PNG_BYTES, "image/png")}
    )
    assert uploaded.status_code == 200, uploaded.text
    encounter = await _a_signed_consultation(northgate)

    response = await northgate.get(f"/v1/encounters/{encounter['id']}/consultation-pdf")
    assert response.status_code == 200, response.text
    assert len(_embedded_images(response.content)) == 1

    await northgate.delete("/v1/tenant/config/logo")
