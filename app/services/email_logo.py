"""A clinic's logo, made fit for the header of an email it sends.

Same bytes the clinic's PDFs draw (`tenants.logo_for`), same technique as the
platform's own mark (`platform_logo.prepare_email_mark`): resized to twice the
size the header shows it, so a 2 MB upload rides along as a few KB, and carried
inside the message as an inline part.

What differs is that a clinic's logo is whatever the clinic uploaded. The
header is dark slate; a logo drawn in dark ink on transparency would vanish on
it. So the artwork is looked at once, here: light artwork on transparency (made
for a dark ground) is drawn as it is, anything else goes on a small light
rounded tile (`EmailMark.tile`).

Every step is forgiving. A missing logo, a bucket that will not answer, bytes
that will not decode, an image past the dimension cap — each yields `None`, and
the header is the clinic's name alone. An invoice is never held back over its
decoration.
"""

from __future__ import annotations

import functools
import io
import logging

from app.services.mailer import CLINIC_MARK_CID, EmailMark
from app.services.platform_logo import bombs_refused, drawable

log = logging.getLogger("pms.brand")

#: The box a clinic's logo is drawn in, in CSS pixels. Wider than tall by two
#: to one or more: on its own line, the name under it. Otherwise beside the
#: name, in a square.
_WIDE_BOX = (132, 30)
_SQUARE_BOX = (34, 34)

#: Visible pixels (alpha above this) are the artwork; below it, background.
_VISIBLE = 40
#: Artwork lighter than this, on transparency, is made for a dark ground and is
#: drawn straight on the header; anything darker gets the tile.
_LIGHT_ARTWORK = 0.72


def _needs_tile(image) -> bool:
    """Whether a logo must sit on a light tile to be seen on the dark header.

    Opaque artwork (a JPEG, a PNG with its own background) brings its own
    ground and always gets the tile, so a white-backed logo does not show as a
    white box on slate. Transparent artwork gets it unless the visible part of
    it is light."""
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    if alpha.getextrema()[0] >= 250:
        return True
    total = weighted = 0.0
    for (r, g, b, a) in rgba.getdata():
        if a <= _VISIBLE:
            continue
        total += a
        weighted += a * (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    if total == 0:
        return True
    return weighted / total < _LIGHT_ARTWORK


@functools.lru_cache(maxsize=32)
def prepare_clinic_mark(data: bytes) -> EmailMark | None:
    """A clinic's logo, made fit to ride inside an email — or `None` when the
    bytes will not decode or are past the dimension cap. Cached on the bytes."""
    from PIL import Image

    try:
        with bombs_refused(), Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if not drawable(width, height):
                return None
            image.load()
            wide = width / height >= 2
            box_w, box_h = _WIDE_BOX if wide else _SQUARE_BOX
            scale = min(box_w / width, box_h / height)
            shown = (max(1, round(width * scale)), max(1, round(height * scale)))
            # Twice the shown size for a high-density screen, never larger
            # than the upload itself, which would only blur it.
            pixels = (min(width, shown[0] * 2), min(height, shown[1] * 2))
            resized = image.convert("RGBA").resize(pixels, Image.Resampling.LANCZOS)
            tile = _needs_tile(resized)
            out = io.BytesIO()
            resized.save(out, format="PNG", optimize=True)
    except Exception:  # noqa: BLE001 - no logo, not no invoice
        log.exception("could not prepare the clinic's logo for email")
        return None
    return EmailMark(
        data=out.getvalue(), width=shown[0], height=shown[1], wide=wide,
        cid=CLINIC_MARK_CID, filename="logo.png", tile=tile,
    )


def clinic_mark(logo: bytes | None) -> EmailMark | None:
    """The mark for a clinic's email from the logo bytes the clinic's PDFs use
    (`tenants.logo_for`), or `None` for a clinic with none, or one that cannot
    be drawn."""
    return prepare_clinic_mark(logo) if logo else None

