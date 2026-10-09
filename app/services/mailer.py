"""Transactional email: invitations, password resets, and support requests.

This is *not* the patient-messaging outbox. That belongs to a clinic, is wired
to a clinic's own provider, and is scoped by tenant; this is the platform
speaking to a staff account, sometimes before that account can even sign in.
Keeping them apart means a clinic that has not configured Twilio can still
receive its own invitation.

Three transports:

*   ``console`` — writes the message, link and all, to the log. What
    development wants, and refused outright in production (`config.py`).
*   ``smtp`` — a real handover. `smtplib` is synchronous, so the send runs in a
    worker thread and never blocks the event loop.
*   ``ses`` — Amazon SES, through its v2 API. The message is built exactly as
    for SMTP and handed over raw, so both transports send the same bytes —
    headers, reply-to and both parts included. boto3 is blocking too, so it
    runs in a thread as well.

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
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape

from app.core import aws
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


def _send_ses(email: Email) -> None:
    """Blocking. Always called through a thread.

    SES only sends from an identity verified in the region it is asked in, so
    `MAIL_FROM` has to be (or sit under a domain that is) verified there. A
    refusal — unverified sender, sandbox recipient, throttling that outlasted
    the retries — is a `MailError` like any SMTP failure, and callers treat it
    the same way.
    """
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:  # pragma: no cover - boto3 is a declared dependency
        raise MailError("boto3 is not installed; `pip install boto3`.") from exc

    # Raw content: the MIME message already carries From, To, Reply-To and the
    # rest of the headers, exactly as the SMTP transport would send them.
    request: dict = {
        "FromEmailAddress": settings.mail_from,
        "Destination": {"ToAddresses": [email.to]},
        "Content": {"Raw": {"Data": email.as_message().as_bytes()}},
    }
    if settings.ses_configuration_set:
        request["ConfigurationSetName"] = settings.ses_configuration_set

    try:
        client = aws.client("sesv2", region=settings.ses_region)
        client.send_email(**request)
    except (aws.AwsUnavailable, BotoCoreError, ClientError) as exc:
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
    if settings.mail_provider == "ses":
        await asyncio.to_thread(_send_ses, email)
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
# The layout: a coloured header band that says who is writing and what kind
# of message this is (the eyebrow), the headline in the band, the body on
# white beneath it, and a footer that says why the recipient got it and where
# to get help. The band's colour is the message's *tone* — blue for ordinary
# business, amber for "this needs doing soon", red for "this has happened" —
# so a billing notice reads differently from an invitation before a word of
# it is read.
#
# The palette is the product's own (`globals.css`), so the message looks like
# the thing it is inviting somebody into.

_GROUND = "#F2F5FB"
_SURFACE = "#FFFFFF"
_LINE = "#E6ECF5"
_LINE_STRONG = "#D6DFEE"
_INK = "#0F1729"
_INK_2 = "#4A556B"
_INK_3 = "#6B7890"
_SURFACE_2 = "#F6F8FC"

_TONES = {
    # band, band text, soft ground, soft border, soft text, button
    "accent": ("#2F6BF3", "#FFFFFF", "#EAF1FE", "#C6DAFC", "#1A4FCC", "#2F6BF3"),
    "warn": ("#B45309", "#FFFFFF", "#FEF6E7", "#F5D9A6", "#8A4B06", "#B45309"),
    "critical": ("#B42318", "#FFFFFF", "#FDEEEC", "#F4C3BD", "#912018", "#B42318"),
    "ok": ("#1F7A4D", "#FFFFFF", "#E7F5EE", "#B7E0CA", "#17603C", "#1F7A4D"),
}

_SANS = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"
)
_SERIF = "Georgia,'Times New Roman',serif"
_MONO = "'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace"


def _shell(
    *,
    preheader: str,
    eyebrow: str,
    heading: str,
    body: str,
    recipient: str | None = None,
    reason: str = "",
    tone: str = "accent",
    to_the_desk: bool = False,
) -> str:
    """The frame every message shares.

    `heading` and `eyebrow` are HTML-escaped by the caller (the heading may
    carry markup); `preheader` is escaped here. `preheader` is the line a
    client shows next to the subject in the inbox list — left unset it picks
    up whatever text comes first, so it is set deliberately and then hidden.
    `recipient` and `reason` fill the footer's "why you got this" line.
    `to_the_desk` is for mail *to* the support desk: it is not told to write to
    itself for help, and its replies do go somewhere — to the person who asked.
    """
    band, band_ink, *_ = _TONES.get(tone, _TONES["accent"])
    help_line = "" if to_the_desk else (
        f'<p style="margin:0 0 8px 0;">Need a hand? Write to '
        f'<a href="mailto:{escape(settings.support_email)}" style="color:{_INK_2};text-decoration:underline;">'
        f'{escape(settings.support_email)}</a>.</p>'
    )
    automatic = "" if to_the_desk else (
        "<br>This message was sent automatically; replies to it are not read."
    )
    why = ""
    if recipient:
        why = (
            f'<p style="margin:0 0 8px 0;">This message was sent to '
            f'<span style="color:{_INK_2};">{escape(recipient)}</span>'
            f'{" " + reason if reason else ""}.</p>'
        )
    return f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{escape(eyebrow)}</title>
</head>
<body style="margin:0;padding:0;background-color:{_GROUND};">
<div style="display:none;font-size:1px;color:{_GROUND};line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;">
{escape(preheader)}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;
</div>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color:{_GROUND};">
<tr><td align="center" style="padding:28px 12px 36px 12px;">

<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="width:100%;max-width:600px;">

  <!-- card -->
  <tr><td style="background-color:{_SURFACE};border:1px solid {_LINE};border-radius:16px;overflow:hidden;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">

      <!-- band: who is writing, what kind of message, the headline -->
      <tr><td bgcolor="{band}" style="background-color:{band};padding:22px 32px 26px 32px;border-radius:15px 15px 0 0;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
          <tr>
            <td style="font-family:{_SANS};">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td width="30" style="width:30px;">
                    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="30" style="width:30px;height:30px;background-color:#FFFFFF;border-radius:8px;">
                      <tr><td align="center" valign="middle" style="height:30px;font-family:{_SERIF};font-size:16px;font-weight:700;color:{band};line-height:30px;">A</td></tr>
                    </table>
                  </td>
                  <td style="padding-left:10px;font-family:{_SERIF};font-size:16px;font-weight:700;color:{band_ink};letter-spacing:-0.2px;">Astrale <span style="font-family:{_SANS};font-size:11px;font-weight:600;letter-spacing:1.4px;text-transform:uppercase;opacity:0.85;">Practice OS</span></td>
                </tr>
              </table>
            </td>
            <td align="right" style="font-family:{_SANS};">
              <span style="display:inline-block;padding:4px 10px;border:1px solid rgba(255,255,255,0.55);border-radius:999px;font-size:10.5px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:{band_ink};">{eyebrow}</span>
            </td>
          </tr>
        </table>
        <h1 style="margin:22px 0 0 0;font-family:{_SERIF};font-size:26px;line-height:1.22;font-weight:700;color:{band_ink};letter-spacing:-0.4px;">{heading}</h1>
      </td></tr>

      <!-- body -->
      <tr><td style="padding:30px 32px 30px 32px;">
        {body}
      </td></tr>
    </table>
  </td></tr>

  <!-- footer -->
  <tr><td style="padding:22px 12px 0 12px;font-family:{_SANS};font-size:12px;line-height:1.6;color:{_INK_3};text-align:center;">
    {why}
    {help_line}
    <p style="margin:0;">Astrale Practice OS &middot; patient management for independent clinics.{automatic}</p>
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


def _button(url: str, label: str, *, tone: str = "accent") -> str:
    """A bulletproof button: a table cell with a background, not a padded `<a>`.

    Outlook ignores padding on an anchor and would render this as bare blue
    text; a cell with `bgcolor` is the one shape every client agrees on. The
    rounded corners are the part that degrades — Outlook squares them off,
    which is a fair trade for a button that is always clickable.
    """
    colour = _TONES.get(tone, _TONES["accent"])[5]
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:22px 0 8px 0;">
  <tr><td align="center" bgcolor="{colour}" style="background-color:{colour};border-radius:10px;">
    <a href="{escape(url, quote=True)}" style="display:inline-block;padding:14px 28px;font-family:{_SANS};font-size:15px;font-weight:600;color:#FFFFFF;text-decoration:none;border-radius:10px;">{label} &rarr;</a>
  </td></tr>
</table>"""


def _fallback_link(url: str) -> str:
    """The URL as text, because a button is not always a link.

    Some clients strip the anchor, some people read mail in a terminal, and a
    `localhost` address is not linkified by Gmail at all — which is exactly the
    case this product hits in development.
    """
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:14px 0 0 0;">
  <tr><td style="background-color:{_SURFACE_2};border:1px dashed {_LINE_STRONG};border-radius:10px;padding:12px 14px;">
    <div style="font-family:{_SANS};font-size:10.5px;font-weight:700;letter-spacing:0.8px;text-transform:uppercase;color:{_INK_3};padding-bottom:6px;">Button not working? Paste this into your browser</div>
    <div style="font-family:{_MONO};font-size:12px;line-height:1.5;color:{_TONES['accent'][4]};word-break:break-all;">{escape(url)}</div>
  </td></tr>
</table>"""


def _notice(text: str, *, tone: str = "accent") -> str:
    """The one fact the recipient must not miss, set apart from the prose."""
    _, _, soft, border, ink, _ = _TONES.get(tone, _TONES["accent"])
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:20px 0 0 0;">
  <tr><td style="background-color:{soft};border:1px solid {border};border-left:4px solid {ink};border-radius:10px;padding:13px 15px;font-family:{_SANS};font-size:13.5px;line-height:1.6;color:{ink};">
    {text}
  </td></tr>
</table>"""


def _steps(title: str, items: list[str]) -> str:
    """A numbered "what happens next" — the recipient's path, not the product's."""
    rows = "".join(
        f'<tr><td valign="top" width="30" style="width:30px;padding:0 0 12px 0;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="22" style="width:22px;height:22px;background-color:{_TONES["accent"][2]};border-radius:999px;">'
        f'<tr><td align="center" valign="middle" style="height:22px;font-family:{_SANS};font-size:11.5px;font-weight:700;color:{_TONES["accent"][4]};line-height:22px;">{n}</td></tr></table></td>'
        f'<td valign="top" style="padding:1px 0 12px 0;font-family:{_SANS};font-size:14px;line-height:1.55;color:{_INK_2};">{item}</td></tr>'
        for n, item in enumerate(items, start=1)
    )
    return f"""\
<div style="margin:26px 0 6px 0;font-family:{_SANS};font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:{_INK_3};">{escape(title)}</div>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">{rows}</table>"""


def _footnote(text: str) -> str:
    return f"""\
<div style="margin:24px 0 0 0;padding-top:18px;border-top:1px solid {_LINE_STRONG};font-family:{_SANS};font-size:12.5px;line-height:1.65;color:{_INK_3};">
  {text}
</div>"""


def _facts(rows: list[tuple[str, str]], *, highlight: int | None = None) -> str:
    """A label/value panel — the details somebody would otherwise have to ask
    for. `highlight` sets one row's value in bold, for the figure that matters."""
    cells = "".join(
        f'<tr><td style="padding:8px 16px 8px 0;font-family:{_SANS};font-size:11px;font-weight:700;'
        f'letter-spacing:0.6px;text-transform:uppercase;color:{_INK_3};white-space:nowrap;vertical-align:top;'
        f'{"border-top:1px solid " + _LINE + ";" if i else ""}">{escape(label)}</td>'
        f'<td style="padding:8px 0;font-family:{_SANS};font-size:14px;line-height:1.5;color:{_INK};'
        f'{"font-weight:700;font-size:16px;" if i == highlight else ""}'
        f'{"border-top:1px solid " + _LINE + ";" if i else ""}">{escape(value)}</td></tr>'
        for i, (label, value) in enumerate(rows)
    )
    return f"""<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:8px 0 6px 0;background-color:{_SURFACE_2};border:1px solid {_LINE};border-radius:10px;">
  <tr><td style="padding:6px 16px;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">{cells}</table>
  </td></tr>
</table>"""


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
        eyebrow="Invitation",
        heading=f"You're invited to {safe_clinic}",
        recipient=to,
        reason=f"because an administrator at {safe_clinic} created an account for this address",
        body=(
            _paragraph(f"Hello {safe_name},")
            + _paragraph(
                f"An account has been created for you on <strong style=\"color:{_INK};\">{safe_clinic}</strong>. "
                "It has no password yet — not a temporary one, and not one anybody has handed you. "
                "The password you choose is the first this account will ever have."
            )
            + _button(url, "Choose your password")
            + _fallback_link(url)
            + _steps("What happens next", [
                "Open the link and choose a password of at least 12 characters.",
                f"You are signed straight in to {safe_clinic}.",
                "From then on, sign in at your clinic's own address with this email.",
            ])
            + _notice(
                f"This link works <strong>once</strong> and expires in <strong>{days}</strong>. "
                "If it has already expired, ask an administrator to send you another."
            )
            + _footnote(
                "Not expecting this? You can safely ignore it. The account cannot be signed in "
                "to until someone sets a password, so nothing happens if you do nothing."
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
        eyebrow="Password reset",
        heading=f"Reset your {safe_clinic} password",
        recipient=to,
        reason="because a password reset was requested for it",
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
                "Wasn't you? Nothing has changed and you can ignore this message — your "
                "current password still works. If you get these unexpectedly, tell an "
                "administrator at your practice."
            )
        ),
    )
    return Email(to=to, subject=f"Reset your {clinic} password", text=text, html=html)


_SUPPORT_ASK = {
    "walkthrough": ("Walkthrough for {clinic}", "asked for a walkthrough."),
    "general": ("Support request from {clinic}", "sent a support request."),
}


def support_request(
    *,
    to: str,
    kind: str,
    clinic: str,
    slug: str,
    requested_by: str,
    reply_to: str | None,
    message: str,
    console_url: str,
    reference: str,
) -> Email:
    """To the support desk, not to a clinic: one clinic asking for something.

    Everything the desk needs to act is in the body — which clinic, who asked,
    what they said — and a reply goes straight back to the person who asked.
    """
    subject_t, line = _SUPPORT_ASK.get(kind, _SUPPORT_ASK["general"])
    subject = subject_t.format(clinic=clinic)

    rows = [("Clinic", f"{clinic} ({slug})")]
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
            f'<span style="display:block;padding:12px 14px;border-left:3px solid {_LINE_STRONG};'
            f'white-space:pre-wrap;">{escape(message)}</span>',
            top=16,
        )
    body += _button(console_url, "Open in the console")
    if reply_to:
        body += _footnote("Reply to this email to answer the person who asked.")

    eyebrow = {"walkthrough": "Walkthrough request"}.get(
        kind, "Support request"
    )
    html = _shell(
        preheader=subject, eyebrow=eyebrow, heading=escape(subject), body=body,
        recipient=to, reason="because it is the platform's support address",
        to_the_desk=True,
    )
    return Email(to=to, subject=subject, text=text, html=html, reply_to=reply_to)


# --- subscription rent --------------------------------------------------------------

#: stage -> (tone, eyebrow, subject/heading, opening line, notice or None)
_RENT_COPY: dict[str, tuple[str, str, str, str, str | None]] = {
    "issued": (
        "accent", "New bill", "Your {period} bill for {clinic}",
        "Here is {clinic}'s Astrale subscription bill for {period}. It is the flat monthly "
        "clinic fee, which covers every module.",
        "Please pay by <strong>{due}</strong>.",
    ),
    "due_soon": (
        "accent", "Payment reminder", "Your {period} bill is due on {due}",
        "A reminder that {clinic}'s subscription bill for {period} is due on {due}.",
        None,
    ),
    "due": (
        "warn", "Due today", "Your {period} bill is due today",
        "{clinic}'s subscription bill for {period} is due today.",
        "If it is still unpaid on <strong>{hold}</strong>, {clinic} will be put on hold.",
    ),
    "overdue": (
        "warn", "Overdue", "Your {period} bill is overdue",
        "{clinic}'s subscription bill for {period} was due on {due} and has not been paid yet.",
        "{clinic} will be put on hold on <strong>{hold}</strong> if it is still unpaid. "
        "Settle it before then and nothing changes.",
    ),
    "final": (
        "critical", "Final notice", "Last day to pay before {clinic} is put on hold",
        "{clinic}'s subscription bill for {period} is still unpaid, and today is the last "
        "day of the grace period.",
        "From <strong>{hold}</strong>, your team will be able to sign in and see this bill, "
        "and nothing else, until it is paid.",
    ),
    "blocked": (
        "critical", "Account on hold", "{clinic} is on hold",
        "{clinic} has been put on hold because its subscription bill for {period} is unpaid.",
        "Your team can still sign in and see what is owed. Patients, appointments and every "
        "other screen come back the moment the bill is settled — nothing has been deleted.",
    ),
    "paid": (
        "ok", "Payment received", "Thank you — your {period} bill is paid",
        "We have recorded payment of {clinic}'s subscription bill for {period}.",
        None,
    ),
}


def rent_notice(
    *,
    to: str,
    name: str,
    clinic: str,
    stage: str,
    number: str,
    period: str,
    amount: str,
    lines: list[tuple[str, str]],
    due_on,
    hold_on,
    reference: str = "",
    url: str,
) -> Email:
    """To a clinic's practice admins: one bill, at one rung of the ladder."""
    tone, eyebrow, heading_t, opening_t, notice_t = _RENT_COPY[stage]
    due = due_on.strftime("%d %b %Y")
    hold = hold_on.strftime("%d %b %Y")
    fill = {"clinic": clinic, "period": period, "due": due, "hold": hold}
    heading = heading_t.format(**fill)
    subject = f"{heading} — {amount}" if stage not in ("paid", "blocked") else heading

    rows = [("Bill", number), ("Period", period), ("Amount", amount)]
    if stage == "paid":
        if reference:
            rows.append(("Reference", reference))
    else:
        rows.append(("Due", due))
        if stage not in ("blocked",):
            rows.append(("Hold from", hold))
    lines_text = "\n".join(f"  {label:<24}{value}" for label, value in lines)

    safe = {k: escape(v) for k, v in fill.items()}
    body = _paragraph(f"Hello {escape(name)},") + _paragraph(opening_t.format(**safe))
    body += _facts(rows, highlight=2)
    if lines:
        body += _facts(lines)
    if notice_t:
        body += _notice(notice_t.format(**safe), tone="accent" if tone == "ok" else tone)
    if stage != "paid":
        body += _button(url, "View the bill", tone=tone if tone != "ok" else "accent")
        body += _footnote(
            f"Already paid? Reply to {escape(settings.support_email)} with the payment reference "
            "and we will match it to this bill."
        )

    text = "\n".join(
        [
            f"Hello {name},",
            "",
            opening_t.format(**fill),
            "",
            *(f"{label + ':':<11}{value}" for label, value in rows),
            "",
            *(["Charges:", lines_text, ""] if lines else []),
            *([re.sub(r"<[^>]+>", "", notice_t.format(**fill)), ""] if notice_t else []),
            *([f"View the bill: {url}"] if stage != "paid" else []),
        ]
    ).rstrip() + "\n"

    html = _shell(
        preheader=f"{heading} — {amount}", eyebrow=eyebrow, heading=escape(heading), body=body,
        recipient=to, reason=f"because you are a practice admin at {escape(clinic)}", tone=tone,
    )
    return Email(to=to, subject=subject, text=text, html=html)
