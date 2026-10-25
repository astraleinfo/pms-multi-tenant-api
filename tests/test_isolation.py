"""The tests that matter most: one clinic cannot reach another's data.

Each one attacks the boundary from a different angle — a valid token at the
wrong address, a real id from another clinic, a `tenant_id` planted in a
request body, a query parameter, and finally the database itself with the
application's own credentials.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from tests.conftest import CLEARVIEW, NORTHGATE, patients_of


async def _first_patient(signed) -> dict:
    return (await patients_of(signed))[0]


async def test_each_clinic_sees_only_its_own_patients(northgate, clearview):
    ours = {p["id"] for p in await patients_of(northgate)}
    theirs = {p["id"] for p in await patients_of(clearview)}

    assert ours and theirs
    assert ours.isdisjoint(theirs)

    tenants = {p["tenant_id"] for p in await patients_of(northgate)}
    assert tenants == {northgate.tenant_id}


async def test_a_token_is_refused_at_another_clinics_address(northgate):
    northgate.host = CLEARVIEW
    response = await northgate.get("/v1/patients")
    assert response.status_code == 401
    assert "address" in response.json()["error"]["message"]


async def test_another_clinics_patient_id_is_not_found_not_forbidden(northgate, clearview):
    """404, deliberately. A 403 would confirm the row exists."""
    theirs = await _first_patient(clearview)

    response = await northgate.get(f"/v1/patients/{theirs['id']}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_another_clinics_patient_cannot_be_edited(northgate, clearview):
    theirs = await _first_patient(clearview)
    response = await northgate.patch(
        f"/v1/patients/{theirs['id']}", json={"full_name": "Changed By Another Clinic"}
    )
    assert response.status_code == 404

    unchanged = await clearview.get(f"/v1/patients/{theirs['id']}")
    assert unchanged.json()["full_name"] == theirs["full_name"]


async def test_history_of_another_clinics_patient_is_not_found(northgate, clearview):
    theirs = await _first_patient(clearview)
    assert (await northgate.get(f"/v1/patients/{theirs['id']}/history")).status_code == 404


async def test_a_planted_tenant_id_in_the_body_is_rejected(northgate, clearview):
    """The field does not exist on the schema, so the request never runs."""
    response = await northgate.post(
        "/v1/patients",
        json={
            "full_name": "Planted Tenant",
            "dob": "1990-01-01",
            "sex": "other",
            "phone": "9770090000",
            "tenant_id": clearview.tenant_id,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_created_rows_are_stamped_with_the_callers_tenant(northgate):
    response = await northgate.post(
        "/v1/patients",
        json={"full_name": "Stamped Correctly", "dob": "1991-02-03", "sex": "female", "phone": "9000000001"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["tenant_id"] == northgate.tenant_id


async def test_another_clinics_encounter_and_invoice_are_invisible(northgate, clearview):
    theirs_encounters = (await clearview.get("/v1/encounters")).json()["items"]
    assert theirs_encounters
    response = await northgate.get(f"/v1/encounters/{theirs_encounters[0]['id']}")
    assert response.status_code == 404

    theirs_invoices = (await clearview.get("/v1/billing/invoices")).json()["items"]
    if theirs_invoices:
        response = await northgate.get(f"/v1/billing/invoices/{theirs_invoices[0]['id']}")
        assert response.status_code == 404


async def test_an_appointment_cannot_be_booked_for_another_clinics_patient(northgate, clearview):
    theirs = await _first_patient(clearview)
    doctors = (await northgate.get("/v1/users/doctors")).json()

    response = await northgate.post(
        "/v1/appointments",
        json={
            "patient_id": theirs["id"],
            "doctor_id": doctors[0]["id"],
            "date": "2026-09-01",
            "time": "09:00",
            "duration_min": 30,
            "reason": "Should never be created",
        },
    )
    assert response.status_code == 404


async def test_custom_fields_do_not_cross_clinics(northgate, clearview):
    ours = (await northgate.get("/v1/tenant/fields")).json()
    theirs = (await clearview.get("/v1/tenant/fields")).json()

    our_tenant_fields = {f["field_key"] for f in ours if f["scope"] == "tenant"}
    their_tenant_fields = {f["field_key"] for f in theirs if f["scope"] == "tenant"}

    assert our_tenant_fields and their_tenant_fields
    assert our_tenant_fields.isdisjoint(their_tenant_fields)
    assert all(f["tenant_id"] in (None, northgate.tenant_id) for f in ours)


async def test_staff_lists_do_not_cross_clinics(northgate, clearview):
    ours = {u["email"] for u in (await northgate.get("/v1/users")).json()}
    theirs = {u["email"] for u in (await clearview.get("/v1/users")).json()}
    assert ours.isdisjoint(theirs)


async def test_no_password_material_is_ever_returned(northgate):
    """No hash and no plaintext — in any field, under any name.

    `must_change_password` is a fact *about* a password rather than any part of
    one, so the check is for material: the argon2 marker a hash always carries,
    a `password_hash` field, and a bare `password` field. Matching the word
    anywhere in the body would fail on a boolean that reveals nothing.
    """
    rows = (await northgate.get("/v1/users")).json()
    assert rows
    for row in rows:
        assert "password" not in row
        assert "password_hash" not in row
        assert not any(isinstance(v, str) and "argon2" in v for v in row.values())

    assert "argon2" not in (await northgate.get("/v1/users")).text


@pytest.mark.parametrize("path", ["/v1/patients", "/v1/appointments", "/v1/queue", "/v1/encounters"])
async def test_every_collection_needs_a_session(client, path):
    response = await client.get(path, headers={"Host": NORTHGATE})
    assert response.status_code == 401


async def test_the_database_itself_refuses_to_cross_tenants():
    """The last line of defence, tested without the application in the way.

    Connecting as the application's own role and asking for every patient
    returns nothing at all unless a tenant is bound — and once one is, only
    that tenant's rows come back.
    """
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        async with session.begin():
            unbound = (await session.execute(text("select count(*) from patients"))).scalar()
            assert unbound == 0, "rows were visible with no tenant bound"

            tenants = (
                await session.execute(text("select id from tenants order by slug"))
            ).scalars().all()
            assert len(tenants) >= 2

            first, second = tenants[0], tenants[1]

            await session.execute(
                text("select set_config('app.tenant_id', :t, true)"), {"t": str(first)}
            )
            visible = (
                await session.execute(text("select distinct tenant_id from patients"))
            ).scalars().all()
            assert visible == [first]

            # A WHERE clause naming another tenant returns nothing: the policy
            # is applied on top of whatever the query asks for.
            leaked = (
                await session.execute(
                    text("select count(*) from patients where tenant_id = :t"), {"t": str(second)}
                )
            ).scalar()
            assert leaked == 0

    await engine.dispose()


async def test_the_application_role_cannot_disable_the_policies():
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        with pytest.raises(Exception) as exc:
            async with session.begin():
                await session.execute(text("alter table patients disable row level security"))
        assert "owner" in str(exc.value).lower() or "permission" in str(exc.value).lower()

    await engine.dispose()


async def test_the_application_role_does_not_bypass_rls():
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        bypasses = (
            await conn.execute(
                text("select rolbypassrls from pg_roles where rolname = current_user")
            )
        ).scalar()
    assert bypasses is False
    await engine.dispose()


async def test_one_appointment_is_read_by_id_and_only_in_its_own_clinic(northgate, clearview):
    page = (await northgate.get("/v1/appointments", params={"limit": 1})).json()
    assert page["items"], "the seed books appointments"
    booking = page["items"][0]

    mine = await northgate.get(f"/v1/appointments/{booking['id']}")
    assert mine.status_code == 200, mine.text
    assert mine.json()["id"] == booking["id"] and mine.json()["patient_name"] == booking["patient_name"]

    assert (await clearview.get(f"/v1/appointments/{booking['id']}")).status_code == 404
    assert (await northgate.get("/v1/appointments/not-a-uuid")).status_code in (404, 422)
    # the route above it still answers as itself, not as an id
    assert (await northgate.get("/v1/appointments/availability")).status_code == 422
