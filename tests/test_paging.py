"""The list endpoints answer with a page, not a collection.

Five screens used to load every row a clinic owned in order to show a dozen of
them. Each of these endpoints now searches, sorts and slices in the database,
and says how many rows matched — with `limit=0` still returning the lot, for
the views that are genuinely about a whole day or a whole patient.
"""

from __future__ import annotations

import pytest

PAGED = [
    "/v1/appointments",
    "/v1/encounters",
    "/v1/prescriptions",
    "/v1/lab/orders",
    "/v1/billing/invoices",
]


@pytest.mark.parametrize("path", PAGED)
async def test_a_page_is_a_page_and_knows_the_total(northgate, path):
    body = (await northgate.get(path, params={"limit": 3})).json()
    assert len(body["items"]) <= 3
    assert body["limit"] == 3 and body["offset"] == 0
    assert body["total"] >= len(body["items"])


@pytest.mark.parametrize("path", PAGED)
async def test_the_whole_collection_is_still_one_request_away(northgate, path):
    """`limit=0` is what the day grid and the badge counts ask for."""
    everything = (await northgate.get(path)).json()
    assert everything["limit"] == 0
    assert len(everything["items"]) == everything["total"]


@pytest.mark.parametrize("path", PAGED)
async def test_pages_do_not_overlap_or_lose_rows(northgate, path):
    first = (await northgate.get(path, params={"limit": 5, "offset": 0})).json()
    second = (await northgate.get(path, params={"limit": 5, "offset": 5})).json()
    assert first["total"] == second["total"]

    ids = [row["id"] for row in first["items"]] + [row["id"] for row in second["items"]]
    assert len(ids) == len(set(ids)), "a row appeared on two pages"

    everything = (await northgate.get(path)).json()["items"]
    assert ids == [row["id"] for row in everything[: len(ids)]]


async def test_a_total_counts_matches_not_rows_on_the_page(northgate):
    everything = (await northgate.get("/v1/encounters")).json()
    page = (await northgate.get("/v1/encounters", params={"limit": 2})).json()
    assert page["total"] == everything["total"] > 2
    assert len(page["items"]) == 2


async def test_search_runs_in_the_database(northgate):
    everything = (await northgate.get("/v1/encounters")).json()["items"]
    name = everything[0]["patient_name"]

    hit = (await northgate.get("/v1/encounters", params={"search": name, "limit": 10})).json()
    assert hit["total"] >= 1
    assert all(row["patient_name"] == name for row in hit["items"])

    miss = (await northgate.get("/v1/encounters", params={"search": "zzz no such person"})).json()
    assert miss["total"] == 0 and miss["items"] == []


async def test_a_search_cannot_reach_across_clinics(northgate, clearview):
    theirs = (await clearview.get("/v1/encounters", params={"limit": 1})).json()["items"][0]
    hunted = (await northgate.get("/v1/encounters", params={"search": theirs["patient_name"]})).json()
    assert hunted["total"] == 0


async def test_sorting_is_a_query_not_a_client_side_shuffle(northgate):
    oldest = (await northgate.get(
        "/v1/encounters", params={"sort": "date", "order": "asc", "limit": 1}
    )).json()["items"][0]
    newest = (await northgate.get(
        "/v1/encounters", params={"sort": "date", "order": "desc", "limit": 1}
    )).json()["items"][0]
    assert oldest["date"] <= newest["date"]


async def test_an_unknown_sort_key_falls_back_rather_than_reaching_the_database(northgate):
    """An ORDER BY is a query plan, not a free-text field."""
    response = await northgate.get(
        "/v1/encounters", params={"sort": "date; drop table encounters", "limit": 5}
    )
    assert response.status_code == 200
    default = (await northgate.get("/v1/encounters", params={"limit": 5})).json()
    assert [r["id"] for r in response.json()["items"]] == [r["id"] for r in default["items"]]


async def test_the_ledger_can_be_ordered_by_money_the_database_adds_up(northgate):
    page = (await northgate.get(
        "/v1/billing/invoices", params={"sort": "total", "order": "desc", "limit": 10}
    )).json()
    totals = [row["totals"]["total"] for row in page["items"]]
    assert totals == sorted(totals, reverse=True)


async def test_tab_counts_describe_the_ledger_not_the_page(northgate):
    everything = (await northgate.get("/v1/billing/invoices")).json()
    page = (await northgate.get("/v1/billing/invoices", params={"status": "paid", "limit": 2})).json()

    assert page["counts"] == everything["counts"], "the counts ignore the tab that is open"
    assert page["total"] == page["counts"].get("paid", 0)
    assert all(row["status"] == "paid" for row in page["items"])


async def test_unpaid_is_a_question_about_money(northgate):
    page = (await northgate.get("/v1/billing/invoices", params={"unpaid": True})).json()
    assert page["total"] == page["counts"]["unpaid"]
    assert all(row["status"] in ("issued", "partially_paid") for row in page["items"])


async def test_prescription_counts_are_taken_before_the_status_filter(northgate):
    everything = (await northgate.get("/v1/prescriptions")).json()
    dispensed = (await northgate.get(
        "/v1/prescriptions", params={"status": "dispensed", "limit": 5}
    )).json()

    assert dispensed["counts"] == everything["counts"]
    assert dispensed["total"] == dispensed["counts"].get("dispensed", 0)


async def test_the_bench_view_is_everything_not_yet_released(northgate):
    page = (await northgate.get("/v1/lab/orders", params={"unverified": True})).json()
    assert page["total"] > 0
    assert all(row["status"] != "verified" for row in page["items"])


async def test_a_page_is_still_only_this_clinics_rows(northgate, clearview):
    ours = (await northgate.get("/v1/encounters", params={"limit": 200})).json()["items"]
    theirs = {row["id"] for row in (await clearview.get("/v1/encounters")).json()["items"]}
    assert {row["id"] for row in ours}.isdisjoint(theirs)
