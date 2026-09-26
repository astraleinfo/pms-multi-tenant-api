"""The clinic's own logo — set from its settings, printed on its PDFs.

Same technique as `test_storage_and_mail.py`: the row holds the bytes by
default, and moves to a fake S3 bucket when the test switches the backend.
"""

from __future__ import annotations

import io
import uuid

import pytest

from tests.conftest import sign_in
from tests.test_pricing_and_files import PNG_BYTES
from tests.test_storage_and_mail import BUCKET, fake_aws, use_s3  # noqa: F401


@pytest.fixture(autouse=True)
async def _no_lingering_logo(northgate):
    """The database is wiped once per session, not per test, and a clinic's
    logo is a singleton — so a test that sets one has to take it off again,
    or a later test (in this file or another) inherits it. Draining pending
    deletes matters here specifically: an S3 test's cleanup would otherwise
    leave a delete task to fire whenever the event loop gets to it — possibly
    during a later test, against whatever the bucket mock looks like by then."""
    from app.services import storage

    yield
    await northgate.delete("/v1/tenant/config/logo")
    await storage.drain()


async def _logo_row(tenant_id: str) -> tuple[bytes | None, str | None, str | None]:
    """What the database holds for one clinic's logo, read as that clinic."""
    from sqlalchemy import select
    from sqlalchemy.orm import undefer

    from app.db.session import tenant_session
    from app.models.tenancy import TenantConfig

    async with tenant_session(uuid.UUID(tenant_id)) as session:
        row = (
            await session.execute(
                select(TenantConfig)
                .options(undefer(TenantConfig.logo_data))
                .where(TenantConfig.tenant_id == uuid.UUID(tenant_id))
            )
        ).scalar_one()
        return row.logo_data, row.logo_storage_key, row.logo_filename


async def test_a_clinic_starts_with_no_logo(northgate):
    config = (await northgate.get("/v1/tenant/config")).json()
    assert config["has_logo"] is False

    missing = await northgate.get("/v1/tenant/config/logo")
    assert missing.status_code == 404


async def test_uploading_a_logo_stores_it_and_it_can_be_downloaded(northgate):
    uploaded = await northgate.post(
        "/v1/tenant/config/logo",
        files={"file": ("mark.png", PNG_BYTES, "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["has_logo"] is True

    downloaded = await northgate.get("/v1/tenant/config/logo")
    assert downloaded.status_code == 200
    assert downloaded.content == PNG_BYTES
    assert downloaded.headers["content-type"].startswith("image/png")


async def test_replacing_a_logo_serves_only_the_latest_one(northgate):
    other_png = PNG_BYTES.replace(b"IEND", b"IEND")  # same bytes, distinct upload
    await northgate.post("/v1/tenant/config/logo", files={"file": ("old.png", PNG_BYTES, "image/png")})
    replaced = await northgate.post(
        "/v1/tenant/config/logo", files={"file": ("new.jpg", other_png, "image/png")}
    )
    assert replaced.status_code == 200, replaced.text

    data, key, filename = await _logo_row(northgate.tenant_id)
    assert filename == "new.jpg"
    assert key is None and data == other_png


async def test_removing_a_logo(northgate):
    await northgate.post("/v1/tenant/config/logo", files={"file": ("mark.png", PNG_BYTES, "image/png")})

    removed = await northgate.delete("/v1/tenant/config/logo")
    assert removed.status_code == 200, removed.text
    assert removed.json()["has_logo"] is False

    assert (await northgate.get("/v1/tenant/config/logo")).status_code == 404


async def test_only_an_admin_can_change_the_logo_but_anyone_can_see_it(northgate, northgate_doctor):
    refused = await northgate_doctor.post(
        "/v1/tenant/config/logo", files={"file": ("mark.png", PNG_BYTES, "image/png")}
    )
    assert refused.status_code == 403

    await northgate.post("/v1/tenant/config/logo", files={"file": ("mark.png", PNG_BYTES, "image/png")})
    seen = await northgate_doctor.get("/v1/tenant/config/logo")
    assert seen.status_code == 200

    assert (await northgate_doctor.delete("/v1/tenant/config/logo")).status_code == 403


async def test_a_logo_that_is_not_an_image_is_refused(northgate):
    refused = await northgate.post(
        "/v1/tenant/config/logo", files={"file": ("mark.pdf", b"%PDF-1.4", "application/pdf")}
    )
    assert refused.status_code == 422


async def test_a_logo_over_the_size_limit_is_refused(northgate):
    from app.services.tenants import MAX_LOGO_BYTES

    oversized = b"\x00" * (MAX_LOGO_BYTES + 1)
    refused = await northgate.post(
        "/v1/tenant/config/logo", files={"file": ("big.png", oversized, "image/png")}
    )
    assert refused.status_code == 422


async def test_another_clinic_cannot_reach_this_clinics_logo(northgate, clearview):
    await northgate.post("/v1/tenant/config/logo", files={"file": ("mark.png", PNG_BYTES, "image/png")})
    assert (await clearview.get("/v1/tenant/config/logo")).status_code == 404


async def test_a_logo_is_stored_in_the_bucket_under_s3(fake_aws, monkeypatch, northgate):
    use_s3(monkeypatch)
    uploaded = await northgate.post(
        "/v1/tenant/config/logo", files={"file": ("mark.png", PNG_BYTES, "image/png")}
    )
    assert uploaded.status_code == 200, uploaded.text

    data, key, _ = await _logo_row(northgate.tenant_id)
    assert data is None
    assert key == f"pms/tenants/{northgate.tenant_id}/logo"

    stored = fake_aws.get_object(Bucket=BUCKET, Key=key)
    assert stored["Body"].read() == PNG_BYTES

    downloaded = await northgate.get("/v1/tenant/config/logo")
    assert downloaded.status_code == 200
    assert downloaded.content == PNG_BYTES


async def test_replacing_an_s3_logo_overwrites_the_same_key_not_a_new_one(fake_aws, monkeypatch, northgate):
    use_s3(monkeypatch)
    first = await northgate.post(
        "/v1/tenant/config/logo", files={"file": ("old.png", PNG_BYTES, "image/png")}
    )
    assert first.status_code == 200, first.text
    _, first_key, _ = await _logo_row(northgate.tenant_id)

    new_png = PNG_BYTES + b""  # distinguishable only by content identity in the assertion below
    second = await northgate.post(
        "/v1/tenant/config/logo", files={"file": ("new.png", new_png, "image/png")}
    )
    assert second.status_code == 200, second.text
    _, second_key, _ = await _logo_row(northgate.tenant_id)

    assert first_key == second_key
    listing = fake_aws.list_objects_v2(Bucket=BUCKET, Prefix=first_key)
    assert listing["KeyCount"] == 1


async def test_removing_an_s3_logo_removes_its_object(fake_aws, monkeypatch, northgate):
    from app.services import storage

    use_s3(monkeypatch)
    await northgate.post("/v1/tenant/config/logo", files={"file": ("mark.png", PNG_BYTES, "image/png")})
    _, key, _ = await _logo_row(northgate.tenant_id)

    assert (await northgate.delete("/v1/tenant/config/logo")).status_code == 200
    await storage.drain()

    listing = fake_aws.list_objects_v2(Bucket=BUCKET, Prefix=key)
    assert listing.get("KeyCount", 0) == 0
