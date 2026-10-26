"""The optional identity fields the printed documents carry: the clinic's
registration number, GSTIN and brand colour; a doctor's qualifications and
registration number; and the two vitals (temperature, SpO2) a consultation can
record.

Every test that sets one puts it back — the database is shared across the
suite, and a leftover registration number would show up on another test's PDF.
"""

from __future__ import annotations

import pytest

from tests.conftest import patients_of

DEFAULT_BRAND = "#0f766e"


@pytest.fixture
async def clean_identity(northgate):
    yield
    await northgate.patch(
        "/v1/tenant/config", json={"registration_no": "", "gstin": "", "brand_color": DEFAULT_BRAND}
    )


@pytest.fixture
async def clean_doctor(northgate):
    yield
    for doctor in (await northgate.get("/v1/users/doctors")).json():
        await northgate.patch(f"/v1/users/{doctor['id']}", json={"qualifications": "", "registration_no": ""})


def test_the_contrast_helpers():
    from app.services.colour import contrast_with_white, parse_hex

    assert contrast_with_white("#000000") == pytest.approx(21)
    assert contrast_with_white("#ffffff") == pytest.approx(1)
    assert parse_hex("#0f766e") == (15, 118, 110)
    with pytest.raises(ValueError):
        parse_hex("teal")


def test_tint_and_darken_stay_valid_colours():
    from app.services.colour import contrast_with_white, darken, parse_hex, tint

    assert tint("#0f766e", 0.0) == "#ffffff"
    assert tint("#0f766e", 1.0) == "#0f766e"
    parse_hex(tint("#0f766e", 0.18))
    assert contrast_with_white(darken("#0f766e", 0.3)) > contrast_with_white("#0f766e")


async def test_a_clinic_sets_its_print_identity(northgate, clean_identity):
    saved = await northgate.patch(
        "/v1/tenant/config",
        json={"registration_no": "KA/CL/2041", "gstin": "29ABCDE1234F1Z5", "brand_color": "#1e3a8a"},
    )
    assert saved.status_code == 200, saved.text

    config = (await northgate.get("/v1/tenant/config")).json()
    assert config["registration_no"] == "KA/CL/2041"
    assert config["gstin"] == "29ABCDE1234F1Z5"
    assert config["brand_color"] == "#1e3a8a"


@pytest.mark.parametrize("colour", ["#ffff00", "teal", "#12345", "#ffffff", "0f766e"])
async def test_the_brand_colour_must_be_a_dark_enough_hex(northgate, colour):
    refused = await northgate.patch("/v1/tenant/config", json={"brand_color": colour})
    assert refused.status_code == 422, colour


async def test_a_too_light_colour_is_refused_for_that_reason(northgate):
    refused = await northgate.patch("/v1/tenant/config", json={"brand_color": "#ffff00"})
    assert refused.status_code == 422
    assert "too light" in refused.text


async def test_a_clinic_that_never_chose_a_colour_gets_the_default(clearview):
    config = (await clearview.get("/v1/tenant/config")).json()
    assert config["brand_color"] == DEFAULT_BRAND
    assert config["registration_no"] == ""
    assert config["gstin"] == ""


async def test_a_doctor_cannot_change_the_clinics_print_identity(northgate_doctor):
    refused = await northgate_doctor.patch("/v1/tenant/config", json={"gstin": "29ABCDE1234F1Z5"})
    assert refused.status_code == 403


async def test_a_colleagues_qualifications_and_registration_number_round_trip(northgate, clean_doctor):
    doctor = (await northgate.get("/v1/users/doctors")).json()[0]
    assert doctor["qualifications"] is None
    assert doctor["registration_no"] is None

    saved = await northgate.patch(
        f"/v1/users/{doctor['id']}",
        json={"qualifications": "MBBS, MD (General Medicine)", "registration_no": "KMC 48213"},
    )
    assert saved.status_code == 200, saved.text

    again = next(d for d in (await northgate.get("/v1/users/doctors")).json() if d["id"] == doctor["id"])
    assert again["qualifications"] == "MBBS, MD (General Medicine)"
    assert again["registration_no"] == "KMC 48213"

    cleared = await northgate.patch(f"/v1/users/{doctor['id']}", json={"qualifications": "", "registration_no": ""})
    assert cleared.json()["qualifications"] is None
    assert cleared.json()["registration_no"] is None


async def test_temperature_and_spo2_are_kept_on_an_encounter(northgate):
    doctor = (await northgate.get("/v1/users/doctors")).json()[0]
    patient = (await patients_of(northgate))[0]

    encounter = (
        await northgate.post(
            "/v1/encounters",
            json={
                "patient_id": patient["id"],
                "doctor_id": doctor["id"],
                "chief_complaint": "Vitals check",
                "clinical_ext": {"bp": "118/76", "temperature": 99.1, "spo2": 97},
            },
        )
    ).json()

    assert encounter["clinical_ext"]["temperature"] == 99.1
    assert encounter["clinical_ext"]["spo2"] == 97
    assert encounter["clinical_ext"]["bp"] == "118/76"
