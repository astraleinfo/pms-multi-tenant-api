"""Field resolution (§3.3), the console (§3.1), and the checks that keep the
isolation machinery honest as the model grows."""

from __future__ import annotations

import pytest

from tests.conftest import CLEARVIEW, CONSOLE, NORTHGATE, audit_lines, token_from

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


async def test_appointment_types_are_the_clinics_own_and_validated(northgate, clearview):
    ours = (await northgate.get("/v1/tenant/config")).json()
    assert [t["id"] for t in ours["appointment_types"]]  # every clinic starts with some

    types = [
        {"id": "consultation", "name": "Consultation", "minutes": 30},
        {"id": "root-canal", "name": "Root canal", "minutes": 90},
    ]
    saved = await northgate.patch("/v1/tenant/config", json={"appointment_types": types})
    assert saved.status_code == 200
    assert saved.json()["appointment_types"] == types

    # another clinic's list is untouched
    theirs = (await clearview.get("/v1/tenant/config")).json()
    assert theirs["appointment_types"] != types

    for bad in (
        [{"id": "x", "name": "Odd length", "minutes": 17}],            # not a 5-minute step
        [{"id": "x", "name": "Too long", "minutes": 300}],
        [{"id": "a", "name": "Review", "minutes": 15}, {"id": "b", "name": "review", "minutes": 20}],
        [{"id": "x", "name": "   ", "minutes": 15}],
    ):
        assert (await northgate.patch("/v1/tenant/config", json={"appointment_types": bad})).status_code == 422

    await northgate.patch("/v1/tenant/config", json={"appointment_types": ours["appointment_types"]})


# --- §3.1 the console --------------------------------------------------------


async def test_the_console_creates_a_clinic_that_is_immediately_usable(console, client):
    """Creating a clinic, and the invitation that is the only way into it.

    No password is sent, chosen, or returned anywhere in this flow. The console
    gets a link (only because the test transport is `console`); the person named
    on the account is the one who picks what it will be.
    """
    created = await console.post(
        "/v1/platform/tenants",
        json={
            "name": "Harbour Eye Clinic",
            "slug": "harbour",
            "city": "Bristol",
            "clinical_type": "ophthalmology",
            "admin_name": "Dana Okafor",
            "admin_email": "dana@harbour.eye",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    tenant = body["tenant"]
    assert tenant["status"] == "trial"
    assert body["invitation_sent"] is True

    # Nothing can sign in yet: the account has no password at all.
    too_early = await client.post(
        "/v1/auth/login",
        headers={"Host": "harbour.localhost"},
        json={"email": "dana@harbour.eye", "password": "guessing-at-it-2026"},
    )
    assert too_early.status_code == 401

    invite_token = token_from(body["invitation_url"])

    # The page behind the link knows who it is for, before anyone is signed in.
    preview = await client.get(
        "/v1/auth/invitation",
        headers={"Host": "harbour.localhost"},
        params={"token": invite_token},
    )
    assert preview.status_code == 200
    assert preview.json() == {
        "valid": True, "purpose": "invite", "name": "Dana Okafor",
        "email": "dana@harbour.eye", "clinic": "Harbour Eye Clinic", "problem": None,
    }

    accepted = await client.post(
        "/v1/auth/password/reset",
        headers={"Host": "harbour.localhost"},
        json={"token": invite_token, "password": "harbour-admin-2026"},
    )
    assert accepted.status_code == 200, accepted.text

    # The link is spent. Somebody reading the same email twice gets nothing.
    again = await client.post(
        "/v1/auth/password/reset",
        headers={"Host": "harbour.localhost"},
        json={"token": invite_token, "password": "another-password-2026"},
    )
    assert again.status_code == 422

    login = await client.post(
        "/v1/auth/login",
        headers={"Host": "harbour.localhost"},
        json={"email": "dana@harbour.eye", "password": "harbour-admin-2026"},
    )
    assert login.status_code == 200
    # They chose it themselves, so nothing is forcing a change.
    assert login.json()["must_change_password"] is False
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


async def test_the_subdomain_check_answers_before_the_form_is_submitted(console):
    """§7 — the create form asks while somebody types.

    The same rule the create path enforces, so the answer here is the answer
    they will get on submit: free, taken, reserved, or malformed.
    """
    free = (await console.get("/v1/platform/tenants/slug-check", params={"slug": "harbourside"})).json()
    assert free["available"] is True and free["problem"] is None
    assert free["url"].endswith("harbourside.localhost:3000")

    taken = (await console.get("/v1/platform/tenants/slug-check", params={"slug": "northgate"})).json()
    assert taken["available"] is False
    assert "already uses" in taken["problem"]

    reserved = (await console.get("/v1/platform/tenants/slug-check", params={"slug": "admin"})).json()
    assert reserved["available"] is False
    assert "reserved" in reserved["problem"]

    malformed = (await console.get("/v1/platform/tenants/slug-check", params={"slug": "No Spaces"})).json()
    assert malformed["available"] is False

    # Uppercase is normalised rather than refused — the form should not punish
    # somebody for typing a capital.
    assert (await console.get(
        "/v1/platform/tenants/slug-check", params={"slug": "HarbourSide"}
    )).json()["slug"] == "harbourside"


async def test_the_console_edits_a_clinic_but_never_its_address(console):
    """§4 — name, city, plan, timezone and discipline are editable.

    The subdomain is not, and asking for it is a 422 rather than a silent
    no-op: every live session token carries the slug, so renaming one would
    sign the whole clinic out.
    """
    rowan = next(t for t in (await console.get("/v1/platform/tenants")).json() if t["slug"] == "rowan")

    updated = await console.patch(
        f"/v1/platform/tenants/{rowan['id']}",
        json={"name": "Rowan Skin & Laser", "city": "Bath", "plan": "Growth"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Rowan Skin & Laser"
    assert updated.json()["city"] == "Bath"
    assert updated.json()["slug"] == "rowan", "the address is untouched"

    refused = await console.patch(f"/v1/platform/tenants/{rowan['id']}", json={"slug": "rowan-skin"})
    assert refused.status_code == 422

    bad_zone = await console.patch(f"/v1/platform/tenants/{rowan['id']}", json={"timezone": "Mars/Olympus"})
    assert bad_zone.status_code == 422

    await console.patch(
        f"/v1/platform/tenants/{rowan['id']}",
        json={"name": rowan["name"], "city": rowan["city"], "plan": rowan["plan"]},
    )


async def test_the_console_switches_a_module_on_and_the_guard_agrees(console, rowan, client):
    """§5 — the platform's own hand on the switch.

    Rowan has not bought billing, so its billing routes are shut. Switching the
    module on from the console opens them, because both sides read the same
    row — turning it off closes them again.
    """
    assert (await rowan.get("/v1/billing/invoices")).status_code == 403

    tenant_id = rowan.tenant_id
    on = await console.put(
        f"/v1/platform/tenants/{tenant_id}/features/billing", json={"status": "active"}
    )
    assert on.status_code == 200, on.text
    assert on.json()["status"] == "active"
    assert on.json()["activated_at"] is not None

    assert (await rowan.get("/v1/billing/invoices")).status_code == 200

    off = await console.put(
        f"/v1/platform/tenants/{tenant_id}/features/billing", json={"status": "inactive"}
    )
    assert off.status_code == 200
    assert (await rowan.get("/v1/billing/invoices")).status_code == 403

    # The product does not work without its core, so that is not a switch.
    core = await console.put(
        f"/v1/platform/tenants/{tenant_id}/features/patient", json={"status": "inactive"}
    )
    assert core.status_code == 409


async def test_a_support_session_can_work_inside_a_clinic_and_is_named_in_the_trail(
    console, client, northgate
):
    """§6 — super admin access, and what it costs to use it.

    The session reads and writes as a practice admin would. What separates it
    from one is the trail: every line it leaves says `platform` and names the
    person who opened it, never the clinic account it writes through.
    """
    tenant_id = northgate.tenant_id
    opened = await console.post(f"/v1/platform/tenants/{tenant_id}/impersonate")
    assert opened.status_code == 200, opened.text
    support = opened.json()["access_token"]
    assert opened.json()["url"].startswith("http://northgate.localhost")

    headers = {"Host": NORTHGATE, "Authorization": f"Bearer {support}"}

    me = (await client.get("/v1/auth/me", headers=headers)).json()
    assert me["impersonated"] is True
    assert me["platform_user"]["email"] == "ines@astrale.health"
    assert me["role"] == "tenant_admin"

    # Full access: a clinic's own configuration, which only an admin may read.
    assert (await client.get("/v1/tenant/config", headers=headers)).status_code == 200

    # And it can write.
    created = await client.post(
        "/v1/patients",
        headers=headers,
        json={"full_name": "Support Test", "sex": "other", "dob": "1990-01-01", "phone": "07000 000000"},
    )
    assert created.status_code == 201, created.text

    trail = await audit_lines(tenant_id)

    opened_line = next(e for e in trail if e["action"] == "platform.impersonate")
    assert opened_line["actor_kind"] == "platform"
    assert "ines@astrale.health" in opened_line["actor_label"]

    write_line = next(e for e in trail if e["action"] == "patient.create")
    assert write_line["actor_kind"] == "platform", "a support write is not a clinic write"
    assert "platform support" in write_line["actor_label"]
    assert "ines@astrale.health" in write_line["actor_label"], "the trail names the person"


async def test_a_support_account_is_not_a_way_in(console, northgate, client):
    """The row a support session writes through is not an account anybody has.

    It is invisible to the clinic, it has no password, and a link cannot be
    minted for it — so the one thing it is for is the only thing it does.
    """
    tenant_id = northgate.tenant_id
    await console.post(f"/v1/platform/tenants/{tenant_id}/impersonate")

    staff = (await northgate.get("/v1/users")).json()
    assert not any("platform.invalid" in u["email"] for u in staff)
    assert not any("support" in u["name"].lower() for u in staff)

    # Nor from the console's own view of the clinic.
    console_view = (await console.get(f"/v1/platform/tenants/{tenant_id}/users")).json()
    assert not any("platform.invalid" in u["email"] for u in console_view)

    # It cannot be asked for a link either. `platform.invalid` is the reserved
    # TLD of RFC 2606 — no mail will ever reach it, and it is not even an
    # address this API will accept in a form.
    asked = await client.post(
        "/v1/auth/password/forgot",
        headers={"Host": NORTHGATE},
        json={"email": "support+00000000-0000-0000-0000-000000000000@platform.invalid"},
    )
    assert asked.status_code == 422


async def test_a_support_session_cannot_get_back_into_the_console(console, northgate, client):
    tenant_id = northgate.tenant_id
    support = (await console.post(f"/v1/platform/tenants/{tenant_id}/impersonate")).json()["access_token"]

    response = await client.get(
        "/v1/platform/tenants", headers={"Host": CONSOLE, "Authorization": f"Bearer {support}"}
    )
    assert response.status_code == 401


async def test_a_support_session_is_scoped_to_the_clinic_it_was_opened_for(
    console, northgate, clearview, client
):
    support = (
        await console.post(f"/v1/platform/tenants/{northgate.tenant_id}/impersonate")
    ).json()["access_token"]

    response = await client.get(
        "/v1/patients", headers={"Host": CLEARVIEW, "Authorization": f"Bearer {support}"}
    )
    assert response.status_code == 401


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



def test_every_request_transaction_commits_before_the_response():
    """`db_session` must be function-scoped everywhere it is used.

    Request-scoped, its commit runs after the response is sent: a client that
    creates something and immediately re-reads the list can miss the new row,
    which is exactly what the module page's "Request activation" did.

    Read from source, like the RLS check above: FastAPI keeps included routers
    in private structures, and walking those would break on an upgrade.
    """
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    found = [
        f"{path.relative_to(app_dir)}:{source[:m.start()].count(chr(10)) + 1}"
        for path in app_dir.rglob("*.py")
        for source in [path.read_text(encoding="utf-8")]
        for m in re.finditer(r"Depends\(\s*db_session\s*(\)|,(?![^)]*scope=\"function\")[^)]*\))", source)
    ]
    assert not found, f"db_session without scope='function' at: {found}"


async def test_switching_a_module_back_on_restarts_its_date(console, rowan):
    """The console counts billing from `activated_at`, so a module switched off
    and on again must start from the day it came back — not look billed for
    the whole time it was off. Switching on one that is already on changes
    nothing."""
    import datetime as _dt
    import uuid as _uuid

    from sqlalchemy import select

    from app.db.session import tenant_session
    from app.models.tenancy import TenantFeature

    tenant_id = rowan.tenant_id
    url = f"/v1/platform/tenants/{tenant_id}/features/inventory"
    today = (await rowan.get("/v1/tenant")).json()["today"]
    before = next(
        (
            f["status"]
            for f in (await console.get(f"/v1/platform/tenants/{tenant_id}/features")).json()
            if f["feature_key"] == "inventory"
        ),
        "inactive",
    )

    assert (await console.put(url, json={"status": "active"})).status_code == 200
    long_ago = _dt.date(2024, 9, 18)
    async with tenant_session(_uuid.UUID(tenant_id)) as session:
        row = (
            await session.execute(
                select(TenantFeature).where(
                    TenantFeature.tenant_id == _uuid.UUID(tenant_id),
                    TenantFeature.feature_key == "inventory",
                )
            )
        ).scalar_one()
        row.activated_at = long_ago

    again = await console.put(url, json={"status": "active"})
    assert again.json()["activated_at"] == long_ago.isoformat(), "already on: left alone"

    assert (await console.put(url, json={"status": "inactive"})).status_code == 200
    back = await console.put(url, json={"status": "active"})
    assert back.status_code == 200, back.text
    assert back.json()["activated_at"] == today

    # Leave the clinic as the seed had it, for the tests that run after this one.
    await console.put(url, json={"status": before})
