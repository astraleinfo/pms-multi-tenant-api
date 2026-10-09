"""The printable invoice and the practice report.

A PDF is checked for what matters about it: it is a PDF, it carries the
figures the API reports for the same row, and it is refused to anyone the JSON
would be refused to.
"""

from __future__ import annotations

import io

import pytest

from tests.conftest import sign_in


def _text(pdf: bytes) -> str:
    """The text a reader would see. reportlab compresses page streams, so the
    bytes are searched only after pypdf extracts them."""
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)


async def _an_invoice(signed) -> dict:
    page = (await signed.get("/v1/billing/invoices", params={"limit": 50})).json()
    return next(i for i in page["items"] if i["status"] != "void" and i["line_items"])


async def test_an_invoice_prints_with_the_figures_the_screen_shows(northgate):
    invoice = await _an_invoice(northgate)

    response = await northgate.get(f"/v1/billing/invoices/{invoice['id']}/pdf")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.content.startswith(b"%PDF")

    text = _text(response.content)
    assert invoice["number"] in text
    assert "INVOICE" in text
    assert f"{invoice['totals']['total']:,.2f}" in text
    assert f"{invoice['totals']['balance']:,.2f}" in text
    assert invoice["line_items"][0]["description"].split()[0] in text


async def test_an_invoice_from_another_clinic_is_not_found(northgate, clearview):
    invoice = await _an_invoice(northgate)
    assert (await clearview.get(f"/v1/billing/invoices/{invoice['id']}/pdf")).status_code == 404


async def test_an_invoice_pdf_needs_a_session(client, northgate):
    from tests.conftest import NORTHGATE

    invoice = await _an_invoice(northgate)
    response = await client.get(
        f"/v1/billing/invoices/{invoice['id']}/pdf", headers={"Host": NORTHGATE}
    )
    assert response.status_code == 401


async def test_a_malformed_invoice_id_is_a_404_not_a_crash(northgate):
    assert (await northgate.get("/v1/billing/invoices/not-a-uuid/pdf")).status_code == 404


@pytest.mark.parametrize("days", [7, 30, 365])
async def test_the_practice_report_prints_the_dashboard_figures(northgate, days):
    dashboard = (await northgate.get("/v1/analytics/dashboard", params={"days": days})).json()

    response = await northgate.get("/v1/analytics/report.pdf", params={"days": days})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"

    text = _text(response.content)
    assert "PRACTICE REPORT" in text
    assert f"{dashboard['core']['patients']:,}" in text
    assert f"{days} days" in text
    assert f"{dashboard['modules']['billing']['outstanding']:,.2f}" in text


async def test_the_report_prints_every_module_for_every_clinic(rowan):
    """Rowan only ever had the lab switched on; its report has every section."""
    text = _text((await rowan.get("/v1/analytics/report.pdf")).content)
    for heading in ("BILLING", "PHARMACY", "LAB\n"):
        assert heading in text, heading


async def test_the_report_is_refused_where_the_dashboard_is(client):
    signed = await sign_in(client, "northgate_reception")
    dashboard = await signed.get("/v1/analytics/dashboard")
    report = await signed.get("/v1/analytics/report.pdf")
    assert report.status_code == dashboard.status_code


async def test_the_report_window_is_bounded(northgate):
    assert (await northgate.get("/v1/analytics/report.pdf", params={"days": 0})).status_code == 422
    assert (await northgate.get("/v1/analytics/report.pdf", params={"days": 366})).status_code == 422
