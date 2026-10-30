"""An invoice, emailed to the patient it bills.

The mail goes out through the platform's own transport — a clinic never hands
the product a mail password. What the clinic decides is how the message reads
and where a reply lands: a reply-to address, the name the message is from,
and the subject and wording, with a short whitelist of placeholders filled in.

`mailer.send` is replaced with a capture, so nothing leaves the process; the
captured `Email` is exactly what a real transport would have been handed.

Every test that changes the clinic's settings puts them back — the database is
shared across the suite.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from app.services import invoice_email, mailer
from tests.conftest import audit_lines, sign_in

_BLANK = {
    "invoice_email_reply_to": "",
    "invoice_email_sender_name": "",
    "invoice_email_subject": "",
    "invoice_email_message": "",
}


@pytest.fixture
def outbox(monkeypatch) -> list[mailer.Email]:
    sent: list[mailer.Email] = []

    async def capture(email: mailer.Email) -> None:
        sent.append(email)

    monkeypatch.setattr(mailer, "send", capture)
    return sent


@pytest.fixture
async def clean_invoice_email(northgate):
    yield
    await northgate.patch(
        "/v1/tenant/config", json={**_BLANK, "contact_email": "hello@northgate.clinic"}
    )


async def _patient(signed, *, email: str | None) -> dict:
    tag = uuid.uuid4().hex[:8]
    body = {"full_name": f"Mail Test {tag}", "dob": "1988-04-05", "sex": "female", "email": email}
    created = await signed.post("/v1/patients", json=body)
    assert created.status_code == 201, created.text
    return created.json()


async def _invoice(signed, patient: dict, *, issue: bool = True) -> dict:
    created = await signed.post(
        "/v1/billing/invoices",
        json={
            "patient_id": patient["id"],
            "line_items": [{"description": "Consultation", "source": "manual", "qty": 1, "unit_price": 750}],
            "issue": issue,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


def _send(signed, invoice: dict, **body):
    return signed.post(f"/v1/billing/invoices/{invoice['id']}/send-email", json=body)


# --- sending -------------------------------------------------------------------


async def test_an_invoice_goes_out_with_its_pdf_and_the_clinics_own_wording(
    northgate, outbox, clean_invoice_email
):
    saved = await northgate.patch(
        "/v1/tenant/config",
        json={
            "invoice_email_reply_to": "accounts@northgate.clinic",
            "invoice_email_sender_name": "Northgate Accounts",
            "invoice_email_subject": "Your bill {invoice_number} from {clinic_name}",
            "invoice_email_message": "Dear {patient_name},\nThis is {invoice_number} for {amount}.",
        },
    )
    assert saved.status_code == 200, saved.text

    patient = await _patient(northgate, email="mail.test@example.com")
    invoice = await _invoice(northgate, patient)

    response = await _send(northgate, invoice)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "sent"
    assert body["to"] == "mail.test@example.com"
    assert body["invoice"]["emailed_to"] == "mail.test@example.com"
    assert body["invoice"]["emailed_at"]

    assert len(outbox) == 1
    email = outbox[0]
    clinic = (await northgate.get("/v1/tenant")).json()["name"]
    assert email.to == "mail.test@example.com"
    assert email.subject == f"Your bill {invoice['number']} from {clinic}"
    assert f"Dear {patient['full_name']},\nThis is {invoice['number']} for ₹750.00." in email.text
    assert email.reply_to == "accounts@northgate.clinic"
    assert email.sensitive, "an invoice names a patient; its body must stay out of the log"

    message = email.as_message()
    assert message["Reply-To"] == "accounts@northgate.clinic"
    assert message["From"].addresses[0].display_name == "Northgate Accounts via Astrale PMS"
    assert message["From"].addresses[0].addr_spec == "no-reply@astrale.health"

    pdfs = [part for part in message.iter_attachments() if part.get_content_type() == "application/pdf"]
    assert len(pdfs) == 1
    assert pdfs[0].get_filename() == f"invoice-{invoice['number']}.pdf"
    assert pdfs[0].get_content().startswith(b"%PDF")

    # the invoice remembers it, so the screen can say "last sent"
    again = (await northgate.get(f"/v1/billing/invoices/{invoice['id']}")).json()
    assert again["emailed_to"] == "mail.test@example.com"
    assert again["emailed_at"] == body["invoice"]["emailed_at"]

    trail = await audit_lines(northgate.tenant_id, action="invoice.emailed")
    mine = [t for t in trail if t["entity_id"] == invoice["id"]]
    assert mine and mine[0]["outcome"] == "ok"
    assert mine[0]["meta"]["to"] == "mail.test@example.com"
    assert mine[0]["actor_label"]


async def test_a_typed_address_is_used_instead_of_the_one_on_file(northgate, outbox):
    patient = await _patient(northgate, email="on.file@example.com")
    invoice = await _invoice(northgate, patient)

    response = await _send(northgate, invoice, to="family.member@example.com")

    assert response.status_code == 200, response.text
    assert response.json()["to"] == "family.member@example.com"
    assert [e.to for e in outbox] == ["family.member@example.com"]


async def test_a_typed_address_that_is_not_an_email_is_refused(northgate, outbox):
    patient = await _patient(northgate, email="on.file@example.com")
    invoice = await _invoice(northgate, patient)

    response = await _send(northgate, invoice, to="not-an-address")

    assert response.status_code == 422
    assert outbox == []


async def test_a_patient_with_no_email_is_told_apart_from_a_failure(northgate, outbox):
    patient = await _patient(northgate, email=None)
    invoice = await _invoice(northgate, patient)

    response = await _send(northgate, invoice)

    assert response.status_code == 422
    message = response.json()["error"]["message"]
    assert "no email" in message.lower()
    assert "patient" in message.lower()
    assert outbox == []


async def test_a_malformed_email_on_file_is_refused_with_what_to_fix(northgate, outbox):
    patient = await _patient(northgate, email="nobody-at-example")
    invoice = await _invoice(northgate, patient)

    response = await _send(northgate, invoice)

    assert response.status_code == 422
    assert "nobody-at-example" in response.json()["error"]["message"]
    assert outbox == []


async def test_a_void_invoice_cannot_be_sent(northgate, outbox):
    patient = await _patient(northgate, email="void.test@example.com")
    invoice = await _invoice(northgate, patient)
    assert (await northgate.post(f"/v1/billing/invoices/{invoice['id']}/void")).status_code == 200

    response = await _send(northgate, invoice)

    assert response.status_code == 409
    assert "void" in response.json()["error"]["message"].lower()
    assert outbox == []


async def test_a_draft_must_be_issued_before_it_is_sent(northgate, outbox):
    patient = await _patient(northgate, email="draft.test@example.com")
    invoice = await _invoice(northgate, patient, issue=False)

    response = await _send(northgate, invoice)

    assert response.status_code == 409
    assert "issue" in response.json()["error"]["message"].lower()
    assert outbox == []


async def test_another_clinics_invoice_is_not_found(northgate, rowan, outbox):
    patient = await _patient(northgate, email="isolation@example.com")
    invoice = await _invoice(northgate, patient)

    response = await _send(rowan, invoice)

    assert response.status_code == 404
    assert outbox == []


async def test_a_role_that_does_not_message_patients_cannot_send(client, northgate, outbox):
    patient = await _patient(northgate, email="guard@example.com")
    invoice = await _invoice(northgate, patient)
    pharmacist = await sign_in(client, "northgate_pharmacist")

    response = await _send(pharmacist, invoice)

    assert response.status_code == 403
    assert outbox == []


async def test_a_failed_handover_is_reported_and_recorded_but_not_marked_sent(
    northgate, monkeypatch
):
    async def broken(email):
        raise mailer.MailError("connection refused")

    monkeypatch.setattr(mailer, "send", broken)
    patient = await _patient(northgate, email="broken@example.com")
    invoice = await _invoice(northgate, patient)

    response = await _send(northgate, invoice)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["detail"]
    assert body["invoice"]["emailed_at"] is None

    trail = await audit_lines(northgate.tenant_id, action="invoice.emailed")
    mine = [t for t in trail if t["entity_id"] == invoice["id"]]
    assert mine and mine[0]["outcome"] == "failed"


# --- the per-person budget -----------------------------------------------------------
#
# The limiter is in-process and shared, and conftest's autouse
# `reset_rate_limiter` empties it around every test — so each test here starts
# with a full budget and leaves none spent behind it.


async def test_sending_past_the_budget_is_a_429_and_nothing_goes(northgate, outbox, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "invoice_email_rate_limit", "2/hour")
    patient = await _patient(northgate, email="budget@example.com")
    invoice = await _invoice(northgate, patient)

    assert (await _send(northgate, invoice)).status_code == 200
    assert (await _send(northgate, invoice)).status_code == 200
    refused = await _send(northgate, invoice)

    assert refused.status_code == 429
    error = refused.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["details"]["retry_after_seconds"] > 0
    assert len(outbox) == 2


async def test_one_persons_spent_budget_holds_up_nobody_else(
    client, northgate, rowan, outbox, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "invoice_email_rate_limit", "1/hour")
    patient = await _patient(northgate, email="shared.budget@example.com")
    invoice = await _invoice(northgate, patient)
    assert (await _send(northgate, invoice)).status_code == 200
    assert (await _send(northgate, invoice)).status_code == 429

    # a colleague in the same clinic
    colleague = await sign_in(client, "northgate_doctor")
    assert (await _send(colleague, invoice)).status_code == 200, "a colleague has their own budget"

    # and someone in another clinic
    theirs = await _invoice(rowan, await _patient(rowan, email="rowan.budget@example.com"))
    assert (await _send(rowan, theirs)).status_code == 200, "another clinic has its own budget"

    assert len(outbox) == 3


# --- a clinic that has set nothing up ---------------------------------------------


async def test_with_nothing_set_up_it_still_sends_and_replies_go_to_the_clinic_contact(
    northgate, outbox, clean_invoice_email
):
    await northgate.patch("/v1/tenant/config", json=_BLANK)
    patient = await _patient(northgate, email="defaults@example.com")
    invoice = await _invoice(northgate, patient)

    response = await _send(northgate, invoice)

    assert response.status_code == 200, response.text
    clinic = (await northgate.get("/v1/tenant")).json()["name"]
    email = outbox[0]
    assert email.subject == f"Invoice {invoice['number']} from {clinic}"
    assert patient["full_name"] in email.text
    assert email.reply_to == "hello@northgate.clinic"
    assert email.as_message()["From"].addresses[0].display_name == f"{clinic} via Astrale PMS"


async def test_with_no_address_at_all_there_is_no_reply_to_and_the_patient_is_told(
    northgate, outbox, clean_invoice_email
):
    await northgate.patch("/v1/tenant/config", json={**_BLANK, "contact_email": ""})
    patient = await _patient(northgate, email="noreply@example.com")
    invoice = await _invoice(northgate, patient)

    response = await _send(northgate, invoice)

    assert response.status_code == 200, response.text
    email = outbox[0]
    assert email.reply_to is None
    assert email.as_message()["Reply-To"] is None
    assert "not read" in email.text


# --- the settings ------------------------------------------------------------------


async def test_the_invoice_email_settings_round_trip(northgate, clean_invoice_email):
    sent = {
        "invoice_email_reply_to": "Billing@Northgate.Clinic",
        "invoice_email_sender_name": "Northgate Billing",
        "invoice_email_subject": "Invoice {invoice_number}",
        "invoice_email_message": "Hello {patient_name}, {balance} is due on {due_date}.",
    }
    saved = await northgate.patch("/v1/tenant/config", json=sent)
    assert saved.status_code == 200, saved.text

    config = (await northgate.get("/v1/tenant/config")).json()
    assert config["invoice_email_reply_to"] == "billing@northgate.clinic"
    for key in ("invoice_email_sender_name", "invoice_email_subject", "invoice_email_message"):
        assert config[key] == sent[key]


@pytest.mark.parametrize(
    "field,value",
    [
        ("invoice_email_reply_to", "not-an-address"),
        ("invoice_email_reply_to", "two@example.com, three@example.com"),
        ("invoice_email_sender_name", "Billing\nBcc: someone@example.com"),
        ("invoice_email_sender_name", "billing@bank.example"),
        ("invoice_email_subject", "Invoice {invoice_no}"),
        ("invoice_email_subject", "Invoice {0}"),
        ("invoice_email_subject", "Invoice {patient_name.__class__}"),
        ("invoice_email_subject", "Line one\nLine two"),
        ("invoice_email_message", "Hello {patient_name"),
        ("invoice_email_message", "Total: {amount!r}"),
        ("invoice_email_message", "Hi }"),
    ],
)
async def test_bad_invoice_email_settings_are_refused(northgate, field, value):
    refused = await northgate.patch("/v1/tenant/config", json={field: value})
    assert refused.status_code == 422, (field, value)


async def test_an_unknown_placeholder_is_named_in_the_refusal(northgate):
    refused = await northgate.patch("/v1/tenant/config", json={"invoice_email_subject": "Hi {first_name}"})
    assert refused.status_code == 422
    assert "{first_name}" in refused.text


async def test_a_doctor_cannot_change_the_invoice_email(northgate_doctor):
    refused = await northgate_doctor.patch(
        "/v1/tenant/config", json={"invoice_email_reply_to": "x@example.com"}
    )
    assert refused.status_code == 403


# --- the template and the log ---------------------------------------------------------


def test_a_value_that_looks_like_a_placeholder_is_not_filled_in_again():
    rendered = invoice_email.render(
        "{patient_name} owes {amount}", {"patient_name": "{amount}", "amount": "₹10.00"}
    )
    assert rendered == "{amount} owes ₹10.00"


def test_the_placeholder_check():
    assert invoice_email.template_problem("") is None
    assert invoice_email.template_problem("Dear {patient_name}, {amount} for {invoice_number}") is None
    assert "{nope}" in invoice_email.template_problem("{nope}")
    assert invoice_email.template_problem("{{patient_name}}") is not None


async def test_the_console_transport_keeps_a_sensitive_body_out_of_the_info_log(caplog):
    email = mailer.Email(
        to="patient@example.com", subject="Invoice X", text="Dear Asha Rao, you owe ₹750.", sensitive=True
    )
    with caplog.at_level(logging.INFO, logger="pms.mail"):
        await mailer.send(email)

    info = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.INFO)
    assert "Asha Rao" not in info
    assert "patient@example.com" not in info
