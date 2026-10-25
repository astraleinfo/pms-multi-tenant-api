"""A patient's mobile number is exactly ten digits; formatting is tolerated."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.clinical import PatientIn, PatientPatch


def _body(phone: str) -> dict:
    return {"full_name": f"Phone Rule {uuid.uuid4().hex[:8]}", "dob": "1990-02-03", "sex": "female", "phone": phone}


@pytest.mark.parametrize(
    "raw",
    ["9876543210", "+91 98765 43210", "+91-98765-43210", "919876543210", "098765 43210", " 98765 43210 "],
)
def test_formatted_numbers_are_stored_as_ten_digits(raw):
    assert PatientIn(**_body(raw)).phone == "9876543210"
    assert PatientPatch(phone=raw).phone == "9876543210"


@pytest.mark.parametrize("raw", ["1", "98765", "1234567890985645465", "98765abcde", "+44 7700 900142", "98765432101"])
def test_anything_else_is_refused(raw):
    with pytest.raises(ValidationError):
        PatientIn(**_body(raw))
    with pytest.raises(ValidationError):
        PatientPatch(phone=raw)


def test_a_clinic_may_leave_the_number_blank():
    assert PatientIn(**_body("")).phone == ""


async def test_registering_with_a_long_number_is_a_422(northgate):
    response = await northgate.post("/v1/patients", json=_body("1234567890985645465"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_a_formatted_number_is_saved_as_ten_digits(northgate):
    response = await northgate.post("/v1/patients", json=_body("+91 98765 43210"))
    assert response.status_code == 201, response.text
    assert response.json()["phone"] == "9876543210"


async def test_updating_to_a_bad_number_is_a_422(northgate):
    patient = (await northgate.post("/v1/patients", json=_body("9876543210"))).json()
    bad = await northgate.patch(f"/v1/patients/{patient['id']}", json={"phone": "12345"})
    assert bad.status_code == 422
    good = await northgate.patch(f"/v1/patients/{patient['id']}", json={"phone": "+91 91234 56789"})
    assert good.status_code == 200, good.text
    assert good.json()["phone"] == "9123456789"


def test_ten_digit_numbers_are_sent_with_91_and_others_are_left_alone():
    from app.schemas.clinical import to_e164
    from app.services.messaging.providers.twilio import _address

    assert to_e164("9876543210") == "+919876543210"
    assert to_e164("+44 7700 900142") == "+44 7700 900142"
    assert to_e164("") == ""
    assert _address(to_e164("9876543210"), "whatsapp") == "whatsapp:+919876543210"
    assert _address(to_e164("9876543210"), "sms") == "+919876543210"
