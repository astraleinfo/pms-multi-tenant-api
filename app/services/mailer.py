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
#   * there are no images and no webfonts. A logo or a font file would need
#     hosting, would be blocked by default in most clients, and would leak a
#     read receipt to whoever hosts it. The mark is a table cell with a
#     background colour, which always renders and tells nobody anything.
#
# The look is the application's own (`globals.css` in the web app), and the
# idea is that a message *opens on the sign-in stage*: the dark slate pane
# with the blue glow that every link in these messages lands on. So the top
# of the card is that stage — the lockup, the clinic, what kind of message
# this is, the headline, and the one thing to do, all before the fold. A
# bill puts its amount there too, as the screen's one hero figure.
#
# Below the stage the card turns to the app's daylight: an off-white surface,
# hairlines, the details somebody would otherwise have to ask for. One
# typeface (Google Sans where the reader has it), weights that stop at 600,
# labels in sentence case.
#
# Colour is spent only where it means something. The message's *tone* — blue
# for ordinary business, amber for "this needs doing soon", red for "this has
# happened", green for "done" — is a dot and a word over the headline and the
# callout below it, never colour alone. On the stage the tones use the app's
# dark-theme values, which are the ones drawn to read on slate.

_GROUND = "#F1F4F9"
_SURFACE = "#FCFDFF"
_SURFACE_2 = "#F4F7FB"
_LINE = "#E2E7F0"
_ROW_LINE = "#E9EDF5"
_INK = "#141C2B"
_INK_2 = "#3D475B"
_INK_3 = "#4E596E"

_STAGE = "#151C28"
_STAGE_LINE = "#2B3446"
_STAGE_INK = "#EEF2F9"
_STAGE_DIM = "#9BA6BB"

_ACCENT = "#2F62C8"
_ACCENT_TEXT = "#204B9E"


@dataclass(frozen=True)
class _Tone:
    soft: str        # a tinted ground, on the light card
    border: str
    text: str        # ink on the soft ground
    stage_dot: str   # the tone's dot, drawn to read on the dark stage
    stage_text: str  # the tone's word, on the stage
    button: str      # the filled button this tone asks for


_TONES = {
    "accent": _Tone("#E9EFFB", "#C6D6F1", "#204B9E", "#7C9FE8", "#A6C0F5", "#2F62C8"),
    # The app never fills a button amber — white on it does not clear 4.5:1 —
    # so a warning keeps the ordinary blue button and says so in its words.
    "warn": _Tone("#FAF2E2", "#EAD5A8", "#7C4D07", "#D9A253", "#E8B873", "#2F62C8"),
    "critical": _Tone("#FBEDEB", "#F0CDC8", "#9E2C22", "#E8776A", "#F09A8C", "#C43D32"),
    "ok": _Tone("#E7F3EE", "#B5DAC7", "#0E6447", "#4FBB8D", "#6FCFA6", "#2F62C8"),
}

_SANS = (
    "'Google Sans','Google Sans Text',Roboto,-apple-system,BlinkMacSystemFont,"
    "'Segoe UI','Helvetica Neue',Arial,sans-serif"
)

#: the stage's glow, as on the sign-in pane. A client that does not draw
#: gradients drops the declaration and keeps the flat slate beneath it.
_GLOW = (
    "radial-gradient(560px 320px at 0% 0%, rgba(47,98,200,0.42), rgba(47,98,200,0) 68%),"
    "radial-gradient(460px 300px at 100% 100%, rgba(47,98,200,0.20), rgba(47,98,200,0) 70%)"
)


def _tone(name: str) -> _Tone:
    return _TONES.get(name, _TONES["accent"])


def _stage(
    *,
    clinic: str,
    eyebrow: str,
    heading: str,
    lead: str,
    figure: tuple[str, str] | None,
    action: tuple[str, str] | None,
    tone: str,
) -> str:
    t = _tone(tone)
    lead_html = (
        f'<p style="margin:12px 0 0 0;font-family:{_SANS};font-size:16px;line-height:1.55;color:{_STAGE_DIM};">{lead}</p>'
        if lead else ""
    )
    figure_html = ""
    if figure:
        amount, caption = figure
        figure_html = f"""
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:26px 0 0 0;border-top:1px solid {_STAGE_LINE};">
          <tr><td style="padding:20px 0 0 0;font-family:{_SANS};">
            <div style="font-size:36px;line-height:1.08;font-weight:600;letter-spacing:-0.8px;color:{_STAGE_INK};font-variant-numeric:tabular-nums;">{escape(amount)}</div>
            <div style="margin-top:6px;font-size:14px;line-height:1.5;color:{_STAGE_DIM};">{caption}</div>
          </td></tr>
        </table>"""
    action_html = ""
    if action:
        url, label = action
        colour = t.button
        action_html = f"""
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:28px 0 0 0;">
          <tr><td align="center" bgcolor="{colour}" style="background-color:{colour};border-radius:8px;">
            <a href="{escape(url, quote=True)}" style="display:inline-block;padding:13px 24px;font-family:{_SANS};font-size:15px;line-height:20px;font-weight:500;color:#FFFFFF;text-decoration:none;border-radius:8px;">{label}</a>
          </td></tr>
        </table>"""
    return f"""\
<tr><td bgcolor="{_STAGE}" style="background-color:{_STAGE};border-radius:13px 13px 0 0;">
<div style="background-image:{_GLOW};border-radius:13px 13px 0 0;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
  <tr><td style="padding:26px 36px 36px 36px;">

    <!-- lockup, and the clinic this is about -->
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr>
        <td valign="middle">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td width="34" style="width:34px;">
                <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="34" style="width:34px;height:34px;background-color:{_ACCENT};border-radius:10px;">
                  <tr><td align="center" valign="middle" style="height:34px;font-family:{_SANS};font-size:16px;font-weight:600;color:#FFFFFF;line-height:34px;">A</td></tr>
                </table>
              </td>
              <td style="padding-left:11px;font-family:{_SANS};">
                <div style="font-size:15px;font-weight:600;line-height:1;color:{_STAGE_INK};">Astrale</div>
                <div style="margin-top:5px;font-size:10.5px;font-weight:500;line-height:1;letter-spacing:1.6px;text-transform:uppercase;color:{_STAGE_DIM};">Practice OS</div>
              </td>
            </tr>
          </table>
        </td>
        <td align="right" valign="middle" style="font-family:{_SANS};font-size:13px;line-height:1.4;color:{_STAGE_DIM};">{clinic}</td>
      </tr>
    </table>

    <!-- what kind of message, and what it says -->
    <div style="margin:44px 0 0 0;font-family:{_SANS};font-size:13px;line-height:1.4;font-weight:500;color:{t.stage_text};">
      <span style="display:inline-block;width:8px;height:8px;margin:0 8px 1px 0;border-radius:999px;background-color:{t.stage_dot};vertical-align:middle;"></span>{eyebrow}
    </div>
    <h1 style="margin:10px 0 0 0;font-family:{_SANS};font-size:28px;line-height:1.18;font-weight:600;color:{_STAGE_INK};letter-spacing:-0.6px;">{heading}</h1>
    {lead_html}
    {figure_html}
    {action_html}

  </td></tr>
</table>
</div>
</td></tr>"""


def _shell(
    *,
    preheader: str,
    clinic: str,
    eyebrow: str,
    heading: str,
    body: str,
    lead: str = "",
    figure: tuple[str, str] | None = None,
    action: tuple[str, str] | None = None,
    recipient: str | None = None,
    reason: str = "",
    tone: str = "accent",
    to_the_desk: bool = False,
) -> str:
    """The frame every message shares: the stage, then the card's details.

    Everything that is HTML here is escaped by the caller — `clinic`,
    `eyebrow`, `heading`, `lead`, the figure's caption, the action's label
    (they may carry markup) — except `preheader`, the figure's amount and the
    action's URL, which are escaped here. `preheader` is the line a client
    shows next to the subject in the inbox list; left unset it picks up
    whatever text comes first, so it is set deliberately and then hidden.

    `figure` is (amount, caption) — the one number this message is about.
    `action` is (url, label) — the one thing to do, set on the stage.
    `recipient` and `reason` fill the footer's "why you got this" line.
    `to_the_desk` is for mail *to* the support desk: it is not told to write to
    itself for help, and its replies do go somewhere — to the person who asked.
    """
    help_line = "" if to_the_desk else (
        f'<p style="margin:0 0 6px 0;">Need a hand? Write to '
        f'<a href="mailto:{escape(settings.support_email)}" style="color:{_ACCENT_TEXT};text-decoration:underline;">'
        f'{escape(settings.support_email)}</a>.</p>'
    )
    automatic = "" if to_the_desk else (
        " Sent automatically; replies to this address are not read."
    )
    why = ""
    if recipient:
        why = (
            f'<p style="margin:0 0 6px 0;">Sent to '
            f'<span style="color:{_INK_2};">{escape(recipient)}</span>'
            f'{" " + reason if reason else ""}.</p>'
        )
    stage = _stage(
        clinic=clinic, eyebrow=eyebrow, heading=heading, lead=lead,
        figure=figure, action=action, tone=tone,
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
<tr><td align="center" style="padding:32px 12px 40px 12px;">

<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="width:100%;max-width:600px;">

  <!-- card -->
  <tr><td style="background-color:{_SURFACE};border:1px solid {_LINE};border-radius:14px;overflow:hidden;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      {stage}
      <tr><td style="padding:32px 36px 34px 36px;">
        {body}
      </td></tr>
    </table>
  </td></tr>

  <!-- footer -->
  <tr><td style="padding:24px 28px 0 28px;font-family:{_SANS};font-size:13px;line-height:1.55;color:{_INK_3};text-align:center;">
    {why}
    {help_line}
    <p style="margin:0;">Astrale Practice OS, patient management for independent clinics.{automatic}</p>
  </td></tr>

</table>

</td></tr>
</table>
</body>
</html>"""


def _paragraph(text: str) -> str:
    return (
        f'<p style="margin:0 0 14px 0;font-family:{_SANS};font-size:15px;'
        f'line-height:1.6;color:{_INK_2};">{text}</p>'
    )


def _strong(text: str) -> str:
    """Emphasis the app's way: the full ink and the 600 step, never 700."""
    return f'<strong style="color:{_INK};font-weight:600;">{text}</strong>'


def _fallback_link(url: str) -> str:
    """The URL as text, because a button is not always a link.

    Some clients strip the anchor, some people read mail in a terminal, and a
    `localhost` address is not linkified by Gmail at all — which is exactly the
    case this product hits in development.
    """
    return f"""\
<p style="margin:14px 0 0 0;font-family:{_SANS};font-size:13px;line-height:1.55;color:{_INK_3};">
  Button not working? Paste this link into your browser:<br>
  <span style="color:{_ACCENT_TEXT};word-break:break-all;">{escape(url)}</span>
</p>"""


def _notice(text: str, *, tone: str = "accent") -> str:
    """The one fact the recipient must not miss: the app's callout, a soft
    tinted line across the width with the tone's own ink."""
    t = _tone(tone)
    # A bare <strong> is 700 in every client; the app's emphasis stops at 600.
    text = text.replace("<strong>", '<strong style="font-weight:600;">')
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:22px 0 0 0;">
  <tr><td style="background-color:{t.soft};border:1px solid {t.border};border-radius:10px;padding:13px 16px;font-family:{_SANS};font-size:14px;line-height:1.55;color:{t.text};">
    {text}
  </td></tr>
</table>"""


def _steps(title: str, items: list[str]) -> str:
    """A numbered "what happens next" — the recipient's path, not the product's.

    The numbers hang on a hairline, so the three steps read as one path rather
    than three bullets."""
    t = _tone("accent")
    last = len(items)
    rows = "".join(
        f'<tr><td valign="top" width="40" style="width:40px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="26" style="width:26px;height:26px;background-color:{t.soft};border:1px solid {t.border};border-radius:999px;">'
        f'<tr><td align="center" valign="middle" style="height:26px;font-family:{_SANS};font-size:12px;font-weight:600;color:{t.text};line-height:26px;">{n}</td></tr></table></td>'
        f'<td valign="top" style="padding:3px 0 {"0" if n == last else "16px"} 0;font-family:{_SANS};font-size:15px;line-height:1.5;color:{_INK_2};">{item}</td></tr>'
        for n, item in enumerate(items, start=1)
    )
    return f"""\
<div style="margin:28px 0 14px 0;font-family:{_SANS};font-size:16px;line-height:1.4;font-weight:600;color:{_INK};letter-spacing:-0.1px;">{escape(title)}</div>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">{rows}</table>"""


def _footnote(text: str) -> str:
    return f"""\
<div style="margin:28px 0 0 0;padding-top:18px;border-top:1px solid {_LINE};font-family:{_SANS};font-size:13px;line-height:1.55;color:{_INK_3};">
  {text}
</div>"""


def _quote(text: str) -> str:
    """Words somebody typed, set apart as theirs and kept as they wrote them."""
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:18px 0 0 0;">
  <tr><td style="background-color:{_SURFACE_2};border:1px solid {_LINE};border-radius:10px;padding:14px 16px;font-family:{_SANS};font-size:15px;line-height:1.6;color:{_INK};white-space:pre-wrap;">{text}</td></tr>
</table>"""


def _facts(rows: list[tuple[str, str]], *, title: str = "") -> str:
    """A label/value list — the details somebody would otherwise have to ask
    for, laid out like a row of the app's data lists: the label quiet on the
    left, the value in full ink on the right, hairlines between."""
    head = (
        f'<div style="margin:24px 0 4px 0;font-family:{_SANS};font-size:16px;line-height:1.4;'
        f'font-weight:600;color:{_INK};letter-spacing:-0.1px;">{escape(title)}</div>'
        if title else ""
    )
    last = len(rows) - 1
    rule = f"border-bottom:1px solid {_ROW_LINE};"
    cells = "".join(
        f'<tr><td style="padding:11px 16px 11px 0;font-family:{_SANS};font-size:14px;line-height:1.5;'
        f'color:{_INK_3};white-space:nowrap;vertical-align:top;{"" if i == last else rule}">{escape(label)}</td>'
        f'<td align="right" style="padding:11px 0;font-family:{_SANS};font-size:14px;line-height:1.5;color:{_INK};'
        f'font-weight:500;font-variant-numeric:tabular-nums;{"" if i == last else rule}">{escape(value)}</td></tr>'
        for i, (label, value) in enumerate(rows)
    )
    return f"""{head}<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:0 0 4px 0;">{cells}</table>"""


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
        clinic=safe_clinic,
        eyebrow="Invitation",
        heading=f"You're invited to {safe_clinic}",
        lead="Choose a password to finish setting up your account.",
        action=(url, "Choose your password"),
        recipient=to,
        reason=f"because an administrator at {safe_clinic} created an account for this address",
        body=(
            _paragraph(f"Hello {safe_name},")
            + _paragraph(
                f"An account has been created for you on {_strong(safe_clinic)}. "
                "It has no password yet — not a temporary one, and not one anybody has handed you. "
                "The password you choose is the first this account will ever have."
            )
            + _steps("What happens next", [
                "Open the link and choose a password of at least 12 characters.",
                f"You are signed straight in to {safe_clinic}.",
                "From then on, sign in at your clinic's own address with this email.",
            ])
            + _notice(
                f"This link works <strong>once</strong> and expires in <strong>{days}</strong>. "
                "If it has already expired, ask an administrator to send you another."
            )
            + _fallback_link(url)
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
        clinic=safe_clinic,
        eyebrow="Password reset",
        heading="Reset your password",
        lead=f"Someone asked to reset the password for your {safe_clinic} account. "
             "If that was you, choose a new one.",
        action=(url, "Choose a new password"),
        recipient=to,
        reason="because a password reset was requested for it",
        body=(
            _paragraph(f"Hello {safe_name},")
            + _paragraph(
                f"The button above opens a page where you can set a new password for "
                f"{_strong(safe_clinic)}. Choose one of at least 12 characters."
            )
            + _notice(
                f"This link works <strong>once</strong> and expires in <strong>{hours}</strong>. "
                "Using it signs this account out everywhere, on every device."
            )
            + _fallback_link(url)
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

    body = _facts(rows)
    if message:
        body += _quote(escape(message))
    if reply_to:
        body += _footnote("Reply to this email to answer the person who asked.")

    eyebrow = {"walkthrough": "Walkthrough request"}.get(kind, "Support request")
    html = _shell(
        preheader=subject, clinic=escape(clinic), eyebrow=eyebrow, heading=escape(subject),
        lead=f"{escape(requested_by)} at {escape(clinic)} {escape(line)}",
        action=(console_url, "Open in the console"), body=body,
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

#: stage -> the line under the amount on the stage
_RENT_CAPTION = {
    "issued": "Due on {due}",
    "due_soon": "Due on {due}",
    "due": "Due today",
    "overdue": "Was due on {due}",
    "final": "Was due on {due}; on hold from {hold}",
    "blocked": "Outstanding since {due}",
    "paid": "Paid in full for {period}",
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
    if notice_t:
        body += _notice(notice_t.format(**safe), tone="accent" if tone == "ok" else tone)
    body += _facts(rows, title="Bill details")
    if lines:
        body += _facts(lines, title="Charges")
    if stage != "paid":
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
        preheader=f"{heading} — {amount}", clinic=safe["clinic"], eyebrow=eyebrow,
        heading=escape(heading), figure=(amount, _RENT_CAPTION[stage].format(**safe)),
        action=(url, "View the bill") if stage != "paid" else None, body=body,
        recipient=to, reason=f"because you are a practice admin at {escape(clinic)}", tone=tone,
    )
    return Email(to=to, subject=subject, text=text, html=html)
