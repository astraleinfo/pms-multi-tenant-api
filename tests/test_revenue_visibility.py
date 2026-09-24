"""The clinic's takings are the practice admin's to see.

Every role reads the analytics read model — the dashboard is everyone's first
screen — so the money is taken out on the server for anyone without
`revenue:read`, rather than sent for a screen to hide.
"""

from __future__ import annotations

import pytest

from tests.conftest import sign_in

MONEY_CORE = {"revenue"}
MONEY_BILLING = {"billed", "paid", "outstanding", "revenue_mix"}


async def _analytics(clinic) -> dict:
    response = await clinic.get("/v1/analytics/dashboard", params={"days": 30})
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_practice_admin_sees_the_takings(northgate):
    data = await _analytics(northgate)
    assert data["revenue_visible"] is True
    assert MONEY_CORE <= set(data["core"])
    if "billing" in data["modules"]:
        assert MONEY_BILLING <= set(data["modules"]["billing"])


@pytest.mark.parametrize("who", ["northgate_doctor", "northgate_reception"])
async def test_anyone_else_is_sent_the_counts_and_not_the_money(client, who):
    data = await _analytics(await sign_in(client, who))
    assert data["revenue_visible"] is False
    assert not MONEY_CORE & set(data["core"])
    assert "patients" in data["core"], "the rest of the read model is untouched"

    billing = data["modules"].get("billing")
    if billing is not None:
        assert not MONEY_BILLING & set(billing)
        assert {"count", "unpaid", "drafts"} <= set(billing), "the counter's work list stays"
    pharmacy = data["modules"].get("pharmacy")
    if pharmacy is not None:
        assert "dispensed_value" not in pharmacy
    for clinician in data["modules"].get("advanced_reports", {}).get("clinicians", []):
        assert "revenue" not in clinician


async def test_the_report_pdf_prints_without_the_money_for_a_doctor(northgate_doctor):
    response = await northgate_doctor.get("/v1/analytics/report.pdf", params={"days": 30})
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"%PDF")
