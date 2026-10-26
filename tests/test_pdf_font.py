"""The embedded PDF font: the characters the layouts need, and what happens to
the ones it cannot draw.

Helvetica, the built-in face these documents used to print in, has no rupee
sign and no letters outside Latin-1, so a clinic's amounts printed as
"INR 1,250.00" and a patient named in another script printed as blank boxes.
"""

from __future__ import annotations

import io

from tests.conftest import patients_of


def _text(pdf: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)


async def _an_invoice(signed) -> dict:
    page = (await signed.get("/v1/billing/invoices", params={"limit": 50})).json()
    return next(i for i in page["items"] if i["status"] != "void" and i["line_items"])


def test_the_font_covers_every_character_the_layouts_use():
    from app.services.pdf_fonts import has_glyph

    for ch in "₹$€£·–—…éÅü":
        assert has_glyph(ch), ch


def test_glyph_safe_replaces_what_the_font_cannot_draw():
    from app.services.pdf_fonts import glyph_safe

    out = glyph_safe("अनिता Verma")
    assert "Verma" in out
    assert not any("ऀ" <= ch <= "ॿ" for ch in out)
    assert glyph_safe("") == ""
    assert glyph_safe("plain ASCII, ₹ and é") == "plain ASCII, ₹ and é"


async def test_an_invoice_prints_the_clinics_currency_symbol(northgate):
    from app.services.pdf_fonts import CURRENCY_SYMBOLS

    config = (await northgate.get("/v1/tenant/config")).json()
    invoice = await _an_invoice(northgate)

    text = _text((await northgate.get(f"/v1/billing/invoices/{invoice['id']}/pdf")).content)

    assert CURRENCY_SYMBOLS[config["currency"]] in text
    assert f"{config['currency']} " not in text


async def test_a_patient_name_in_an_unsupported_script_does_not_break_the_record_pdf(northgate):
    patient = (
        await northgate.post(
            "/v1/patients",
            json={"full_name": "अनिता Verma", "dob": "1988-04-11", "sex": "female", "phone": "9770090055"},
        )
    ).json()
    assert patient["id"], patient

    response = await northgate.get(f"/v1/patients/{patient['id']}/record.pdf")

    assert response.status_code == 200, response.text
    assert "Verma" in _text(response.content)
    assert (await patients_of(northgate))  # the clinic still has its patients
