"""Transactional email: invitations, password resets, and support requests.

This is *not* the patient-messaging outbox. That belongs to a clinic, is wired
to a clinic's own provider, and is scoped by tenant; this is the platform
speaking to a staff account, sometimes before that account can even sign in.
Keeping them apart means a clinic that has not configured Twilio can still
receive its own invitation.

Two transports:

*   ``console`` — writes the message, link and all, to the log. What
    development wants, and refused outright in production (`config.py`).
*   ``smtp`` — a real handover. `smtplib` is synchronous, so the send runs in a
    worker thread and never blocks the event loop.

Every message goes out as ``multipart/alternative``: a plain-text part and an
HTML one. Both are always built. Text-only mail looks like a phishing attempt
to a person and scores badly with spam filters; HTML-only mail is worse, since
a client that will not render it shows nothing at all.

A send that fails raises. Callers decide whether that should fail the request
(creating a clinic whose admin never gets the link is not a success) or merely
be recorded (a self-service reset answers the same either way).
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape

from app.core.config import settings

log = logging.getLogger("pms.mail")


class MailError(Exception):
    """The message could not be handed to a transport."""


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    text: str
    html: str | None = None
    #: where a reply goes — a support request answers to the person who asked
    reply_to: str | None = None

    def as_message(self) -> EmailMessage:
        message = EmailMessage()
        message["From"] = settings.mail_from
        message["To"] = self.to
        message["Subject"] = self.subject
        if self.reply_to:
            message["Reply-To"] = self.reply_to
        message["Auto-Submitted"] = "auto-generated"
        # Tells a client not to offer "unsubscribe" or threading on a message
        # that is a one-off answer to something the recipient just did.
        message["X-Auto-Response-Suppress"] = "All"
        message.set_content(self.text)
        if self.html:
            message.add_alternative(self.html, subtype="html")
        return message


def _send_smtp(email: Email) -> None:
    """Blocking. Always called through a thread."""
    try:
        with smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
        ) as server:
            if settings.smtp_starttls:
                server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(email.as_message())
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(str(exc)) from exc


async def send(email: Email) -> None:
    if settings.mail_provider == "console":
        # The plain-text part, because the link is the only thing that matters
        # in development and a developer needs to be able to read it.
        log.info(
            "email (console transport)\n  to: %s\n  subject: %s\n%s",
            email.to,
            email.subject,
            email.text,
        )
        return
    await asyncio.to_thread(_send_smtp, email)


# --- the HTML shell ----------------------------------------------------------
#
# Email is not the web. Three constraints shape everything below:
#
#   * every style is inline, because a `<style>` block is stripped by Gmail's
#     web client and by most of the rest once a message is forwarded;
#   * layout is nested tables, because flexbox and grid do not exist in
#     Outlook's Word rendering engine;
#   * there are no images. A logo would need hosting, would be blocked by
#     default in most clients, and would leak a read receipt to whoever hosts
#     it. The mark is a table cell with a background colour, which always
#     renders and tells nobody anything.
#
# The palette is the product's own (`globals.css`), so the message looks like
# the thing it is inviting somebody into.

_GROUND = "#F4F6FC"
_SURFACE = "#FFFFFF"
_LINE = "#EAEFF8"
_LINE_STRONG = "#DAE2F1"
_INK = "#0F1729"
_INK_2 = "#58637C"
_INK_3 = "#68768F"
_ACCENT = "#2F6BF3"
_ACCENT_SOFT = "#EAF1FE"
_ACCENT_BORDER = "#C6DAFC"
_ACCENT_TEXT = "#1A4FCC"
_SURFACE_2 = "#F5F7FD"

_SANS = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"
)
_SERIF = "Georgia,'Times New Roman',serif"
_MONO = "'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace"


def _shell(*, preheader: str, heading: str, body: str) -> str:
    """The frame every message shares.

    `preheader` is the line a client shows next to the subject in the inbox
    list. Left unset it picks up whatever text comes first, which is usually
    the logo alt or the greeting — so it is set deliberately and then hidden.
    """
    return f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{escape(heading)}</title>
</head>
<body style="margin:0;padding:0;background-color:{_GROUND};">
<div style="display:none;font-size:1px;color:{_GROUND};line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;">
{escape(preheader)}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;
</div>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color:{_GROUND};">
<tr><td align="center" style="padding:32px 16px;">

<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="width:100%;max-width:600px;">

  <!-- brand -->
  <tr><td style="padding:0 4px 20px 4px;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td width="36" style="width:36px;">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="36" style="width:36px;height:36px;background-color:{_ACCENT};border-radius:10px;">
            <tr><td align="center" valign="middle" style="height:36px;font-family:{_SERIF};font-size:19px;font-weight:700;color:#FFFFFF;line-height:36px;">A</td></tr>
          </table>
        </td>
        <td style="padding-left:11px;font-family:{_SANS};">
          <div style="font-family:{_SERIF};font-size:17px;font-weight:700;color:{_INK};line-height:1.1;letter-spacing:-0.2px;">Astrale</div>
          <div style="font-size:10px;font-weight:600;letter-spacing:1.6px;text-transform:uppercase;color:{_INK_3};padding-top:3px;">Practice OS</div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- card -->
  <tr><td style="background-color:{_SURFACE};border:1px solid {_LINE};border-radius:14px;padding:36px 36px 30px 36px;">
    <h1 style="margin:0 0 16px 0;font-family:{_SERIF};font-size:25px;line-height:1.22;font-weight:700;color:{_INK};letter-spacing:-0.4px;">{heading}</h1>
    {body}
  </td></tr>

  <!-- footer -->
  <tr><td style="padding:22px 8px 0 8px;font-family:{_SANS};font-size:12px;line-height:1.6;color:{_INK_3};">
    <p style="margin:0 0 6px 0;">
      Astrale Practice OS &mdash; multi-tenant patient management.
    </p>
    <p style="margin:0;">
      This message was sent automatically. Nobody monitors replies to this address.
    </p>
  </td></tr>

</table>

</td></tr>
</table>
</body>
</html>"""


def _paragraph(text: str, *, top: int = 0) -> str:
    return (
        f'<p style="margin:{top}px 0 14px 0;font-family:{_SANS};font-size:15px;'
        f'line-height:1.65;color:{_INK_2};">{text}</p>'
    )


def _button(url: str, label: str) -> str:
    """A bulletproof button: a table cell with a background, not a padded `<a>`.

    Outlook ignores padding on an anchor and would render this as bare blue
    text; a cell with `bgcolor` is the one shape every client agrees on. The
    rounded corners are the part that degrades — Outlook squares them off,
    which is a fair trade for a button that is always clickable.
    """
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:22px 0 8px 0;">
  <tr><td align="center" bgcolor="{_ACCENT}" style="background-color:{_ACCENT};border-radius:9px;">
    <a href="{escape(url, quote=True)}" style="display:inline-block;padding:13px 26px;font-family:{_SANS};font-size:15px;font-weight:600;color:#FFFFFF;text-decoration:none;border-radius:9px;">{label}</a>
  </td></tr>
</table>"""


def _fallback_link(url: str) -> str:
    """The URL as text, because a button is not always a link.

    Some clients strip the anchor, some people read mail in a terminal, and a
    `localhost` address is not linkified by Gmail at all — which is exactly the
    case this product hits in development.
    """
    safe = escape(url)
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:14px 0 0 0;">
  <tr><td style="background-color:{_SURFACE_2};border:1px solid {_LINE};border-radius:9px;padding:12px 14px;">
    <div style="font-family:{_SANS};font-size:11px;font-weight:600;letter-spacing:0.6px;text-transform:uppercase;color:{_INK_3};padding-bottom:6px;">Or paste this into your browser</div>
    <div style="font-family:{_MONO};font-size:12px;line-height:1.5;color:{_ACCENT_TEXT};word-break:break-all;">{safe}</div>
  </td></tr>
</table>"""


def _notice(text: str) -> str:
    """The one fact the recipient must not miss, set apart from the prose."""
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:20px 0 0 0;">
  <tr><td style="background-color:{_ACCENT_SOFT};border:1px solid {_ACCENT_BORDER};border-radius:9px;padding:12px 14px;font-family:{_SANS};font-size:13px;line-height:1.6;color:{_ACCENT_TEXT};">
    {text}
  </td></tr>
</table>"""


def _footnote(text: str) -> str:
    return f"""\
<div style="margin:26px 0 0 0;padding-top:18px;border-top:1px solid {_LINE_STRONG};font-family:{_SANS};font-size:12.5px;line-height:1.65;color:{_INK_3};">
  {text}
</div>"""


# --- the messages this application sends -------------------------------------


def invitation(*, to: str, name: str, clinic: str, url: str, expires_in_days: int) -> Email:
    days = "a day" if expires_in_days == 1 else f"{expires_in_days} days"
    safe_name = escape(name)
    safe_clinic = escape(clinic)

    text = (
        f"Hello {name},\n\n"
        f"An account has been created for you on {clinic}.\n\n"
        "Choose a password to finish setting it up:\n\n"
        f"    {url}\n\n"
        f"The link works once and expires in {days}. "
        "If it has already expired, ask an administrator to send another.\n\n"
        "If you were not expecting this, you can ignore this message — the "
        "account cannot be used until someone sets a password.\n"
    )

    html = _shell(
        preheader=f"Choose a password to finish setting up your {clinic} account.",
        heading=f"Set up your {safe_clinic} account",
        body=(
            _paragraph(f"Hello {safe_name},")
            + _paragraph(
                f"An account has been created for you on <strong style=\"color:{_INK};\">{safe_clinic}</strong>. "
                "It has no password yet — not a temporary one, and not one anybody has handed you. "
                "The password you choose here is the first this account will ever have."
            )
            + _button(url, "Choose your password")
            + _fallback_link(url)
            + _notice(
                f"This link works <strong>once</strong> and expires in <strong>{days}</strong>. "
                "If it has already expired, ask an administrator to send you another."
            )
            + _footnote(
                "If you were not expecting this, you can safely ignore it. The account cannot "
                "be signed in to until someone sets a password, so nothing happens if you do nothing."
            )
        ),
    )
    return Email(to=to, subject=f"Set up your {clinic} account", text=text, html=html)


def password_reset(*, to: str, name: str, clinic: str, url: str, expires_in_hours: int) -> Email:
    hours = "an hour" if expires_in_hours == 1 else f"{expires_in_hours} hours"
    safe_name = escape(name)
    safe_clinic = escape(clinic)

    text = (
        f"Hello {name},\n\n"
        f"Someone asked to reset the password for your {clinic} account.\n\n"
        "If that was you, choose a new one here:\n\n"
        f"    {url}\n\n"
        f"The link works once and expires in {hours}.\n\n"
        "If it was not you, nothing has changed and you can ignore this "
        "message. Your current password still works.\n"
    )

    html = _shell(
        preheader=f"Choose a new password for your {clinic} account.",
        heading=f"Reset your {safe_clinic} password",
        body=(
            _paragraph(f"Hello {safe_name},")
            + _paragraph(
                f"Someone asked to reset the password for your "
                f"<strong style=\"color:{_INK};\">{safe_clinic}</strong> account. "
                "If that was you, choose a new one below."
            )
            + _button(url, "Choose a new password")
            + _fallback_link(url)
            + _notice(
                f"This link works <strong>once</strong> and expires in <strong>{hours}</strong>. "
                "Using it signs this account out everywhere, on every device."
            )
            + _footnote(
                "If it was not you, nothing has changed and you can ignore this message — your "
                "current password still works. If you get these unexpectedly, tell an "
                "administrator at your practice."
            )
        ),
    )
    return Email(to=to, subject=f"Reset your {clinic} password", text=text, html=html)


def _facts(rows: list[tuple[str, str]]) -> str:
    """A label/value table — the details the desk would otherwise have to ask for."""
    cells = "".join(
        f'<tr><td style="padding:6px 14px 6px 0;font-family:{_SANS};font-size:12px;font-weight:600;'
        f'letter-spacing:0.4px;text-transform:uppercase;color:{_INK_3};white-space:nowrap;vertical-align:top;">{escape(label)}</td>'
        f'<td style="padding:6px 0;font-family:{_SANS};font-size:14px;line-height:1.5;color:{_INK};">{escape(value)}</td></tr>'
        for label, value in rows
    )
    return f"""<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:6px 0 4px 0;background-color:{_SURFACE_2};border:1px solid {_LINE};border-radius:9px;">
  <tr><td style="padding:10px 14px;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0">{cells}</table>
  </td></tr>
</table>"""


_SUPPORT_ASK = {
    "activation": ("Enable {module} for {clinic}", "asked for the {module} module to be enabled."),
    "walkthrough": ("Walkthrough of {module} for {clinic}", "asked for a walkthrough of the {module} module."),
    "general": ("Support request from {clinic}", "sent a support request."),
}


def support_request(
    *,
    to: str,
    kind: str,
    clinic: str,
    slug: str,
    module: str | None,
    module_key: str | None,
    price: str | None,
    requested_by: str,
    reply_to: str | None,
    message: str,
    console_url: str,
    reference: str,
) -> Email:
    """To the support desk, not to a clinic: one clinic asking for something.

    Everything the desk needs to act is in the body — which clinic, which
    module, who asked — and a reply goes straight back to the person who asked.
    """
    subject_t, line_t = _SUPPORT_ASK.get(kind, _SUPPORT_ASK["general"])
    module_name = module or "a module"
    subject = subject_t.format(module=module_name, clinic=clinic)
    line = line_t.format(module=module_name)

    rows = [("Clinic", f"{clinic} ({slug})")]
    if module:
        rows.append(("Module", f"{module} ({module_key})" if module_key else module))
    if price:
        rows.append(("Listed", f"{price} per month"))
    rows.append(("Asked by", requested_by))
    rows.append(("Reference", reference))

    text = "\n".join(
        [
            f"{clinic} {line}",
            "",
            *(f"{label + ':':<11}{value}" for label, value in rows),
            "",
            *(["Message:", message, ""] if message else []),
            f"Open the clinic in the console: {console_url}",
            "",
            "Reply to this email to answer the person who asked." if reply_to else "",
        ]
    ).rstrip() + "\n"

    body = _paragraph(f"<strong style=\"color:{_INK};\">{escape(clinic)}</strong> {escape(line)}") + _facts(rows)
    if message:
        body += _paragraph(
            f'<span style="white-space:pre-wrap;">{escape(message)}</span>', top=16
        )
    body += _button(console_url, "Open in the console")
    if reply_to:
        body += _footnote("Reply to this email to answer the person who asked.")

    html = _shell(preheader=subject, heading=escape(subject), body=body)
    return Email(to=to, subject=subject, text=text, html=html, reply_to=reply_to)
