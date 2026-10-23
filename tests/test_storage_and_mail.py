"""S3 file storage and SES email, against a fake AWS.

`moto` stands in for AWS inside the process, so these exercise the real boto3
calls — bucket, key, encryption header, raw MIME — without anything leaving
the machine. The rest of the suite runs on the defaults (bytes in the row, the
console mail transport), which is why each test here switches the settings
itself and puts them back.
"""

from __future__ import annotations

import uuid

import boto3
import pytest
from moto import mock_aws
from moto.core import DEFAULT_ACCOUNT_ID
from moto.ses.models import ses_backends

from app.core import aws
from app.core.config import Settings, settings
from app.services import mailer, storage
from tests.test_pricing_and_files import PNG_BYTES, _fresh_order

REGION = "eu-west-2"
BUCKET = "pms-test-files"


@pytest.fixture
def fake_aws(monkeypatch):
    """A fake AWS account with one bucket and one verified sending domain."""
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setattr(settings, "aws_region", REGION)
    monkeypatch.setattr(settings, "aws_access_key_id", "")
    monkeypatch.setattr(settings, "aws_secret_access_key", "")
    monkeypatch.setattr(settings, "aws_session_token", "")
    aws.reset_clients()
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        boto3.client("sesv2", region_name=REGION).create_email_identity(EmailIdentity="astrale.health")
        yield s3
    aws.reset_clients()


def use_s3(monkeypatch, bucket: str = BUCKET) -> None:
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "s3_bucket", bucket)
    monkeypatch.setattr(settings, "s3_region", "")
    monkeypatch.setattr(settings, "s3_endpoint_url", "")
    monkeypatch.setattr(settings, "s3_prefix", "pms")
    monkeypatch.setattr(settings, "s3_server_side_encryption", "AES256")


async def _row(tenant_id: str, attachment_id: str) -> tuple[bytes | None, str | None]:
    """What the database holds for one attachment, read as that clinic."""
    from sqlalchemy import select
    from sqlalchemy.orm import undefer

    from app.db.session import tenant_session
    from app.models.clinical import LabAttachment

    async with tenant_session(uuid.UUID(tenant_id)) as session:
        row = (
            await session.execute(
                select(LabAttachment)
                .options(undefer(LabAttachment.data))
                .where(LabAttachment.id == uuid.UUID(attachment_id))
            )
        ).scalar_one_or_none()
        return (row.data, row.storage_key) if row else (None, None)


# --- S3 storage ----------------------------------------------------------------


async def test_a_report_is_stored_in_the_bucket_and_not_in_the_row(
    fake_aws, monkeypatch, northgate, northgate_doctor
):
    use_s3(monkeypatch)
    order = await _fresh_order(northgate, northgate_doctor)

    uploaded = await northgate.post(
        f"/v1/lab/orders/{order['id']}/attachments",
        files={"file": ("scan.png", PNG_BYTES, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["size_bytes"] == len(PNG_BYTES)

    data, key = await _row(northgate.tenant_id, body["id"])
    assert data is None
    # Tenant first, then order, then the attachment's own id — all server-minted.
    assert key == (
        f"pms/tenants/{northgate.tenant_id}/lab-orders/{order['id']}/attachments/{body['id']}"
    )

    stored = fake_aws.get_object(Bucket=BUCKET, Key=key)
    assert stored["Body"].read() == PNG_BYTES
    assert stored["ContentType"] == "image/png"
    assert stored["ServerSideEncryption"] == "AES256"

    downloaded = await northgate.get(f"/v1/lab/orders/{order['id']}/attachments/{body['id']}")
    assert downloaded.status_code == 200
    assert downloaded.content == PNG_BYTES
    assert downloaded.headers["content-type"].startswith("image/png")


async def test_removing_a_report_removes_its_object(fake_aws, monkeypatch, northgate, northgate_doctor):
    use_s3(monkeypatch)
    order = await _fresh_order(northgate, northgate_doctor)
    body = (
        await northgate.post(
            f"/v1/lab/orders/{order['id']}/attachments",
            files={"file": ("draft.pdf", b"%PDF-1.4 draft", "application/pdf")},
        )
    ).json()
    _, key = await _row(northgate.tenant_id, body["id"])

    deleted = await northgate.delete(f"/v1/lab/orders/{order['id']}/attachments/{body['id']}")
    assert deleted.status_code == 204
    await storage.drain()

    listing = fake_aws.list_objects_v2(Bucket=BUCKET, Prefix=key)
    assert listing.get("KeyCount", 0) == 0
    assert (await northgate.get(f"/v1/lab/orders/{order['id']}/attachments")).json() == []


async def test_a_refused_delete_leaves_the_object_where_it_is(
    fake_aws, monkeypatch, northgate, northgate_doctor
):
    """The object goes only when the row's removal commits — never before."""
    use_s3(monkeypatch)
    order = await _fresh_order(northgate, northgate_doctor)
    body = (
        await northgate.post(
            f"/v1/lab/orders/{order['id']}/attachments",
            files={"file": ("keep.pdf", b"%PDF-1.4 keep", "application/pdf")},
        )
    ).json()
    _, key = await _row(northgate.tenant_id, body["id"])

    # A doctor may not remove lab paper; the request fails before anything is dropped.
    refused = await northgate_doctor.delete(
        f"/v1/lab/orders/{order['id']}/attachments/{body['id']}"
    )
    assert refused.status_code == 403
    await storage.drain()

    assert fake_aws.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"%PDF-1.4 keep"


async def test_a_file_stored_in_the_row_still_opens_after_moving_to_s3(
    fake_aws, monkeypatch, northgate, northgate_doctor
):
    order = await _fresh_order(northgate, northgate_doctor)
    before = (
        await northgate.post(
            f"/v1/lab/orders/{order['id']}/attachments",
            files={"file": ("old.png", PNG_BYTES, "image/png")},
        )
    ).json()
    data, key = await _row(northgate.tenant_id, before["id"])
    assert data == PNG_BYTES and key is None

    use_s3(monkeypatch)
    downloaded = await northgate.get(f"/v1/lab/orders/{order['id']}/attachments/{before['id']}")
    assert downloaded.status_code == 200
    assert downloaded.content == PNG_BYTES


async def test_another_clinic_cannot_reach_a_file_held_in_s3(
    fake_aws, monkeypatch, northgate, northgate_doctor, rowan
):
    use_s3(monkeypatch)
    order = await _fresh_order(northgate, northgate_doctor)
    body = (
        await northgate.post(
            f"/v1/lab/orders/{order['id']}/attachments",
            files={"file": ("scan.png", PNG_BYTES, "image/png")},
        )
    ).json()

    stolen = await rowan.get(f"/v1/lab/orders/{order['id']}/attachments/{body['id']}")
    assert stolen.status_code == 404


async def test_a_store_that_refuses_the_upload_is_a_503_and_leaves_no_row(
    fake_aws, monkeypatch, northgate, northgate_doctor
):
    use_s3(monkeypatch, bucket="a-bucket-that-does-not-exist")
    order = await _fresh_order(northgate, northgate_doctor)

    response = await northgate.post(
        f"/v1/lab/orders/{order['id']}/attachments",
        files={"file": ("scan.png", PNG_BYTES, "image/png")},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert (await northgate.get(f"/v1/lab/orders/{order['id']}/attachments")).json() == []


async def test_s3_without_a_bucket_is_a_503_not_a_crash(
    fake_aws, monkeypatch, northgate, northgate_doctor
):
    use_s3(monkeypatch, bucket="")
    order = await _fresh_order(northgate, northgate_doctor)

    response = await northgate.post(
        f"/v1/lab/orders/{order['id']}/attachments",
        files={"file": ("scan.png", PNG_BYTES, "image/png")},
    )
    assert response.status_code == 503


# --- SES email -------------------------------------------------------------------


def _sent() -> list:
    return ses_backends[DEFAULT_ACCOUNT_ID][REGION].sent_messages


async def test_ses_sends_the_same_message_smtp_would(fake_aws, monkeypatch):
    monkeypatch.setattr(settings, "mail_provider", "ses")
    monkeypatch.setattr(settings, "mail_from", "Astrale PMS <no-reply@astrale.health>")
    monkeypatch.setattr(settings, "ses_region", "")
    monkeypatch.setattr(settings, "ses_configuration_set", "")
    before = len(_sent())

    email = mailer.invitation(
        to="new.admin@clinic.example", name="New Admin", clinic="Northgate",
        url="https://northgate.example/accept-invitation?token=abc", expires_in_days=7,
    )
    await mailer.send(email)

    sent = _sent()[before:]
    assert len(sent) == 1
    raw = sent[0].raw_data
    assert "To: new.admin@clinic.example" in raw
    assert "Subject: Set up your Northgate account" in raw
    assert "From: Astrale PMS <no-reply@astrale.health>" in raw
    # Both parts, as the SMTP transport sends them.
    assert "text/plain" in raw and "text/html" in raw


async def test_ses_keeps_the_reply_to_on_a_support_request(fake_aws, monkeypatch):
    monkeypatch.setattr(settings, "mail_provider", "ses")
    monkeypatch.setattr(settings, "mail_from", "Astrale PMS <no-reply@astrale.health>")
    before = len(_sent())

    await mailer.send(
        mailer.Email(
            to="support@astrale.health", subject="Enable Lab", text="Please.",
            reply_to="admin@northgate.clinic",
        )
    )
    assert "Reply-To: admin@northgate.clinic" in _sent()[before:][0].raw_data


async def test_an_ses_refusal_is_a_mail_error(fake_aws, monkeypatch):
    """An unverified sender is refused by SES — and callers see the same
    `MailError` an SMTP failure raises, so they need no second code path."""
    monkeypatch.setattr(settings, "mail_provider", "ses")
    monkeypatch.setattr(settings, "mail_from", "Someone <someone@not-verified.example>")

    with pytest.raises(mailer.MailError):
        await mailer.send(mailer.Email(to="x@clinic.example", subject="Hi", text="Hello"))


# --- production refuses to boot half-configured ----------------------------------


def _production(**overrides) -> Settings:
    base = {
        "environment": "production",
        "jwt_secret": "x" * 48,
        "cookie_secure": True,
        "public_scheme": "https",
        "mail_provider": "smtp",
        "flower_enabled": False,
        "database_url": "postgresql+asyncpg://pms_app:pw@db/pms",
    }
    return Settings(_env_file=None, **{**base, **overrides})


def test_production_needs_a_bucket_and_region_for_s3():
    problems = _production(storage_backend="s3", aws_region="").validate_for_production()
    assert any("S3_BUCKET" in p for p in problems)
    assert any("S3_REGION" in p for p in problems)

    ok = _production(storage_backend="s3", s3_bucket="b", aws_region=REGION)
    assert ok.validate_for_production() == []


def test_production_needs_a_region_for_ses():
    problems = _production(mail_provider="ses", aws_region="", ses_region="").validate_for_production()
    assert any("SES_REGION" in p for p in problems)
    assert _production(mail_provider="ses", ses_region=REGION).validate_for_production() == []
