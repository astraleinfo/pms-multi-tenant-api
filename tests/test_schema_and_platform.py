"""Field resolution (§3.3), the console (§3.1), and the checks that keep the
isolation machinery honest as the model grows."""

from __future__ import annotations

import pytest

from tests.conftest import CONSOLE, NORTHGATE

# --- §3.3 field resolution ---------------------------------------------------


async def test_the_effective_schema_merges_common_then_clinical_then_tenant(northgate, clearview):
    dental = (await northgate.get("/v1/tenant/schema/patient")).json()
    eyes = (await clearview.get("/v1/tenant/schema/patient")).json()

    dental_keys = [f["field_key"] for f in dental]
    eye_keys = [f["field_key"] for f in eyes]

    # common — both disciplines
    assert "full_name" in dental_keys and "full_name" in eye_keys
    # clinical type — one each
    assert "bruxism" in dental_keys and "bruxism" not in eye_keys
    assert "contact_lens" in eye_keys and "contact_lens" not in dental_keys
    # tenant custom — only the clinic that added it
    assert "insurance_number" in dental_keys and "insurance_number" not in eye_keys

    assert dental_keys == sorted(dental_keys, key=lambda k: [f["order"] for f in dental if f["field_key"] == k][0])


async def test_a_clinic_adds_and_removes_its_own_field(northgate):
    created = await northgate.post(
        "/v1/tenant/fields",
        json={
            "entity": "patient",
            "field_key": "loyalty_scheme",
            "label": "Loyalty scheme",
            "type": "select",
            "options": ["None", "Silver", "Gold"],
            "required": False,
            "order": 300,
        },
    )
    assert created.status_code == 201
    field = created.json()
    assert field["scope"] == "tenant"
    assert field["tenant_id"] == northgate.tenant_id

    schema = (await northgate.get("/v1/tenant/schema/patient")).json()
    assert "loyalty_scheme" in [f["field_key"] for f in schema]

    patient = (
        await northgate.post(
            "/v1/patients",
            json={
                "full_name": "Custom Field Patient",
                "dob": "1979-09-09",
                "sex": "male",
                "phone": "+44 7700 900777",
                "clinical_ext": {"loyalty_scheme": "Gold"},
            },
        )
    ).json()
    assert patient["clinical_ext"]["loyalty_scheme"] == "Gold"

    bad = await northgate.post(
        "/v1/patients",
        json={
            "full_name": "Bad Option",
            "dob": "1979-09-09",
            "sex": "male",
            "phone": "1",
            "clinical_ext": {"loyalty_scheme": "Platinum"},
        },
    )
    assert bad.status_code == 422

    removed = await northgate.delete(f"/v1/tenant/fields/{field['id']}")
    assert removed.status_code == 204


async def test_a_clinic_cannot_delete_a_platform_field(northgate):
    schema = (await northgate.get("/v1/tenant/fields")).json()
    common = next(f for f in schema if f["scope"] == "common")
    response = await northgate.delete(f"/v1/tenant/fields/{common['id']}")
    assert response.status_code == 404


async def test_a_required_custom_field_is_enforced(northgate):
    created = (
        await northgate.post(
            "/v1/tenant/fields",
            json={
                "entity": "patient",
                "field_key": "consent_reference",
                "label": "Consent reference",
                "type": "text",
                "required": True,
                "order": 310,
            },
        )
    ).json()

    missing = await northgate.post(
        "/v1/patients",
        json={"full_name": "No Consent", "dob": "1980-01-01", "sex": "other", "phone": "1"},
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["details"]["fields"][0]["field"] == "consent_reference"

    await northgate.delete(f"/v1/tenant/fields/{created['id']}")


async def test_configuration_is_per_clinic_and_validated(northgate, clearview):
    ours = (await northgate.get("/v1/tenant/config")).json()
    theirs = (await clearview.get("/v1/tenant/config")).json()
    assert ours["mrn_prefix"] != theirs["mrn_prefix"]
    assert ours["tenant_id"] == northgate.tenant_id

    updated = await northgate.patch("/v1/tenant/config", json={"slot_minutes": 20, "tax_pct": 5})
    assert updated.status_code == 200
    assert updated.json()["slot_minutes"] == 20

    rejected = await northgate.patch("/v1/tenant/config", json={"slot_minutes": 45})
    assert rejected.status_code == 422

    await northgate.patch("/v1/tenant/config", json={"slot_minutes": ours["slot_minutes"], "tax_pct": ours["tax_pct"]})


# --- §3.1 the console --------------------------------------------------------


async def test_the_console_creates_a_clinic_that_is_immediately_usable(console, client):
    created = await console.post(
        "/v1/platform/tenants",
        json={
            "name": "Harbour Eye Clinic",
            "slug": "harbour",
            "city": "Bristol",
            "clinical_type": "ophthalmology",
            "admin_name": "Dana Okafor",
            "admin_email": "dana@harbour.eye",
            "admin_password": "harbour-admin-2026",
        },
    )
    assert created.status_code == 201, created.text
    tenant = created.json()["tenant"]
    assert tenant["status"] == "trial"

    login = await client.post(
        "/v1/auth/login",
        headers={"Host": "harbour.localhost"},
        json={"email": "dana@harbour.eye", "password": "harbour-admin-2026"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Host": "harbour.localhost", "Authorization": f"Bearer {token}"}

    features = (await client.get("/v1/tenant/features", headers=headers)).json()
    active = {f["feature_key"] for f in features if f["status"] == "active"}
    assert active == {"patient", "appointment", "queue", "consultation", "prescription"}

    schema = (await client.get("/v1/tenant/schema/patient", headers=headers)).json()
    keys = {f["field_key"] for f in schema}
    assert "contact_lens" in keys, "the clinical-type field set follows the discipline"
    assert "insurance_number" not in keys, "and nobody else's custom fields come with it"

    page = (await client.get("/v1/patients", headers=headers)).json()
    assert page["items"] == [] and page["total"] == 0, "a new clinic starts empty"

    config = (await client.get("/v1/tenant/config", headers=headers)).json()
    assert config["mrn_prefix"] == "HA"


async def test_a_subdomain_cannot_be_claimed_twice_or_reserved(console):
    taken = await console.post(
        "/v1/platform/tenants",
        json={
            "name": "Copycat Dental",
            "slug": "northgate",
            "city": "Leeds",
            "clinical_type": "dental",
            "admin_name": "Copy Cat",
            "admin_email": "copy@cat.clinic",
            "admin_password": "copycat-admin-2026",
        },
    )
    assert taken.status_code == 409

    reserved = await console.post(
        "/v1/platform/tenants",
        json={
            "name": "Reserved Name",
            "slug": "admin",
            "city": "Leeds",
            "clinical_type": "dental",
            "admin_name": "Copy Cat",
            "admin_email": "copy2@cat.clinic",
            "admin_password": "copycat-admin-2026",
        },
    )
    assert reserved.status_code == 409


async def test_a_clinic_admin_cannot_reach_the_console(northgate, client):
    for path in ("/v1/platform/tenants", "/v1/platform/snapshot"):
        response = await client.get(
            path, headers={"Host": CONSOLE, "Authorization": f"Bearer {northgate.token}"}
        )
        assert response.status_code == 401, path

    response = await northgate.get("/v1/platform/tenants")
    assert response.status_code == 401


async def test_the_snapshot_reports_per_clinic_counts(console):
    snapshot = (await console.get("/v1/platform/snapshot")).json()
    by_slug = {row["tenant"]["slug"]: row for row in snapshot["tenants"]}

    assert {"northgate", "clearview", "rowan"} <= set(by_slug)
    assert by_slug["northgate"]["patients"] > 0
    assert by_slug["northgate"]["patients"] != by_slug["clearview"]["patients"]
    assert "billing" in by_slug["northgate"]["active_features"]
    assert "billing" not in by_slug["rowan"]["active_features"]


# --- the machinery itself ----------------------------------------------------


def test_every_tenant_owned_table_has_a_policy():
    """A new model that carries `tenant_id` must arrive with a policy for it.

    This fails the moment someone adds a tenant table and forgets the migration.
    """
    import ast
    from pathlib import Path

    from app.db.base import tenant_scoped_tables

    # A table can be secured by whichever migration introduced it, so the whole
    # history counts — not just the one that first turned RLS on. Read with ast
    # rather than importing, so this never depends on a migration's imports.
    versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    secured: set[str] = set()
    for path in versions.glob("*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, ast.Assign):
                continue
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if "OWNED_TABLES" in names:
                secured.update(ast.literal_eval(node.value))

    missing = sorted(set(tenant_scoped_tables()) - secured)
    assert not missing, f"tenant-owned tables with no RLS policy in any migration: {missing}"


async def test_the_orm_guard_refuses_an_unbound_query():
    """Below the HTTP layer: asking for tenant rows with nothing bound raises
    rather than quietly returning the empty set."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import settings
    from app.core.errors import TenantIsolationViolation
    from app.models.clinical import Patient

    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        with pytest.raises(TenantIsolationViolation):
            await session.execute(select(Patient))
    await engine.dispose()


async def test_the_orm_guard_refuses_a_write_for_another_tenant(northgate, clearview):
    import uuid
    from datetime import date

    from app.core.errors import TenantIsolationViolation
    from app.db.session import session_factory, set_tenant
    from app.models.clinical import Patient

    async with session_factory() as session:
        await session.begin()
        await set_tenant(session, uuid.UUID(northgate.tenant_id))
        session.add(
            Patient(
                tenant_id=uuid.UUID(clearview.tenant_id),
                mrn="XX-1",
                full_name="Planted Row",
                dob=date(1990, 1, 1),
                sex="other",
                registered_at=date(2026, 1, 1),
            )
        )
        with pytest.raises(TenantIsolationViolation):
            await session.flush()
        await session.rollback()


async def test_the_openapi_document_is_valid(client):
    response = await client.get("/openapi.json", headers={"Host": NORTHGATE})
    assert response.status_code == 200
    document = response.json()
    assert document["info"]["title"] == "Astrale PMS API"
    assert "/v1/patients" in document["paths"]
