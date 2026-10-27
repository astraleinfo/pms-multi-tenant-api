"""The printable consultation record's letterhead and patient details.

Same technique as `test_documents.py`: render the real PDF over HTTP and read
back the text reportlab actually put on the page.
"""

from __future__ import annotations

import io

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

    assert "Weight" in text
    assert "68.5 kg" in text


async def test_the_patient_box_has_no_weight_label_when_none_is_recorded(northgate):
    """No weight recorded is not a crash and not a label with nothing after it —
    same treatment as every other optional field in the box."""
    encounter = await _a_signed_consultation(northgate)

    response = await northgate.get(f"/v1/encounters/{encounter['id']}/consultation-pdf")
    assert response.status_code == 200, response.text
    text = _text(response.content)

    assert "weight" not in text.lower()


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


# --- the hospital-form consultation ------------------------------------------------


async def _consultation(signed, *, patient=None, encounter=None, items=None) -> dict:
    """A patient, a signed consultation for them, and — when `items` are given — its prescription."""
    doctor = await _a_doctor(signed)
    made = (
        await signed.post(
            "/v1/patients",
            json={
                "full_name": "Consultation PDF Patient", "dob": "1988-04-11", "sex": "female",
                "phone": "9770090055", "blood_group": "O+", **(patient or {}),
            },
        )
    ).json()
    visit = (
        await signed.post(
            "/v1/encounters",
            json={
                "patient_id": made["id"], "doctor_id": doctor["id"],
                "chief_complaint": "Dry cough for six days", "diagnosis": "Acute bronchitis",
                "sign": True, **(encounter or {}),
            },
        )
    ).json()
    assert "id" in visit, visit
    if items:
        rx = await signed.post(f"/v1/encounters/{visit['id']}/prescriptions", json={"items": items})
        assert rx.status_code == 201, rx.text
    return visit


async def _printed(signed, visit: dict) -> str:
    response = await signed.get(f"/v1/encounters/{visit['id']}/consultation-pdf")
    assert response.status_code == 200, response.text
    return _text(response.content)


_AZITHROMYCIN = {
    "medicine": "Azithromycin", "strength": "500 mg", "dosage": "1 tablet",
    "frequency": "Once daily", "duration": "3 days", "instructions": "After food", "qty": 3,
}


async def test_the_consultation_reads_evidence_first(northgate):
    visit = await _consultation(
        northgate,
        patient={"allergies": "Penicillin (rash)"},
        encounter={"notes": "Chest clear except scattered wheeze.", "follow_up_days": 7},
        items=[_AZITHROMYCIN],
    )

    text = await _printed(northgate, visit)

    order = ["OPD CONSULTATION SUMMARY", "HISTORY", "EXAMINATION", "DIAGNOSIS", "MEDICATION ADVISED", "FOLLOW-UP"]
    positions = [text.index(heading) for heading in order]
    assert positions == sorted(positions), dict(zip(order, positions, strict=True))
    assert "ALLERGIES" in text and "Penicillin (rash)" in text
    assert "Azithromycin" in text


async def test_the_consultation_has_a_reference_number(northgate):
    visit = await _consultation(northgate)

    text = await _printed(northgate, visit)

    # the random tail of the id — a UUIDv7's head is a timestamp and repeats for hours
    assert "CON-" + visit["id"].replace("-", "")[-6:].upper() in text


async def test_the_consultation_prints_registration_numbers_when_set_and_not_otherwise(northgate):
    doctor = await _a_doctor(northgate)
    visit = await _consultation(northgate)
    assert "Reg. No." not in await _printed(northgate, visit)

    await northgate.patch("/v1/tenant/config", json={"registration_no": "KA/CL/2041"})
    await northgate.patch(
        f"/v1/users/{doctor['id']}",
        json={"qualifications": "MBBS, MD (General Medicine)", "registration_no": "KMC 48213"},
    )
    try:
        text = await _printed(northgate, visit)
        assert "Clinic Reg. No. KA/CL/2041" in text
        assert "MBBS, MD (General Medicine)" in text
        assert "Reg. No. KMC 48213" in text
    finally:
        await northgate.patch("/v1/tenant/config", json={"registration_no": ""})
        await northgate.patch(f"/v1/users/{doctor['id']}", json={"qualifications": "", "registration_no": ""})


async def test_the_consultation_prints_the_vitals_that_were_recorded(northgate):
    visit = await _consultation(
        northgate,
        encounter={"clinical_ext": {"bp": "118/76", "pulse": 82, "temperature": 99.1, "spo2": 97}},
    )

    text = await _printed(northgate, visit)

    assert "BP 118/76" in text
    assert "Pulse 82" in text
    assert "Temp 99.1" in text
    assert "SpO₂ 97" in text


async def test_a_consultation_with_no_vitals_prints_no_vitals_line(northgate):
    visit = await _consultation(northgate)

    assert "vitals" not in (await _printed(northgate, visit)).lower()


async def test_the_examination_prints_the_clinics_own_fields_and_the_structured_exam(northgate):
    visit = await _consultation(
        northgate,
        encounter={"clinical_ext": {"pain_scale": 6, "tooth_chart": {"16": "caries"}, "procedures": ["Scaling"]}},
    )

    text = await _printed(northgate, visit)

    assert "Pain score" in text
    assert "Tooth chart" in text and "16" in text
    assert "Scaling" in text


async def test_nothing_optional_prints_as_an_empty_label(northgate):
    visit = await _consultation(
        northgate,
        patient={"blood_group": None},
        encounter={"chief_complaint": "", "diagnosis": "", "notes": ""},
    )

    text = (await _printed(northgate, visit)).lower()

    for label in ("allergies", "medication advised", "follow-up", "weight", "blood group", "history", "none"):
        assert label not in text, label


async def test_medicine_text_is_printed_literally(northgate):
    visit = await _consultation(
        northgate, items=[{**_AZITHROMYCIN, "medicine": "Amox <500> & Clav", "instructions": "<b>after</b> food"}]
    )

    text = await _printed(northgate, visit)

    assert "Amox <500> & Clav" in text
    assert "<b>after</b> food" in text


async def test_a_very_long_note_flows_onto_more_pages(northgate):
    import re

    visit = await _consultation(northgate, encounter={"notes": "Chest clear on auscultation. " * 250})

    text = await _printed(northgate, visit)

    pages = re.search(r"Page 1 of (\d+)", text)
    assert pages and int(pages.group(1)) >= 2, "a long note should run on, not be cut off"
    assert f"Page 2 of {pages.group(1)}" in text


async def test_a_patient_name_in_an_unsupported_script_does_not_break_the_consultation(northgate):
    visit = await _consultation(northgate, patient={"full_name": "अनिता Verma"})

    text = await _printed(northgate, visit)

    assert "Verma" in text


# --- what the consultation prints of the clinic's own fields -------------------------------


def test_exam_fields_keep_lists_and_drop_what_is_printed_elsewhere():
    from types import SimpleNamespace as NS

    from app.services.consultation_pdf import exam_fields

    schema = [
        NS(field_key="symptoms", label="Symptoms"),
        NS(field_key="pain_scale", label="Pain score"),
        NS(field_key="bp", label="Blood pressure"),
        NS(field_key="chief_complaint", label="Chief complaint"),
        NS(field_key="empty", label="Empty"),
        NS(field_key="chart", label="Chart"),
    ]
    ext = {
        "symptoms": ["Swelling", "Fever"], "pain_scale": 6, "bp": "120/80",
        "chief_complaint": "x", "empty": "", "chart": {"16": "caries"},
    }

    assert exam_fields(schema, ext) == [("Symptoms", ["Swelling", "Fever"]), ("Pain score", 6)]


async def test_a_clinics_own_temperature_field_is_not_printed_as_fahrenheit(northgate):
    """A clinic that defined its own `temperature` field (in °C, say) overrides the shared one;
    the paper must say what the clinic's field says, never attach °F to a Celsius reading."""
    field = await northgate.post(
        "/v1/tenant/fields",
        json={"entity": "encounter", "field_key": "temperature", "label": "Temperature (°C)", "type": "number"},
    )
    assert field.status_code == 201, field.text
    try:
        visit = await _consultation(northgate, encounter={"clinical_ext": {"temperature": 37.5}})
        text = await _printed(northgate, visit)
    finally:
        await northgate.delete(f"/v1/tenant/fields/{field.json()['id']}")

    assert "Temperature (°C) 37.5" in text
    assert "°F" not in text


async def test_a_blood_pressure_typed_with_its_unit_is_not_given_a_second_one(northgate):
    visit = await _consultation(northgate, encounter={"clinical_ext": {"bp": "120/80 mmHg"}})

    text = await _printed(northgate, visit)

    assert "BP 120/80 mmHg" in text
    assert "mmHg mmHg" not in text
