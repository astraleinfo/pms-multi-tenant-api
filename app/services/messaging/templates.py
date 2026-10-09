"""What each message says.

Rendering happens once, at enqueue time, and the result is stored on the row.
A template edited on Tuesday must not change what a message queued on Monday
says, and a patient renamed after the fact must not rewrite history.

`{placeholders}` are filled from a plain dict. A missing key renders as an
empty string rather than raising: a half-configured clinic should send a
slightly bare message, not 500 on the booking that triggered it.
"""

from __future__ import annotations

import string

from app.models.enums import MessageTemplate

# How these are written:
#
#   * The clinic's name leads, the way a sender ID would. Many phones show only
#     the first line in a notification, and "Northgate Dental: ..." is what
#     tells the patient this is not spam before they open it.
#   * The facts come first — who, what day, what time — and the ask comes last,
#     with a way to act on it: the clinic's phone when it has one.
#   * Plain GSM-7 characters only. One curly quote, dash or emoji turns the
#     whole message into UCS-2, which cuts a segment from 160 characters to 70
#     and roughly doubles what every clinic pays per message.
#
# `{contact}` is filled by `render` — "call +44 161 496 0110 or reply here", or
# "reply to this text" for a clinic with no number set.
BODIES: dict[MessageTemplate, str] = {
    MessageTemplate.BOOKING_CONFIRMATION: (
        "{clinic_name}: Hi {patient_name}, you're booked with {doctor_name} on "
        "{weekday} {date} at {time}. To change it, {contact}."
    ),
    MessageTemplate.APPOINTMENT_RESCHEDULED: (
        "{clinic_name}: Hi {patient_name}, your appointment has moved from {was_date} "
        "{was_time} to {weekday} {date} at {time} with {doctor_name}. "
        "If that doesn't suit you, {contact}."
    ),
    MessageTemplate.APPOINTMENT_CANCELLED: (
        "{clinic_name}: Hi {patient_name}, your appointment on {weekday} {date} at {time} "
        "has been cancelled. To book a new time, {contact}."
    ),
    MessageTemplate.FOLLOW_UP: (
        "{clinic_name}: Hi {patient_name}, thank you for visiting. Any questions about "
        "your visit or treatment? Please {contact}."
    ),
}

#: Characters a GSM-7 message may carry without switching the whole message to
#: UCS-2. The basic set plus the extension table's common members.
GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà^{}[~]|€"
)

MAX_BODY = 1200


class _Blank(dict):
    def __missing__(self, key: str) -> str:
        return ""


def contact_phrase(phone: str | None) -> str:
    """How the patient answers: by phone when the clinic has one, by reply always."""
    phone = (phone or "").strip()
    return f"call {phone} or reply here" if phone else "reply to this text"


def render(template: MessageTemplate, variables: dict[str, str]) -> str:
    """The message body for this template, filled in and length-capped."""
    if template == MessageTemplate.CUSTOM:
        raise ValueError("A custom message carries its own body.")
    values = _Blank(variables)
    values.setdefault("contact", contact_phrase(variables.get("clinic_phone")))
    # A doctor-less booking still reads as a sentence.
    if not values.get("doctor_name"):
        values["doctor_name"] = "the clinic"
    body = string.Formatter().vformat(BODIES[template], (), values)
    return " ".join(body.split())[:MAX_BODY]
