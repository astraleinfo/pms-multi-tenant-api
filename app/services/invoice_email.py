"""An invoice, emailed to the patient it bills.

The message goes out through the platform's own mail transport (`mailer`) —
the one that sends invitations — never through credentials a clinic types in.
What the clinic decides, in Settings, is how it reads and where a reply goes:

*   `invoice_email_reply_to` — where the patient's reply lands. Unset, it falls
    back to the clinic's contact email; with neither, there is no Reply-To and
    the message tells the patient replies are not read. The send still goes
    ahead: a clinic that has never opened this corner of Settings can still
    email a bill, and it reads like one from the clinic.
*   `invoice_email_sender_name` — the name the message is from, shown as
    "<name> via Astrale PMS" on the platform's address. Unset, the clinic's.
*   `invoice_email_subject` and `invoice_email_message` — the wording, with the
    placeholders below. Unset, the defaults below.

Placeholders are filled by one regular-expression pass over `{name}` tokens,
never by `str.format`: a template cannot reach an attribute or an index, and a
value that happens to contain `{amount}` is not filled in again. A template
with anything brace-shaped that is not a known placeholder is refused when it
is saved, so what is stored is always something this module can fill.

Every attempt — sent or not — is an `invoice.emailed` audit row naming who
sent it and to whom; a successful one also stamps `emailed_at`/`emailed_to`
on the invoice, which is what the screen's "last sent" reads.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

from email_validator import EmailNotValidError, validate_email
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, ValidationFailed
from app.models.enums import InvoiceStatus
from app.models.ops import Invoice
from app.models.tenancy import TenantConfig
from app.services import audit, mailer, platform_logo
from app.services.pdf_fonts import CURRENCY_SYMBOLS

log = logging.getLogger("pms.billing")

#: name -> what it is filled with. The web's Settings page offers the same list.
PLACEHOLDERS: dict[str, str] = {
    "patient_name": "the patient's full name",
    "invoice_number": "the invoice number",
    "clinic_name": "the clinic's name",
    "amount": "the invoice total",
    "balance": "what is still owed on it",
    "due_date": "the date it is due",
}

DEFAULT_SUBJECT = "Invoice {invoice_number} from {clinic_name}"
DEFAULT_MESSAGE = (
    "Dear {patient_name},\n\n"
    "Please find attached invoice {invoice_number} for {amount}.\n\n"
    "Thank you for choosing {clinic_name}."
)

_TOKEN = re.compile(r"\{([^{}]*)\}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# --- the rules, shared with the settings schema --------------------------------------


def template_problem(text: str) -> str | None:
    """Why `text` cannot be stored as a subject or message — or `None`."""
    for name in _TOKEN.findall(text):
        if name not in PLACEHOLDERS:
            known = ", ".join(f"{{{p}}}" for p in PLACEHOLDERS)
            return f"{{{name}}} is not a placeholder this message can fill. Use one of {known}."
    if "{" in _TOKEN.sub("", text) or "}" in _TOKEN.sub("", text):
        return "A brace has to be part of a placeholder, such as {patient_name}."
    return None


def subject_problem(text: str) -> str | None:
    if "\n" in text or "\r" in text:
        return "A subject is one line."
    return template_problem(text)


def sender_name_problem(text: str) -> str | None:
    """A display name, not an address: no line breaks, and nothing that would
    make the From line read as somebody else's address."""
    if _CONTROL.search(text):
        return "A sender name is one line of plain text."
    if any(ch in text for ch in "@<>\""):
        return "A sender name cannot contain @, <, > or quotation marks — it is a name, not an address."
    return None


def normalise_address(value: str) -> str:
    """One email address, normalised and lower-cased. Raises `ValueError`
    (`EmailNotValidError` is one) saying what is wrong with it."""
    return validate_email(value, check_deliverability=False).normalized.lower()


def render(text: str, values: dict[str, str]) -> str:
    """Fill the placeholders in one pass. An unknown token — which a saved
    template cannot hold — is left exactly as it was written."""
    return _TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), text)


# --- sending -------------------------------------------------------------------------


def _money(value: float, code: str) -> str:
    symbol = CURRENCY_SYMBOLS.get(code, "")
    return f"{symbol}{value:,.2f}" if symbol else f"{code} {value:,.2f}".strip()


def _day(value: date) -> str:
    return value.strftime("%d %b %Y")


def recipient(on_file: str | None, typed: str | None) -> str:
    """Who it goes to: the address staff typed, or else the one on file."""
    if typed:
        return typed
    if not (on_file or "").strip():
        raise ValidationFailed(
            "This patient has no email address on file. Add one to their record, "
            "or type the address to send this invoice to."
        )
    try:
        return normalise_address(on_file.strip())
    except EmailNotValidError as exc:
        raise ValidationFailed(
            f"The email on this patient's record ({on_file.strip()}) is not a valid address. "
            "Correct it on their record, or type the address to send this invoice to."
        ) from exc


def check_sendable(invoice: Invoice, typed: str | None) -> str:
    """Refuse what is never sent; otherwise, the address it goes to."""
    if invoice.status == InvoiceStatus.VOID:
        raise Conflict("This invoice is void. A void invoice is not sent to a patient.")
    if invoice.status == InvoiceStatus.DRAFT:
        raise Conflict("Issue this invoice before sending it — a draft can still change.")
    return recipient(invoice.patient.email, typed)


def reply_address(config: TenantConfig) -> str | None:
    """Where a reply goes: the invoice reply-to, else the clinic's contact
    email, else nowhere."""
    for candidate in (config.invoice_email_reply_to, config.contact_email):
        if candidate:
            try:
                return normalise_address(candidate)
            except EmailNotValidError:
                continue
    return None


async def send(
    session: AsyncSession,
    *,
    invoice: Invoice,
    totals: dict[str, float],
    pdf: bytes,
    filename: str,
    clinic: str,
    config: TenantConfig,
    currency: str,
    to: str | None,
    actor_kind: str,
    actor_id,
    actor_label: str,
) -> tuple[str, str, str]:
    """Email one invoice to its patient. Returns `(status, to, detail)`.

    Refusals — a void or draft invoice, nobody to send to — raise before
    anything is sent (`check_sendable`, which the caller has usually already
    asked, before drawing the PDF). A transport that will not take the message
    is not a refusal: it is recorded as `failed` and answered as such, so the
    attempt stays in the trail (raising would roll the audit row back with it).
    """
    address = check_sendable(invoice, to)
    values = {
        "patient_name": invoice.patient.full_name,
        "invoice_number": invoice.number,
        "clinic_name": clinic,
        "amount": _money(totals["total"], currency),
        "balance": _money(totals["balance"], currency),
        "due_date": _day(invoice.due_at),
    }
    # A subject is one header line whatever a patient's name holds.
    subject = " ".join(render(config.invoice_email_subject or DEFAULT_SUBJECT, values).split())
    message = render(config.invoice_email_message or DEFAULT_MESSAGE, values)
    sender = config.invoice_email_sender_name or clinic

    email = mailer.invoice_to_patient(
        to=address,
        clinic=clinic,
        from_name=f"{sender} via {mailer.platform_name()}",
        reply_to=reply_address(config),
        subject=subject,
        message=message,
        number=invoice.number,
        issued=_day(invoice.issued_at),
        due=_day(invoice.due_at),
        total=values["amount"],
        balance=values["balance"],
        owing=totals["balance"] > 0.005,
        pdf=pdf,
        filename=filename,
        mark=await platform_logo.email_mark(session),
    )

    meta = {"to": address, "number": invoice.number}
    try:
        await mailer.send(email)
    except mailer.MailError as exc:
        # The error, not the message: the body names a patient.
        log.error("invoice %s could not be emailed: %s", invoice.id, exc)
        await audit.record(
            session, action="invoice.emailed", tenant_id=invoice.tenant_id, actor_kind=actor_kind,
            actor_id=actor_id, actor_label=actor_label, entity="invoice", entity_id=str(invoice.id),
            outcome="failed", meta={**meta, "error": str(exc)[:300]},
        )
        return "failed", address, "The mail server did not accept the message. Try again in a moment."

    invoice.emailed_at = datetime.now(timezone.utc)
    invoice.emailed_to = address
    await audit.record(
        session, action="invoice.emailed", tenant_id=invoice.tenant_id, actor_kind=actor_kind,
        actor_id=actor_id, actor_label=actor_label, entity="invoice", entity_id=str(invoice.id),
        meta=meta,
    )
    return "sent", address, f"Sent to {address}."
