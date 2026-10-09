"""Patient notes: who may read and write them, that a saved note is never
rewritten, and that the printed record carries them for the roles that may."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import patients_of, sign_in
from tests.test_consultation_pdf import _text


async def _patient(signed) -> dict:
    """A patient of the test's own, so the notes on it are only this test's."""
    response = await signed.post(
        "/v1/patients",
        json={"full_name": f"Notes Patient {uuid.uuid4().hex[:8]}", "dob": "1984-06-01", "sex": "male",
              "phone": "+91 98450 00000"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _note(signed, patient: dict, body: str, **extra):
    return await signed.post(f"/v1/patients/{patient['id']}/notes", json={"body": body, **extra})


async def test_a_clinician_adds_notes_and_they_are_kept_newest_first(northgate, northgate_doctor):
    patient = await _patient(northgate)

    first = await _note(northgate_doctor, patient, "Anxious about extractions. Prefers mornings.")
    assert first.status_code == 201, first.text
    second = await _note(northgate_doctor, patient, "Asked about whitening —\nadvised after the crown.")
    assert second.status_code == 201, second.text

    note = first.json()
    assert note["author_name"] == northgate_doctor.user["name"]
    assert note["author_id"] == northgate_doctor.user["id"]
    assert note["author_role"] == "doctor"
    assert len(note["created_at"]) == 16  # YYYY-MM-DD HH:mm, clinic time

    notes = (await northgate_doctor.get(f"/v1/patients/{patient['id']}/notes")).json()
    assert [n["id"] for n in notes] == [second.json()["id"], first.json()["id"]]
    assert notes[0]["body"] == "Asked about whitening —\nadvised after the crown."


async def test_a_note_must_say_something(northgate_doctor):
    patient = await _patient(northgate_doctor)
    assert (await _note(northgate_doctor, patient, "   ")).status_code == 422
    assert (await _note(northgate_doctor, patient, "x" * 4001)).status_code == 422


async def test_reading_and_writing_follow_the_consultation_permissions(client, northgate):
    patient = await _patient(northgate)
    assert (await _note(northgate, patient, "Written by the practice admin")).status_code == 201

    reception = await sign_in(client, "northgate_reception")
    assert (await reception.get(f"/v1/patients/{patient['id']}/notes")).status_code == 200
    assert (await _note(reception, patient, "Reception cannot write this")).status_code == 403

    for key in ("northgate_pharmacist", "northgate_lab"):
        other = await sign_in(client, key)
        assert (await other.get(f"/v1/patients/{patient['id']}/notes")).status_code == 403, key
        assert (await _note(other, patient, "Nor this")).status_code == 403, key


async def test_a_correction_is_added_beside_the_note_never_over_it(client, northgate, northgate_doctor):
    patient = await _patient(northgate)
    original = (await _note(northgate_doctor, patient, "Allergic to penicillin.")).json()

    correction = await _note(
        northgate_doctor, patient, "Correction: amoxicillin, not penicillin.", amends_id=original["id"],
    )
    assert correction.status_code == 201, correction.text
    assert correction.json()["amends_id"] == original["id"]

    notes = (await northgate_doctor.get(f"/v1/patients/{patient['id']}/notes")).json()
    assert {n["body"] for n in notes} == {"Allergic to penicillin.", "Correction: amoxicillin, not penicillin."}

    # Only its author can correct a note; anyone else writes their own.
    admin_try = await _note(northgate, patient, "Not mine to correct", amends_id=original["id"])
    assert admin_try.status_code == 403

    # A correction has to name a note on this same patient.
    elsewhere = await _patient(northgate)
    stray = await _note(northgate_doctor, elsewhere, "Wrong patient", amends_id=original["id"])
    assert stray.status_code == 404


async def test_the_database_refuses_to_rewrite_a_saved_note(northgate_doctor):
    """There is no edit route, and the table itself refuses one too."""
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError

    from app.db.session import tenant_session
    from app.models.clinical import PatientNote

    patient = await _patient(northgate_doctor)
    note = (await _note(northgate_doctor, patient, "As first written.")).json()

    async with tenant_session(uuid.UUID(northgate_doctor.tenant_id)) as session:
        with pytest.raises(DBAPIError, match="cannot be changed"):
            await session.execute(
                update(PatientNote).where(PatientNote.id == uuid.UUID(note["id"])).values(body="Rewritten.")
            )

    notes = (await northgate_doctor.get(f"/v1/patients/{patient['id']}/notes")).json()
    assert notes[0]["body"] == "As first written."


async def test_another_clinic_cannot_read_or_write_a_patients_notes(northgate_doctor, clearview):
    patient = await _patient(northgate_doctor)
    await _note(northgate_doctor, patient, "Northgate only.")

    assert (await clearview.get(f"/v1/patients/{patient['id']}/notes")).status_code == 404
    assert (await _note(clearview, patient, "From Clearview")).status_code == 404


async def test_the_printed_record_carries_the_notes_for_roles_that_may_read_them(client, northgate_doctor):
    patient = await _patient(northgate_doctor)
    original = (await _note(northgate_doctor, patient, "Gag reflex — use the smaller sensor.")).json()
    await _note(northgate_doctor, patient, "Use the paediatric sensor.", amends_id=original["id"])

    printed = await northgate_doctor.get(f"/v1/patients/{patient['id']}/record.pdf")
    assert printed.status_code == 200, printed.text
    text = _text(printed.content)
    assert "CLINICAL NOTES" in text
    assert "Gag reflex" in text and "paediatric sensor" in text
    assert "Correction to the note of" in text
    assert northgate_doctor.user["name"] in text

    # Reception reads notes on screen, so reads them on paper.
    reception = await sign_in(client, "northgate_reception")
    assert "CLINICAL NOTES" in _text((await reception.get(f"/v1/patients/{patient['id']}/record.pdf")).content)

    # The pharmacist may print the record but not read the notes — they are left off.
    pharmacist = await sign_in(client, "northgate_pharmacist")
    paper = await pharmacist.get(f"/v1/patients/{patient['id']}/record.pdf")
    assert paper.status_code == 200, paper.text
    assert "CLINICAL NOTES" not in _text(paper.content)
    assert "Gag reflex" not in _text(paper.content)


async def test_a_record_with_no_notes_says_so(northgate):
    patient = (await patients_of(northgate))[0]
    text = _text((await northgate.get(f"/v1/patients/{patient['id']}/record.pdf")).content)
    assert "CLINICAL NOTES" in text
