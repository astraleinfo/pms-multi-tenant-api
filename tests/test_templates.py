"""The words the platform sends: SMS bodies and email templates.

SMS is checked for what costs money or confuses a patient — a character that
forces the expensive encoding, a missing way to reply — and email for the
things that must be true of every message: both parts, the recipient named,
nothing a user typed rendered as markup.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.models.enums import MessageTemplate
from app.services import mailer
from app.services.messaging.templates import BODIES, GSM7, contact_phrase, render

FULL = {
    "patient_name": "Samuel",
    "clinic_name": "Northgate Dental Studio",
    "clinic_phone": "+44 161 496 0110",
    "doctor_name": "Dr Aparna Iyer",
    "weekday": "Tue",
    "date": "09 Nov 2027",
    "time": "09:20",
    "was_date": "02 Nov 2027",
    "was_time": "14:40",
}


@pytest.mark.parametrize("template", list(BODIES))
def test_every_sms_stays_in_the_cheap_character_set(template):
    body = render(template, FULL)
    assert all(ch in GSM7 for ch in body), {ch for ch in body if ch not in GSM7}


@pytest.mark.parametrize("template", list(BODIES))
def test_every_sms_names_the_clinic_first_and_says_how_to_reply(template):
    body = render(template, FULL)
    assert body.startswith("Northgate Dental Studio:")
    assert "+44 161 496 0110" in body
    # two segments at most, even with a long clinic name and doctor
    assert len(body) <= 306


def test_a_clinic_without_a_phone_still_gets_a_way_to_reply():
    body = render(MessageTemplate.BOOKING_CONFIRMATION, {**FULL, "clinic_phone": ""})
    assert "reply to this text" in body
    assert "call" not in body
    assert contact_phrase("  ") == "reply to this text"


def test_a_booking_without_a_doctor_still_reads_as_a_sentence():
    body = render(MessageTemplate.BOOKING_CONFIRMATION, {**FULL, "doctor_name": ""})
    assert "booked with the clinic" in body
    assert "  " not in body


def test_the_booking_confirmation_carries_the_date_and_time():
    body = render(MessageTemplate.BOOKING_CONFIRMATION, FULL)
    assert "Tue 09 Nov 2027 at 09:20" in body


# --- email ----------------------------------------------------------------------------------


def _emails() -> list[mailer.Email]:
    due, hold = date(2026, 10, 8), date(2026, 10, 16)
    rent = [
        mailer.rent_notice(
            to="admin@clinic.example", name="Priya", clinic="Northgate <Dental>", stage=stage,
            number="AST-202610-NORTHGATE", period="October 2026", amount="INR 9,999.00",
            lines=[("Astrale PMS subscription", "INR 9,999.00")], due_on=due, hold_on=hold, reference="UPI 1",
            url="https://northgate.example/settings?tab=subscription",
        )
        for stage in ("issued", "due_soon", "due", "overdue", "final", "blocked", "paid")
    ]
    return [
        mailer.invitation(to="p@clinic.example", name="Priya <b>", clinic="Northgate <Dental>",
                          url="https://x.example/accept?token=a", expires_in_days=7),
        mailer.password_reset(to="p@clinic.example", name="Priya", clinic="Northgate <Dental>",
                              url="https://x.example/reset?token=a", expires_in_hours=2),
        mailer.support_request(
            to="support@astrale.health", kind="walkthrough", clinic="Northgate <Dental>", slug="northgate",
            requested_by="Priya", reply_to="p@clinic.example",
            message="<script>alert(1)</script>", console_url="https://admin.example/t", reference="SR-1",
        ),
        *rent,
    ]


@pytest.mark.parametrize("email", _emails(), ids=lambda e: e.subject[:40])
def test_every_email_has_both_parts_and_escapes_what_it_was_given(email):
    assert email.text.strip() and email.html
    assert "<script>" not in email.html
    assert "Northgate <Dental>" not in email.html  # rendered as text, not as a tag
    assert "Northgate &lt;Dental&gt;" in email.html
    assert email.to in email.html  # the footer says who it was sent to
    message = email.as_message()
    assert message.get_content_type() == "multipart/alternative"


def test_rent_notices_escalate_in_tone():
    by_stage = {e.subject: e.html for e in _emails()[3:]}
    htmls = list(by_stage.values())
    blue, amber, red, green = "#2F6BF3", "#B45309", "#B42318", "#1F7A4D"
    assert blue in htmls[0] and blue in htmls[1]      # issued, due soon
    assert amber in htmls[2] and amber in htmls[3]    # due, overdue
    assert red in htmls[4] and red in htmls[5]        # final, blocked
    assert green in htmls[6]                          # paid


def test_the_desk_is_not_told_to_write_to_itself():
    desk = _emails()[2]
    assert "Need a hand?" not in desk.html
    assert "replies to it are not read" not in desk.html
