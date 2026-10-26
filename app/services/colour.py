"""Small colour arithmetic for the documents a clinic prints in its own colour.

A clinic picks one brand colour; the branded PDFs lay white text on it, so a
colour is only acceptable when white is readable on it (WCAG's 4.5:1 for body
text). Everything else the page needs — the pale wash behind a card, a darker
step for a rule — is derived from that one colour here, never chosen by hand.

Pure functions on `#rrggbb` strings; no imports from the rest of the app, so
the schemas and the PDF code can both use it.
"""

from __future__ import annotations

import re

#: What a clinic that never chose a colour prints in.
DEFAULT_BRAND = "#0f766e"

#: The least contrast against white a brand colour may have.
MIN_CONTRAST = 4.5

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def parse_hex(value: str) -> tuple[int, int, int]:
    """`#rrggbb` as (r, g, b). Raises `ValueError` for anything else."""
    if not isinstance(value, str) or not _HEX.match(value):
        raise ValueError("a colour is written #rrggbb")
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def _to_hex(rgb: tuple[float, float, float]) -> str:
    r, g, b = (max(0, min(255, round(c))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _linear(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def contrast_with_white(value: str) -> float:
    """The WCAG contrast ratio of white text on this colour: 1 (none) to 21 (black)."""
    r, g, b = parse_hex(value)
    luminance = 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)
    return 1.05 / (luminance + 0.05)


def tint(value: str, keep: float) -> str:
    """The colour washed out towards white. `keep` is the share of the colour
    that stays: 1 is the colour itself, 0 is white."""
    r, g, b = parse_hex(value)
    return _to_hex(tuple(c * keep + 255 * (1 - keep) for c in (r, g, b)))


def darken(value: str, amount: float) -> str:
    """The colour pulled towards black by `amount` (0 leaves it, 1 is black)."""
    r, g, b = parse_hex(value)
    return _to_hex(tuple(c * (1 - amount) for c in (r, g, b)))
