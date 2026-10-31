"""Whose message it is decides whose header it wears.

A clinic's mail to its patients (the invoice) has the clinic's own logo and name
in its header, and nothing of the platform's there — no Astrale logo, no product
tagline. What the platform sends (invitations, resets, support requests, rent
notices) keeps the Astrale logo and tagline. The footer names the platform on
every message.

The header is cut out of the HTML between its two comment markers, so "the name
appears once in the header" means exactly that and not "somewhere in the card".
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.core.brand import FULL_NAME, PLATFORM_NAME, PRODUCT_TAGLINE
from app.services import email_logo, mailer, platform_logo, storage
from tests.test_invoice_email import _invoice, _patient, _send
from tests.test_platform_logo import _clear_logo
from tests.test_templates import _emails

CLINIC = "Northgate Dental Studio"
URL = "/v1/tenant/config/logo"
PLATFORM_URL = "/v1/platform/settings/logo"


def _png(width: int, height: int, colour, *, mode="RGBA") -> bytes:
    out = io.BytesIO()
    Image.new(mode, (width, height), colour).save(out, format="PNG")
    return out.getvalue()


def _jpeg(width: int, height: int, colour=(255, 255, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="JPEG")
    return out.getvalue()


#: dark ink on transparency: invisible on the dark header without a tile
DARK_SQUARE = _png(120, 120, (20, 24, 40, 255))
DARK_ON_TRANSPARENT = _png(120, 120, (0, 0, 0, 0))
#: white artwork on transparency, made for a dark ground
WIDE = _png(400, 80, (30, 90, 200, 255))


def _transparent_with(colour, size=(120, 120)) -> bytes:
    """Artwork in `colour` on a transparent field — a block in the middle."""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    w, h = size
    image.paste(Image.new("RGBA", (w // 2, h // 2), colour), (w // 4, h // 4))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


DARK_ART = _transparent_with((10, 20, 40, 255))
LIGHT_ART = _transparent_with((250, 250, 250, 255))
LIGHT_ON_TRANSPARENT = _transparent_with((255, 255, 255, 255), size=(300, 60))


def _header(html: str) -> str:
    start = html.index("<!-- the sender's lockup")
    return html[start : html.index("<!-- what kind of message", start)]


def _invoice_email(branding: mailer.ClinicBranding) -> mailer.Email:
    return mailer.invoice_to_patient(
        to="patient@example.com", clinic=branding.name, from_name=f"{branding.name} via Astrale PMS",
        reply_to="desk@northgate.clinic", subject="Invoice NG-INV-1", message="Dear Samuel,\n\nThank you.",
        number="NG-INV-1", issued="1 Oct 2026", due="8 Oct 2026", total="₹100.00", balance="₹100.00",
        owing=True, pdf=b"%PDF-1.4", filename="NG-INV-1.pdf", branding=branding,
    )


def _images(message) -> list:
    return [p for p in message.walk() if p.get_content_maintype() == "image"]


# --- a clinic's own message: the clinic's header ---------------------------------------


def test_the_invoice_header_is_the_clinics_logo_and_name_with_nothing_of_the_platforms():
    mark = email_logo.prepare_clinic_mark(DARK_SQUARE)
    email = _invoice_email(mailer.ClinicBranding(CLINIC, mark))
    header = _header(email.html)

    assert f'src="cid:{mailer.CLINIC_MARK_CID}"' in header
    assert f"cid:{mailer.MARK_CID}" not in email.html
    assert PLATFORM_NAME not in header
    assert f">{PRODUCT_TAGLINE}<" not in email.html
    assert header.count(CLINIC) == 1, "the clinic's name is printed once, in the lockup"
    assert 'align="right"' not in header, "no second, right-aligned copy of the name"

    # the one inline image is the clinic's, beside the PDF
    message = email.as_message()
    assert message.get_content_type() == "multipart/mixed"
    (image,) = _images(message)
    assert image["Content-ID"] == f"<{mailer.CLINIC_MARK_CID}>"
    assert image.get_content_disposition() == "inline"
    assert image.get_content() == mark.data
    related = message.get_payload()[0].get_payload()[1]
    assert related.get_content_type() == "multipart/related"
    assert [p.get_content_type() for p in related.get_payload()] == ["text/html", "image/png"]
    assert message.get_payload()[1].get_filename() == "NG-INV-1.pdf"


def test_the_footer_of_a_clinics_message_still_names_the_platform():
    for mark in (email_logo.prepare_clinic_mark(DARK_SQUARE), None):
        html = _invoice_email(mailer.ClinicBranding(CLINIC, mark)).html
        assert f"{FULL_NAME}, for independent clinics." in html
        assert html.count(PLATFORM_NAME) == 1, "the platform is named once: in the footer"
        assert html.index(PLATFORM_NAME) > html.index("<!-- footer -->")


def test_a_clinic_without_a_logo_gets_a_text_header_and_no_image_part():
    email = _invoice_email(mailer.ClinicBranding(CLINIC, None))
    header = _header(email.html)

    assert "<img" not in email.html and "cid:" not in email.html
    assert "background-color:#FFFFFF" not in header, "no placeholder tile"
    assert header.count(CLINIC) == 1
    assert PLATFORM_NAME not in header and PRODUCT_TAGLINE not in header
    message = email.as_message()
    assert _images(message) == []
    assert not any(p.get_content_type() == "multipart/related" for p in message.walk())
    assert [p.get_content_type() for p in message.get_payload()[0].get_payload()] == ["text/plain", "text/html"]


def test_the_plain_text_alternative_of_a_clinic_message_is_unaffected_by_the_logo():
    with_logo = _invoice_email(mailer.ClinicBranding(CLINIC, email_logo.prepare_clinic_mark(WIDE)))
    without = _invoice_email(mailer.ClinicBranding(CLINIC, None))
    assert with_logo.text == without.text
    assert "NG-INV-1" in with_logo.text and "cid:" not in with_logo.text


def test_a_clinic_name_is_escaped_in_the_header():
    html = _invoice_email(mailer.ClinicBranding("Smith & <Sons> Dental", None)).html
    assert "Smith &amp; &lt;Sons&gt; Dental" in _header(html)
    assert "<Sons>" not in html


@pytest.mark.parametrize(
    ("logo", "tile"),
    [
        (DARK_SQUARE, True),          # opaque dark artwork
        (DARK_ON_TRANSPARENT, True),  # nothing visible at all: safest is a tile
        (DARK_ART, True),             # dark ink on transparency
        (LIGHT_ART, False),           # white artwork on transparency: made for dark
        (LIGHT_ON_TRANSPARENT, False),
        (_jpeg(100, 100), True),      # opaque white-backed logo: a tile, not a white box on slate
    ],
    ids=["opaque-dark", "empty", "dark-on-transparent", "light-on-transparent", "light-wide", "jpeg"],
)
def test_artwork_that_would_vanish_on_the_dark_header_sits_on_a_light_tile(logo, tile):
    mark = email_logo.prepare_clinic_mark(logo)
    assert mark is not None and mark.tile is tile
    header = _header(_invoice_email(mailer.ClinicBranding(CLINIC, mark)).html)
    assert ("border-radius:9px;padding:4px" in header) is tile


def test_a_wide_logo_takes_its_own_line_and_a_square_one_sits_beside_the_name():
    wide = email_logo.prepare_clinic_mark(WIDE)
    square = email_logo.prepare_clinic_mark(DARK_SQUARE)
    assert wide.wide and not square.wide
    assert (wide.width, wide.height) == (132, 26) and (square.width, square.height) == (34, 34)
    # drawn at twice its size, so a high-density screen is sharp
    assert Image.open(io.BytesIO(wide.data)).size == (264, 52)
    assert "margin-top:10px" in _header(_invoice_email(mailer.ClinicBranding(CLINIC, wide)).html)
    assert "margin-top:10px" not in _header(_invoice_email(mailer.ClinicBranding(CLINIC, square)).html)


def test_a_logo_that_cannot_be_drawn_is_no_logo():
    assert email_logo.clinic_mark(None) is None
    assert email_logo.clinic_mark(b"") is None
    assert email_logo.prepare_clinic_mark(b"\x89PNG\r\n\x1a\n broken") is None
    assert email_logo.prepare_clinic_mark(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None
    assert email_logo.prepare_clinic_mark(_png(4001, 10, (0, 0, 0, 255))) is None, "over the dimension cap"
    assert email_logo.prepare_clinic_mark(_png(10, 4001, (0, 0, 0, 255))) is None


def test_a_very_large_logo_is_shrunk_to_a_few_kilobytes():
    big = _png(3000, 3000, (30, 90, 200, 255))
    mark = email_logo.prepare_clinic_mark(big)
    assert mark is not None
    assert len(mark.data) < 10_000
    assert Image.open(io.BytesIO(mark.data)).size == (68, 68)


# --- the platform's messages: the platform's header, unchanged --------------------------


@pytest.mark.parametrize("email", _emails(), ids=lambda e: e.subject[:40])
def test_every_platform_message_keeps_the_astrale_wordmark_and_tagline_and_no_clinic_image(email):
    # `_emails()` builds without a logo: its header is the wordmark and the tagline
    header = _header(email.html)
    assert ">Astrale</div>" in header and f">{PRODUCT_TAGLINE}</div>" in header
    assert mailer.CLINIC_MARK_CID not in email.html
    assert _images(email.as_message()) == []


def test_a_platform_message_with_the_logo_carries_only_the_astrale_image():
    mark = platform_logo.prepare_email_mark(platform_logo.default_logo())
    email = mailer.invitation(
        to="p@clinic.example", name="Priya", clinic=CLINIC,
        url="https://northgate.example/accept?token=a", expires_in_days=7, mark=mark,
    )
    header = _header(email.html)
    assert f'src="cid:{mailer.MARK_CID}"' in header
    assert f">{PRODUCT_TAGLINE}</div>" in header
    assert mailer.CLINIC_MARK_CID not in email.html
    (image,) = _images(email.as_message())
    assert image["Content-ID"] == f"<{mailer.MARK_CID}>"
    # the clinic it is about keeps its place at the right of the header
    assert 'align="right"' in header and CLINIC in header
    assert f"{FULL_NAME}, for independent clinics." in email.html


# --- through the route ------------------------------------------------------------------


@pytest.fixture
def outbox(monkeypatch) -> list[mailer.Email]:
    sent: list[mailer.Email] = []

    async def capture(email: mailer.Email) -> None:
        sent.append(email)

    monkeypatch.setattr(mailer, "send", capture)
    return sent


@pytest.fixture
async def clinic_logo(northgate):
    """Sets the clinic's logo and takes it off again: a clinic's logo is a
    singleton in a database shared across the suite."""

    async def put(data: bytes, content_type: str = "image/png"):
        response = await northgate.post(URL, files={"file": ("logo.png", data, content_type)})
        assert response.status_code == 200, response.text

    yield put
    await northgate.delete(URL)
    await storage.drain()
    email_logo.prepare_clinic_mark.cache_clear()


async def _sent(northgate, outbox) -> mailer.Email:
    patient = await _patient(northgate, email="branding@example.com")
    invoice = await _invoice(northgate, patient)
    response = await _send(northgate, invoice)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "sent"
    return outbox[-1]


async def test_a_sent_invoice_carries_the_clinics_uploaded_logo_and_no_astrale_one(
    northgate, outbox, clinic_logo
):
    await clinic_logo(WIDE)

    email = await _sent(northgate, outbox)

    clinic = (await northgate.get("/v1/tenant")).json()["name"]
    header = _header(email.html)
    assert email.mark is not None and email.mark.cid == mailer.CLINIC_MARK_CID
    assert f'src="cid:{mailer.CLINIC_MARK_CID}"' in header
    assert header.count(clinic) == 1
    assert PLATFORM_NAME not in header and f">{PRODUCT_TAGLINE}<" not in email.html
    message = email.as_message()
    (image,) = _images(message)
    assert image["Content-ID"] == f"<{mailer.CLINIC_MARK_CID}>"
    assert [p for p in message.iter_attachments() if p.get_content_type() == "application/pdf"]


async def test_a_platform_logo_does_not_leak_onto_a_clinics_invoice(northgate, console, outbox):
    uploaded = await console.post(PLATFORM_URL, files={"file": ("m.png", DARK_SQUARE, "image/png")})
    assert uploaded.status_code == 200, uploaded.text
    try:
        email = await _sent(northgate, outbox)
    finally:
        await _clear_logo()
        platform_logo.prepare_email_mark.cache_clear()

    assert mailer.MARK_CID not in email.html
    assert email.mark is None, "this clinic has no logo, and the platform's is not its"
    assert _images(email.as_message()) == []


async def test_a_clinic_without_a_logo_is_sent_a_text_header(northgate, outbox):
    assert (await northgate.get("/v1/tenant/config")).json()["has_logo"] is False

    email = await _sent(northgate, outbox)

    clinic = (await northgate.get("/v1/tenant")).json()["name"]
    assert email.mark is None
    assert "<img" not in email.html
    assert _header(email.html).count(clinic) == 1
    assert _images(email.as_message()) == []


@pytest.mark.parametrize(
    "bad",
    [b"\x89PNG\r\n\x1a\n not really an image", _png(4001, 10, (0, 0, 0, 255))],
    ids=["unreadable", "over-the-dimension-cap"],
)
async def test_a_logo_that_cannot_be_drawn_falls_back_to_a_text_header_and_the_send_goes(
    northgate, outbox, clinic_logo, bad
):
    await clinic_logo(bad)

    email = await _sent(northgate, outbox)

    assert email.mark is None
    assert "<img" not in email.html and "cid:" not in email.html
    assert _images(email.as_message()) == []

