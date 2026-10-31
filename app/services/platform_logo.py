"""The platform's own logo — set from the console's Settings, drawn in the
header of every email the platform sends and on the subscription bill.

Stored the way a clinic's logo is (`services/tenants.py`): the bytes in the
`platform_settings` row, or in S3 under one fixed key. Unlike a clinic's, it
is never absent. Until somebody uploads one — and again once it is removed —
the built-in logo bundled with the API (`app/assets/brand/`) stands in, so a
read here always has something to draw. The web app keeps its own copy of the
same file for the console; the API cannot reach that one in production.

Every read that feeds a document is forgiving: a bucket that will not answer,
or bytes that will not decode, fall back to the built-in logo, and past that
to no logo at all. An email or a bill is never refused over its decoration.
"""

from __future__ import annotations

import functools
import io
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.core.errors import ValidationFailed
from app.models.tenancy import PlatformSettings
from app.services import storage
from app.services.mailer import EmailMark
from app.services.tenants import ALLOWED_LOGO_CONTENT_TYPES, MAX_LOGO_BYTES

log = logging.getLogger("pms.brand")

#: The white-on-transparent lockup the web console's rail draws, drawn for a
#: dark ground — which is what both an email's header and the bill's band are.
DEFAULT_LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "brand" / "astrale-logo-mark.png"
DEFAULT_LOGO_CONTENT_TYPE = "image/png"


@functools.cache
def default_logo() -> bytes | None:
    """The built-in logo's bytes, read once — or `None` if the file is missing
    from this build, which only costs the logo, never the message."""
    try:
        return DEFAULT_LOGO_PATH.read_bytes()
    except OSError:
        log.exception("the built-in logo is missing from %s", DEFAULT_LOGO_PATH)
        return None


def sniff(data: bytes) -> str | None:
    """What the bytes say they are, from their first few — `None` for anything
    but a PNG, JPEG or WebP. The declared content type is only the browser's
    guess from a file name; this is what decides."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _decodes(data: bytes) -> bool:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        return True
    except Exception:  # noqa: BLE001 - any decoder complaint means "not a usable image"
        return False


async def _row(session: AsyncSession) -> PlatformSettings:
    """The settings row with the logo's bytes loaded — every other read
    leaves `logo_data` deferred, so an unrelated call never drags an image
    across the wire."""
    from app.services import platform as platform_service  # deferred: see `_settings`

    row = (
        await session.execute(
            select(PlatformSettings)
            .options(undefer(PlatformSettings.logo_data))
            .where(PlatformSettings.id == platform_service.PLATFORM_SETTINGS_ID)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return row if row is not None else await _settings(session)


async def _settings(session: AsyncSession) -> PlatformSettings:
    """The settings row, created if missing. Imported here rather than at the
    top: `services/platform.py` imports the schemas, which import the invoice
    email, which imports this module."""
    from app.services import platform as platform_service

    return await platform_service.get_platform_settings(session)


async def set_logo(
    session: AsyncSession, *, filename: str, content_type: str, data: bytes
) -> PlatformSettings:
    if not data:
        raise ValidationFailed("That file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise ValidationFailed(
            f"Logos are limited to {MAX_LOGO_BYTES // (1024 * 1024)} MB.",
            details={"size_bytes": len(data), "limit_bytes": MAX_LOGO_BYTES},
        )
    declared = (content_type or "").split(";")[0].strip().lower()
    kind = sniff(data)
    if declared not in ALLOWED_LOGO_CONTENT_TYPES or kind is None or not _decodes(data):
        raise ValidationFailed(
            "A logo has to be a PNG, JPEG or WebP image.", details={"content_type": declared}
        )

    row = await _row(session)
    old_key = row.logo_storage_key

    key: str | None = None
    if storage.uses_s3():
        key = storage.platform_logo_object_key()
        await storage.put(key, data, kind)

    row.logo_filename = filename.strip()[:200] or "logo"
    row.logo_content_type = kind
    row.logo_data = None if key else data
    row.logo_storage_key = key
    await session.flush()

    if old_key and old_key != key:
        storage.delete_after_commit(session, old_key)
    return row


async def remove_logo(session: AsyncSession) -> PlatformSettings:
    """Back to the built-in logo."""
    row = await _settings(session)
    key = row.logo_storage_key
    row.logo_filename = None
    row.logo_content_type = None
    row.logo_data = None
    row.logo_storage_key = None
    await session.flush()
    if key:
        storage.delete_after_commit(session, key)
    return row


async def uploaded_logo(session: AsyncSession) -> tuple[bytes, str] | None:
    """The uploaded logo and its content type, or `None` when none has been
    uploaded. Raises `StorageUnavailable` if the bucket will not answer —
    the console route reports that; the documents use `logo_bytes`."""
    row = await _row(session)
    if row.logo_filename is None:
        return None
    data = await storage.get(row.logo_storage_key) if row.logo_storage_key else row.logo_data
    if not data:
        return None
    return data, row.logo_content_type or DEFAULT_LOGO_CONTENT_TYPE


async def logo_bytes(session: AsyncSession) -> bytes | None:
    """The logo to draw: the uploaded one, else the built-in one. Never raises
    for a storage failure — a bill or an email goes out with the built-in logo
    rather than not at all."""
    try:
        uploaded = await uploaded_logo(session)
    except storage.StorageUnavailable:
        log.warning("the platform logo could not be read from storage; drawing the built-in one")
        uploaded = None
    return uploaded[0] if uploaded else default_logo()


# --- for an email's header ------------------------------------------------------------

#: The box the header draws a logo in, in CSS pixels: a lockup (wider than
#: tall by two to one or more) is fitted to a line of text's height under
#: which the tagline sits; a mark alone fills the square the old placeholder
#: had. Mirrored by `mailer._lockup`.
_WIDE_BOX = (160, 22)
_SQUARE_BOX = (34, 34)


@functools.lru_cache(maxsize=8)
def prepare_email_mark(data: bytes) -> EmailMark | None:
    """A logo, made fit to ride inside every message: a PNG at twice the size
    the header draws it, so a 160 KB upload becomes a few KB. `None` when the
    bytes will not decode. Cached on the bytes themselves, so a run of a
    hundred rent notices resizes the logo once."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            if not width or not height:
                return None
            wide = width / height >= 2
            box_w, box_h = _WIDE_BOX if wide else _SQUARE_BOX
            scale = min(box_w / width, box_h / height)
            shown = (max(1, round(width * scale)), max(1, round(height * scale)))
            # Twice the shown size for a high-density screen — but never
            # larger than the upload itself, which would only blur it.
            pixels = (min(width, shown[0] * 2), min(height, shown[1] * 2))
            resized = image.convert("RGBA").resize(pixels, Image.Resampling.LANCZOS)
            out = io.BytesIO()
            resized.save(out, format="PNG", optimize=True)
    except Exception:  # noqa: BLE001 - see the docstring: no logo, not no email
        log.exception("could not prepare the platform logo for email")
        return None
    return EmailMark(data=out.getvalue(), width=shown[0], height=shown[1], wide=wide)


async def email_mark(session: AsyncSession) -> EmailMark | None:
    """The logo for an email's header: the uploaded one, else the built-in
    one — and the built-in one too when the upload will not decode. `None`
    only when neither can be drawn, and the header falls back to the
    wordmark."""
    data = await logo_bytes(session)
    mark = prepare_email_mark(data) if data else None
    if mark is None and data is not None and data is not default_logo():
        builtin = default_logo()
        mark = prepare_email_mark(builtin) if builtin else None
    return mark
