"""The platform's own logo — set from the console's Settings, drawn in every
email's header and on the subscription bill, and the built-in one until then.

Same storage technique as `test_clinic_logo.py`: the row holds the bytes by
default, and a fake S3 bucket when a test switches the backend.
"""

from __future__ import annotations

import io
from datetime import date
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select

from app.core.brand import FULL_NAME, PRODUCT_TAGLINE
from app.db.session import tenant_session
from app.models.tenancy import AuditEvent, PlatformSettings
from app.services import mailer, platform_logo, storage
from app.services.documents import build_subscription_invoice_pdf
from app.services.platform import PLATFORM_SETTINGS_ID
from tests.conftest import CONSOLE, NORTHGATE
from tests.test_storage_and_mail import BUCKET, fake_aws, use_s3  # noqa: F401

URL = "/v1/platform/settings/logo"


def _png(width: int, height: int, colour=(255, 255, 255, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (width, height), colour).save(out, format="PNG")
    return out.getvalue()


#: a lockup's shape: far wider than tall
WIDE_PNG = _png(400, 80)
#: a mark alone
SQUARE_PNG = _png(120, 120)


@pytest.fixture(autouse=True)
async def _no_lingering_logo(client):
    """The settings row is a singleton in a database wiped once per session,
    so a test that uploads a logo must take it off again — or every email
    and bill in a later test carries it."""
    yield
    await _clear_logo()
    await storage.drain()
    platform_logo.prepare_email_mark.cache_clear()


async def _clear_logo() -> None:
    async with tenant_session(None) as session:
        row = await session.get(PlatformSettings, PLATFORM_SETTINGS_ID)
        row.logo_filename = row.logo_content_type = row.logo_data = row.logo_storage_key = None


async def _platform_audit(action: str) -> list[AuditEvent]:
    async with tenant_session(None) as session:
        return list(
            (await session.execute(
                select(AuditEvent).where(AuditEvent.action == action, AuditEvent.tenant_id.is_(None))
                .order_by(AuditEvent.at.desc())
            )).scalars()
        )


# --- the setting ----------------------------------------------------------------------


async def test_until_one_is_uploaded_the_built_in_logo_is_served(console):
    settings = (await console.get("/v1/platform/settings")).json()
    assert settings["has_logo"] is False

    served = await console.get(URL)
    assert served.status_code == 200, served.text
    assert served.headers["x-logo-source"] == "default"
    assert served.headers["content-type"].startswith("image/png")
    assert served.content == platform_logo.default_logo()


async def test_a_super_admin_uploads_a_logo_and_it_is_served(console):
    uploaded = await console.post(URL, files={"file": ("astrale.png", WIDE_PNG, "image/png")})
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["has_logo"] is True
    assert (await console.get("/v1/platform/settings")).json()["has_logo"] is True

    served = await console.get(URL)
    assert served.status_code == 200
    assert served.headers["x-logo-source"] == "custom"
    assert served.content == WIDE_PNG


async def test_removing_the_logo_goes_back_to_the_built_in_one(console):
    await console.post(URL, files={"file": ("astrale.png", WIDE_PNG, "image/png")})

    removed = await console.delete(URL)
    assert removed.status_code == 200, removed.text
    assert removed.json()["has_logo"] is False

    served = await console.get(URL)
    assert served.headers["x-logo-source"] == "default"
    assert served.content == platform_logo.default_logo()


async def test_changing_it_is_recorded_against_who_did_it(console):
    await console.post(URL, files={"file": ("astrale.png", WIDE_PNG, "image/png")})
    await console.delete(URL)

    (set_row, *_) = await _platform_audit("platform_settings.logo_set")
    assert set_row.actor_kind == "platform"
    assert set_row.entity == "platform_settings"
    assert set_row.meta == {"filename": "astrale.png", "bytes": len(WIDE_PNG)}
    (removed_row, *_) = await _platform_audit("platform_settings.logo_removed")
    assert removed_row.actor_id == set_row.actor_id


async def test_the_support_desk_sees_the_logo_but_cannot_change_it(console_support):
    assert (await console_support.get(URL)).status_code == 200
    refused = await console_support.post(URL, files={"file": ("astrale.png", WIDE_PNG, "image/png")})
    assert refused.status_code == 403
    assert (await console_support.delete(URL)).status_code == 403


async def test_nobody_outside_the_console_can_reach_it(client, northgate):
    assert (await client.get(URL, headers={"Host": CONSOLE})).status_code == 401
    # a clinic's own session, on the clinic's own address, is not a console session
    assert (await northgate.get(URL)).status_code in (401, 403, 404)
    refused = await northgate.post(URL, files={"file": ("astrale.png", WIDE_PNG, "image/png")})
    assert refused.status_code in (401, 403, 404)


@pytest.mark.parametrize(
    ("name", "data", "content_type"),
    [
        ("mark.pdf", b"%PDF-1.4", "application/pdf"),
        ("mark.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>', "image/svg+xml"),
        # says it is a PNG, is not one: the bytes decide, not the file name
        ("mark.png", b"<html>not an image</html>", "image/png"),
        # starts like a PNG and then is not one
        ("mark.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png"),
        ("empty.png", b"", "image/png"),
    ],
)
async def test_anything_but_a_png_jpeg_or_webp_is_refused(console, name, data, content_type):
    refused = await console.post(URL, files={"file": (name, data, content_type)})
    assert refused.status_code == 422, refused.text
    assert (await console.get("/v1/platform/settings")).json()["has_logo"] is False


async def test_a_logo_over_the_size_limit_is_refused(console):
    from app.services.tenants import MAX_LOGO_BYTES

    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_LOGO_BYTES
    refused = await console.post(URL, files={"file": ("big.png", oversized, "image/png")})
    assert refused.status_code == 422


async def test_jpeg_and_webp_are_stored_as_what_the_bytes_say(console):
    out = io.BytesIO()
    Image.new("RGB", (60, 60), (10, 20, 30)).save(out, format="WEBP")
    uploaded = await console.post(URL, files={"file": ("mark.webp", out.getvalue(), "image/webp")})
    assert uploaded.status_code == 200, uploaded.text
    assert (await console.get(URL)).headers["content-type"].startswith("image/webp")


async def test_under_s3_the_logo_lives_in_the_bucket_at_one_fixed_key(fake_aws, monkeypatch, console):  # noqa: F811
    use_s3(monkeypatch)
    assert (await console.post(URL, files={"file": ("a.png", WIDE_PNG, "image/png")})).status_code == 200

    async with tenant_session(None) as session:
        row = await session.get(PlatformSettings, PLATFORM_SETTINGS_ID)
        key = row.logo_storage_key
    assert key == "pms/platform/logo"
    assert fake_aws.get_object(Bucket=BUCKET, Key=key)["Body"].read() == WIDE_PNG
    assert (await console.get(URL)).content == WIDE_PNG

    assert (await console.delete(URL)).status_code == 200
    await storage.drain()
    assert fake_aws.list_objects_v2(Bucket=BUCKET, Prefix=key).get("KeyCount", 0) == 0


# --- what a document draws -------------------------------------------------------------


async def test_a_bucket_that_will_not_answer_falls_back_to_the_built_in_logo(console, monkeypatch):
    await console.post(URL, files={"file": ("a.png", WIDE_PNG, "image/png")})

    async def unavailable(*_a, **_k):
        raise storage.StorageUnavailable("down")

    monkeypatch.setattr(platform_logo, "uploaded_logo", unavailable)
    async with tenant_session(None) as session:
        assert await platform_logo.logo_bytes(session) == platform_logo.default_logo()


async def test_an_upload_that_will_not_decode_falls_back_to_the_built_in_mark():
    """Validation refuses such a file, so it can only be a row written some
    other way — which must cost the logo, never the message."""
    async with tenant_session(None) as session:
        row = await session.get(PlatformSettings, PLATFORM_SETTINGS_ID)
        row.logo_filename, row.logo_content_type, row.logo_data = "x.png", "image/png", b"\x89PNG broken"
    async with tenant_session(None) as session:
        mark = await platform_logo.email_mark(session)
    assert mark == platform_logo.prepare_email_mark(platform_logo.default_logo())


async def test_the_email_mark_is_small_and_keeps_the_logos_shape(console):
    builtin = platform_logo.prepare_email_mark(platform_logo.default_logo())
    assert builtin.wide and builtin.height == 22 and 100 < builtin.width <= 160
    assert len(builtin.data) < 40_000, "the 160 KB file is not mailed as it is"

    await console.post(URL, files={"file": ("mark.png", SQUARE_PNG, "image/png")})
    async with tenant_session(None) as session:
        square = await platform_logo.email_mark(session)
    assert not square.wide and (square.width, square.height) == (34, 34)
    with Image.open(io.BytesIO(square.data)) as drawn:
        assert drawn.size == (68, 68), "drawn at twice its size, for a high-density screen"


# --- the email --------------------------------------------------------------------------


def _invitation(mark) -> mailer.Email:
    return mailer.invitation(
        to="p@clinic.example", name="Priya", clinic="Northgate Dental Studio",
        url="https://northgate.example/accept?token=a", expires_in_days=7, mark=mark,
    )


def test_an_email_carries_the_logo_inside_itself():
    mark = platform_logo.prepare_email_mark(platform_logo.default_logo())
    email = _invitation(mark)

    assert f'src="cid:{mailer.MARK_CID}"' in email.html
    assert 'alt="Astrale"' in email.html
    assert ">A</td>" not in email.html, "the placeholder square is gone"

    message = email.as_message()
    assert message.get_content_type() == "multipart/alternative"
    text, related = message.get_payload()
    assert text.get_content_type() == "text/plain"
    assert related.get_content_type() == "multipart/related"
    html, image = related.get_payload()
    assert html.get_content_type() == "text/html"
    assert image.get_content_type() == "image/png"
    assert image["Content-ID"] == f"<{mailer.MARK_CID}>"
    assert image.get_content_disposition() == "inline"
    assert image.get_content() == mark.data


def test_without_a_logo_the_header_is_the_wordmark_and_nothing_points_nowhere():
    email = _invitation(None)
    assert "cid:" not in email.html and "<img" not in email.html
    assert ">Astrale</div>" in email.html
    assert ">A</td>" not in email.html

    message = email.as_message()
    assert message.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in message.get_payload()] == ["text/plain", "text/html"]


def test_a_mark_alone_sits_beside_the_name_and_a_lockup_stands_on_its_own():
    lockup = _invitation(platform_logo.prepare_email_mark(WIDE_PNG)).html
    alone = _invitation(platform_logo.prepare_email_mark(SQUARE_PNG)).html
    assert ">Astrale</div>" not in lockup, "the lockup already spells the name"
    assert ">Astrale</div>" in alone


def test_the_header_and_footer_name_the_product():
    email = _invitation(None)
    assert f">{PRODUCT_TAGLINE}</div>" in email.html
    assert f"{FULL_NAME}, for independent clinics." in email.html
    assert "Practice OS" not in email.html and "Practice OS" not in email.text


def test_an_invoice_email_keeps_its_pdf_beside_the_inline_logo():
    mark = platform_logo.prepare_email_mark(platform_logo.default_logo())
    email = mailer.invoice_to_patient(
        to="patient@example.com", clinic="Northgate", from_name="Northgate via Astrale", reply_to=None,
        subject="Invoice NG-INV-1", message="Hello", number="NG-INV-1", issued="1 Oct 2026",
        due="8 Oct 2026", total="₹100.00", balance="₹100.00", owing=True, pdf=b"%PDF-1.4",
        filename="NG-INV-1.pdf", mark=mark,
    )
    message = email.as_message()
    assert message.get_content_type() == "multipart/mixed"
    body, attached = message.get_payload()
    assert body.get_content_type() == "multipart/alternative"
    assert body.get_payload()[1].get_content_type() == "multipart/related"
    assert attached.get_filename() == "NG-INV-1.pdf"


async def test_a_reset_email_sent_for_real_carries_the_uploaded_logo(console, client, monkeypatch, link_emails):
    sent: list[mailer.Email] = []

    async def capture(email: mailer.Email) -> None:
        sent.append(email)

    monkeypatch.setattr(mailer, "send", capture)
    await console.post(URL, files={"file": ("mark.png", SQUARE_PNG, "image/png")})

    asked = await client.post(
        "/v1/auth/password/forgot", headers={"Host": NORTHGATE}, json={"email": "admin@northgate.clinic"}
    )
    assert asked.status_code == 202
    assert link_emails.outcomes[-1] == "sent"
    (email,) = sent
    assert email.mark is not None and not email.mark.wide, "the square upload, not the built-in lockup"
    assert f'src="cid:{mailer.MARK_CID}"' in email.html


# --- the subscription bill ---------------------------------------------------------------


def _bill() -> SimpleNamespace:
    return SimpleNamespace(
        number="AST-202610-LOGO", status="open", currency="INR", amount=9999,
        lines=[{"name": "Astrale PMS subscription", "amount": 9999}],
        period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
        issued_on=date(2026, 10, 1), due_on=date(2026, 10, 8), payment_reference="",
    )


def _images(pdf: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf)).pages[0].images)


def _text(pdf: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)


@pytest.mark.parametrize("logo", ["builtin", WIDE_PNG, SQUARE_PNG])
def test_the_bill_draws_the_platform_logo(logo):
    data = platform_logo.default_logo() if logo == "builtin" else logo
    pdf = build_subscription_invoice_pdf(_bill(), clinic="Northgate", hold_on=date(2026, 10, 16), platform_logo=data)
    assert _images(pdf) == 1
    text = _text(pdf)
    assert "Clinic Management System" in text
    assert FULL_NAME in text  # the footer
    assert "Practice OS" not in text


@pytest.mark.parametrize("logo", [None, b"\x89PNG broken"])
def test_the_bill_without_a_usable_logo_prints_the_wordmark(logo):
    pdf = build_subscription_invoice_pdf(_bill(), clinic="Northgate", hold_on=date(2026, 10, 16), platform_logo=logo)
    assert _images(pdf) == 0
    text = _text(pdf)
    assert "Astrale" in text and "Clinic Management System" in text
    assert "SUBSCRIPTION BILL" in text


async def test_a_clinics_bill_pdf_carries_the_uploaded_platform_logo(console, northgate):
    """Read through a clinic's session: the platform row is not the clinic's,
    and must still be readable from it."""
    import uuid

    from app.models.enums import SubscriptionInvoiceStatus
    from app.models.rent import SubscriptionInvoice

    tenant_id = uuid.UUID(northgate.tenant_id)
    await console.post(URL, files={"file": ("mark.png", SQUARE_PNG, "image/png")})
    async with tenant_session(tenant_id) as session:
        bill = SubscriptionInvoice(
            tenant_id=tenant_id, number="AST-209912-LOGO", period_start=date(2099, 12, 1),
            period_end=date(2099, 12, 31), currency="INR", amount=9999,
            lines=[{"name": "Astrale PMS subscription", "amount": 9999}], issued_on=date(2099, 12, 1),
            due_on=date(2099, 12, 8), status=SubscriptionInvoiceStatus.OPEN, reminder_stage="none",
        )
        session.add(bill)
        await session.flush()
        bill_id = bill.id
    try:
        response = await northgate.get(f"/v1/tenant/subscription/invoices/{bill_id}/pdf")
        assert response.status_code == 200, response.text
        assert _images(response.content) == 1
    finally:
        from sqlalchemy import delete

        async with tenant_session(tenant_id) as session:
            await session.execute(delete(SubscriptionInvoice).where(SubscriptionInvoice.id == bill_id))
