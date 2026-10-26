"""Consent forms: the clinic's copy of a template, versioned; and patients'
consents against one exact version, final once answered."""

from __future__ import annotations

import base64

from tests.conftest import patients_of, sign_in
from tests.test_consultation_pdf import _embedded_images, _text
from tests.test_pricing_and_files import PNG_BYTES

SIGNATURE = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()


async def _default_form(signed) -> dict:
    forms = (await signed.get("/v1/consent/forms")).json()
    assert forms, "a clinic always has its default form"
    return (await signed.get(f"/v1/consent/forms/{forms[0]['id']}")).json()


def _required_fields(content: dict) -> dict:
    return {
        f["id"]: f"{f['label']} for the test"
        for s in content["sections"] for f in s["fields"] if f["required"]
    }


def _required_acks(content: dict) -> list[str]:
    return [a["id"] for s in content["sections"] for a in s["statements"] if a["required"]]


async def _published_content(signed, form: dict) -> dict:
    n = form["published"]["number"]
    return (await signed.get(f"/v1/consent/forms/{form['id']}/versions/{n}")).json()["content"]


async def _publish_edit(signed, form_id: str, title: str) -> dict:
    form = (await signed.post(f"/v1/consent/forms/{form_id}/draft", json={})).json()
    content = form["draft"]["content"]
    content["title"] = title
    saved = await signed.put(f"/v1/consent/forms/{form_id}/draft", json={"content": content})
    assert saved.status_code == 200, saved.text
    published = await signed.post(f"/v1/consent/forms/{form_id}/publish")
    assert published.status_code == 200, published.text
    return published.json()


async def _request(signed, form: dict, *, patient: dict | None = None) -> dict:
    patient = patient or (await patients_of(signed))[0]
    content = await _published_content(signed, form)
    response = await signed.post("/v1/consent/records", json={
        "patient_id": patient["id"], "form_id": form["id"], "field_values": _required_fields(content),
    })
    assert response.status_code == 201, response.text
    return response.json()


def _signature_payload(record: dict, **overrides) -> dict:
    content = record["content"]
    payload = {
        "signer_name": record["patient"]["full_name"],
        "acknowledged": _required_acks(content),
        "patient_signature": SIGNATURE,
        "provider_signature": SIGNATURE if content["signatures"]["provider"] else None,
    }
    payload.update(overrides)
    return payload


# --- templates and the clinic's forms --------------------------------------------------


async def test_the_system_templates_cover_each_kind_of_practice_and_are_read_only(northgate):
    templates = (await northgate.get("/v1/consent/templates")).json()
    assert {"general", "dental", "ophthalmology", "orthopaedic", "dermatology"} <= {t["key"] for t in templates}
    dental = (await northgate.get("/v1/consent/templates/dental")).json()
    assert dental["content"]["sections"]
    # there is no route that writes a template
    assert (await northgate.put("/v1/consent/templates/dental", json={})).status_code in (404, 405)


async def test_a_clinic_is_given_the_form_for_its_clinical_type_published_as_v1(northgate, clearview):
    dental = await _default_form(northgate)
    assert dental["template_key"] == "dental"
    assert dental["published"]["number"] >= 1
    eye = await _default_form(clearview)
    assert eye["template_key"] == "ophthalmology"


async def test_a_clinic_can_add_a_form_from_another_template(northgate):
    created = await northgate.post("/v1/consent/forms", json={"template_key": "general"})
    assert created.status_code == 201, created.text
    form = created.json()
    assert form["published"]["number"] == 1 and form["draft"] is None
    assert (await northgate.post("/v1/consent/forms", json={"template_key": "nope"})).status_code == 404


async def test_one_clinics_edits_never_reach_another_clinic_or_the_template(northgate, rowan):
    form = await _default_form(northgate)
    await _publish_edit(northgate, form["id"], "Northgate's own wording")

    template = (await northgate.get("/v1/consent/templates/dental")).json()
    assert template["content"]["title"] != "Northgate's own wording"
    rowan_form = await _default_form(rowan)
    assert rowan_form["name"] != "Northgate's own wording"


async def test_another_clinic_cannot_see_or_change_this_clinics_forms_or_consents(northgate, clearview):
    form = await _default_form(northgate)
    record = await _request(northgate, form)

    assert (await clearview.get(f"/v1/consent/forms/{form['id']}")).status_code == 404
    assert (await clearview.post(f"/v1/consent/forms/{form['id']}/draft", json={})).status_code == 404
    assert (await clearview.get(f"/v1/consent/records/{record['id']}")).status_code == 404
    assert (await clearview.post(f"/v1/consent/records/{record['id']}/sign",
                                 json=_signature_payload(record))).status_code == 404
    assert (await clearview.get(f"/v1/consent/records/{record['id']}/document.pdf")).status_code == 404
    listed = (await clearview.get("/v1/consent/records")).json()
    assert record["id"] not in {r["id"] for r in listed}


# --- versions ------------------------------------------------------------------------------


async def test_editing_makes_a_draft_and_publishing_archives_the_version_it_replaces(northgate):
    form = await _default_form(northgate)
    before = form["published"]["number"]

    drafted = (await northgate.post(f"/v1/consent/forms/{form['id']}/draft", json={})).json()
    assert drafted["draft"]["number"] == before + 1
    assert drafted["published"]["number"] == before  # untouched while the draft is edited

    published = await _publish_edit(northgate, form["id"], "Revised dental consent")
    assert published["published"]["number"] == before + 1
    assert published["draft"] is None
    statuses = {v["number"]: v["status"] for v in published["versions"]}
    assert statuses[before] == "archived"
    assert statuses[before + 1] == "published"
    assert published["published"]["published_by"]


async def test_publishing_with_no_draft_is_refused(northgate):
    form = await _default_form(northgate)
    if form["draft"]:
        await northgate.delete(f"/v1/consent/forms/{form['id']}/draft")
    assert (await northgate.post(f"/v1/consent/forms/{form['id']}/publish")).status_code == 409


async def test_a_published_version_cannot_be_changed_even_beneath_the_api(northgate):
    """The trigger holds the rule for any path that is not the service."""
    import uuid

    from sqlalchemy import text

    from app.db.session import tenant_session

    form = await _default_form(northgate)
    version_id = form["published"]["id"]
    tenant_id = uuid.UUID(northgate.tenant_id) if isinstance(northgate.tenant_id, str) else northgate.tenant_id
    raised = False
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                text("UPDATE consent_form_versions SET content = '{}'::jsonb WHERE id = :id"),
                {"id": version_id},
            )
    except Exception as exc:  # noqa: BLE001 - asserting it was refused
        raised = "cannot be changed" in str(exc)
    assert raised


async def test_an_older_version_is_restored_as_a_new_draft_not_revived(northgate):
    form = await _default_form(northgate)
    first = form["published"]["number"]
    original_title = (await _published_content(northgate, form))["title"]
    await _publish_edit(northgate, form["id"], "A later wording")

    restored = (await northgate.post(f"/v1/consent/forms/{form['id']}/draft",
                                     json={"from_version": first})).json()
    assert restored["draft"]["content"]["title"] == original_title
    assert restored["draft"]["number"] > first
    old = next(v for v in restored["versions"] if v["number"] == first)
    assert old["status"] == "archived"


async def test_reset_puts_the_template_back_in_the_draft_only(northgate):
    form = await _default_form(northgate)
    await _publish_edit(northgate, form["id"], "Something custom")
    reset = (await northgate.post(f"/v1/consent/forms/{form['id']}/reset")).json()
    template = (await northgate.get("/v1/consent/templates/dental")).json()
    assert reset["draft"]["content"]["title"] == template["content"]["title"]
    assert reset["published"]["content"]["title"] == "Something custom"


async def test_a_draft_that_does_not_validate_is_refused(northgate):
    form = await _default_form(northgate)
    drafted = (await northgate.post(f"/v1/consent/forms/{form['id']}/draft", json={})).json()
    content = drafted["draft"]["content"]
    content["sections"] = []
    assert (await northgate.put(f"/v1/consent/forms/{form['id']}/draft",
                                json={"content": content})).status_code == 422


# --- patients' consents ------------------------------------------------------------------


async def test_a_consent_captures_the_exact_version_and_keeps_it_after_a_new_one(northgate):
    form = await _default_form(northgate)
    record = await _request(northgate, form)
    asked = record["version_number"]
    asked_title = record["title"]

    signed = await northgate.post(f"/v1/consent/records/{record['id']}/sign", json=_signature_payload(record))
    assert signed.status_code == 200, signed.text
    assert signed.json()["status"] == "signed"

    await _publish_edit(northgate, form["id"], "A newer version nobody signed")
    again = (await northgate.get(f"/v1/consent/records/{record['id']}")).json()
    assert again["version_number"] == asked
    assert again["title"] == asked_title

    text = _text((await northgate.get(f"/v1/consent/records/{record['id']}/document.pdf")).content)
    assert f"v{asked}" in text
    assert "A newer version nobody signed" not in text


async def test_required_fields_and_acknowledgements_are_enforced(northgate):
    form = await _default_form(northgate)
    patient = (await patients_of(northgate))[0]
    missing = await northgate.post("/v1/consent/records", json={
        "patient_id": patient["id"], "form_id": form["id"], "field_values": {},
    })
    assert missing.status_code == 422

    record = await _request(northgate, form)
    refused = await northgate.post(f"/v1/consent/records/{record['id']}/sign",
                                   json=_signature_payload(record, acknowledged=[]))
    assert refused.status_code == 422
    not_drawn = await northgate.post(f"/v1/consent/records/{record['id']}/sign",
                                     json=_signature_payload(record, patient_signature="data:text/plain,hi"))
    assert not_drawn.status_code == 422


async def test_a_signed_consent_is_final(northgate):
    form = await _default_form(northgate)
    record = await _request(northgate, form)
    assert (await northgate.post(f"/v1/consent/records/{record['id']}/sign",
                                 json=_signature_payload(record))).status_code == 200

    assert (await northgate.post(f"/v1/consent/records/{record['id']}/sign",
                                 json=_signature_payload(record))).status_code == 409
    assert (await northgate.post(f"/v1/consent/records/{record['id']}/decline",
                                 json={"reason": "changed my mind"})).status_code == 409


async def test_a_consent_can_be_declined_and_that_is_final_too(northgate):
    form = await _default_form(northgate)
    record = await _request(northgate, form)
    declined = await northgate.post(f"/v1/consent/records/{record['id']}/decline",
                                    json={"reason": "Wants a second opinion"})
    assert declined.status_code == 200, declined.text
    assert declined.json()["status"] == "declined"
    assert (await northgate.post(f"/v1/consent/records/{record['id']}/sign",
                                 json=_signature_payload(record))).status_code == 409


async def test_an_expired_request_cannot_be_signed(northgate):
    import uuid

    from sqlalchemy import text

    from app.db.session import tenant_session

    form = await _default_form(northgate)
    record = await _request(northgate, form)
    tenant_id = uuid.UUID(str(northgate.tenant_id))
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text("UPDATE patient_consents SET expires_at = now() - interval '1 day' WHERE id = :id"),
            {"id": record["id"]},
        )
    again = (await northgate.get(f"/v1/consent/records/{record['id']}")).json()
    assert again["status"] == "expired"
    assert (await northgate.post(f"/v1/consent/records/{record['id']}/sign",
                                 json=_signature_payload(record))).status_code == 409


# --- who may do what ---------------------------------------------------------------------------


async def test_reception_and_doctors_ask_for_consent_but_cannot_change_the_form(client, northgate):
    form = await _default_form(northgate)
    for key in ("northgate_reception", "northgate_doctor"):
        staff = await sign_in(client, key)
        assert (await staff.get("/v1/consent/forms")).status_code == 200
        assert (await staff.post(f"/v1/consent/forms/{form['id']}/draft", json={})).status_code == 403
        assert (await staff.post(f"/v1/consent/forms/{form['id']}/publish")).status_code == 403
        assert (await staff.get("/v1/consent/templates")).status_code == 403
        record = await _request(staff, form)
        assert record["status"] == "pending"


async def test_roles_without_consent_cannot_read_it(client, northgate):
    pharmacist = await sign_in(client, "northgate_pharmacist")
    assert (await pharmacist.get("/v1/consent/forms")).status_code == 403
    assert (await pharmacist.get("/v1/consent/records")).status_code == 403


async def test_every_step_is_in_the_audit_trail(northgate):
    form = await _default_form(northgate)
    await _publish_edit(northgate, form["id"], "Audited wording")
    record = await _request(northgate, form)
    await northgate.post(f"/v1/consent/records/{record['id']}/sign", json=_signature_payload(record))
    await northgate.get(f"/v1/consent/records/{record['id']}/document.pdf")

    import uuid

    from sqlalchemy import select

    from app.db.session import tenant_session
    from app.models.tenancy import AuditEvent

    async with tenant_session(uuid.UUID(str(northgate.tenant_id))) as session:
        actions = set((await session.execute(
            select(AuditEvent.action).where(AuditEvent.action.like("consent.%"))
        )).scalars())
    assert {"consent.draft_created", "consent.draft_saved", "consent.version_published",
            "consent.version_archived", "consent.requested", "consent.signed", "consent.printed"} <= actions


# --- the document ------------------------------------------------------------------------------


async def test_the_document_carries_the_clinic_the_patient_the_version_and_signatures(northgate):
    form = await _default_form(northgate)
    patient = (await patients_of(northgate))[0]
    record = await _request(northgate, form, patient=patient)

    pending = _text((await northgate.get(f"/v1/consent/records/{record['id']}/document.pdf")).content)
    assert "Northgate" in pending
    assert patient["full_name"] in pending and patient["mrn"] in pending
    assert record["title"] in pending
    assert f"v{record['version_number']}" in pending
    assert "Patient (or authorised representative) signature".upper() in pending
    assert "Page 1 of" in pending

    await northgate.post(f"/v1/consent/records/{record['id']}/sign", json=_signature_payload(record))
    signed = await northgate.get(f"/v1/consent/records/{record['id']}/document.pdf")
    assert signed.status_code == 200
    assert signed.headers["content-type"] == "application/pdf"
    import io

    from pypdf import PdfReader

    images = sum(len(p.images) for p in PdfReader(io.BytesIO(signed.content)).pages)
    assert images >= 1  # the drawn signature(s)


async def test_the_consent_document_carries_the_forms_header(northgate):
    form = await _default_form(northgate)
    record = await _request(northgate, form)
    await northgate.patch("/v1/tenant/config", json={"registration_no": "KA/CL/2041"})
    try:
        text = _text((await northgate.get(f"/v1/consent/records/{record['id']}/document.pdf")).content)
    finally:
        await northgate.patch("/v1/tenant/config", json={"registration_no": ""})

    assert "NORTHGATE" in text  # the clinic's name in capitals, as every form's header prints it
    assert "Clinic Reg. No. KA/CL/2041" in text
    assert record["title"].upper() in text
    assert f"Version {record['version_number']}" in text


async def test_a_draft_prints_marked_as_a_draft_and_a_version_prints_blank(northgate):
    form = await _default_form(northgate)
    drafted = (await northgate.post(f"/v1/consent/forms/{form['id']}/draft", json={})).json()
    draft = drafted["draft"]["number"]
    text = _text((await northgate.get(f"/v1/consent/forms/{form['id']}/versions/{draft}/document.pdf")).content)
    assert "DRAFT" in text

    n = drafted["published"]["number"]
    blank = await northgate.get(f"/v1/consent/forms/{form['id']}/versions/{n}/document.pdf")
    assert blank.status_code == 200
    assert "DRAFT" not in _text(blank.content)
    assert len(_embedded_images(blank.content)) == 0  # no logo set, no signatures
