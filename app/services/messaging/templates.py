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

BODIES: dict[MessageTemplate, str] = {
    MessageTemplate.BOOKING_CONFIRMATION: (
        "Hi {patient_name}, your appointment at {clinic_name} with {doctor_name} "
        "is confirmed for {date} at {time}. Reply to this message to change it."
    ),
    MessageTemplate.APPOINTMENT_RESCHEDULED: (
        "Hi {patient_name}, your appointment at {clinic_name} has been moved from "
        "{was_date} at {was_time} to {date} at {time} with {doctor_name}. "
        "Reply to this message if that does not suit you."
    ),
    MessageTemplate.APPOINTMENT_CANCELLED: (
        "Hi {patient_name}, your appointment at {clinic_name} on {date} at {time} "
        "has been cancelled. Reply to this message to rebook."
    ),
    MessageTemplate.APPOINTMENT_REMINDER: (
        "Hi {patient_name}, a reminder of your appointment at {clinic_name} with "
        "{doctor_name} on {date} at {time}. Please arrive 10 minutes early."
    ),
    MessageTemplate.FOLLOW_UP: (
        "Hi {patient_name}, thank you for visiting {clinic_name}. "
        "If you have any questions about your visit, reply to this message."
    ),
}

MAX_BODY = 1200


class _Blank(dict):
    def __missing__(self, key: str) -> str:
        return ""


def render(template: MessageTemplate, variables: dict[str, str]) -> str:
    """The message body for this template, filled in and length-capped."""
    if template == MessageTemplate.CUSTOM:
        raise ValueError("A custom message carries its own body.")
    body = string.Formatter().vformat(BODIES[template], (), _Blank(variables))
    return " ".join(body.split())[:MAX_BODY]
