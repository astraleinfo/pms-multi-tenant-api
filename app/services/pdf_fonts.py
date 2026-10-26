"""The typeface every PDF is printed in.

reportlab's built-in Helvetica has no rupee sign and nothing outside Latin-1,
so amounts used to print as "INR 1,250.00" and a name in another script as
blank boxes. This registers Noto Sans (SIL OFL, bundled under
`app/assets/fonts/`) once, at import — it is the only place fonts are
registered, so a style that wants the document font imports `FONT` /
`FONT_BOLD` from here and never names a face itself.

Noto Sans covers Latin with accents, Greek, Cyrillic and the currency symbols.
The bundled build also holds glyphs for Indic, Arabic and Hebrew letters, but
reportlab draws characters one at a time in logical order — it neither joins
nor reorders them — so those scripts would print with their vowel signs in
the wrong place. A misspelt name on a medical record is worse than an honest
placeholder, so `has_glyph` calls them unsupported and `glyph_safe` swaps each
character that cannot be drawn faithfully for "?".
"""

from __future__ import annotations

from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONT = "NotoSans"
FONT_BOLD = "NotoSans-Bold"

#: The symbol a currency prints with. A code not listed here prints as its ISO code.
CURRENCY_SYMBOLS: dict[str, str] = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}

_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

_regular = TTFont(FONT, str(_DIR / "NotoSans-Regular.ttf"))
pdfmetrics.registerFont(_regular)
pdfmetrics.registerFont(TTFont(FONT_BOLD, str(_DIR / "NotoSans-Bold.ttf")))
# Without the family, `<b>` and `<i>` inside a Paragraph have no face to switch to.
pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT, boldItalic=FONT_BOLD)

#: line breaks and tabs are layout, not glyphs — they pass through untouched
_LAYOUT = "\n\r\t"

#: Code-point ranges whose letters need shaping to read correctly: Hebrew, Arabic,
#: Syriac, Thaana, the Indic scripts, Thai, Lao, Tibetan, Myanmar (0590–109F), Khmer
#: (1780–17FF), and the Hebrew/Arabic presentation forms.
_NEEDS_SHAPING = ((0x0590, 0x109F), (0x1780, 0x17FF), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF))


def has_glyph(ch: str) -> bool:
    """Whether the document font can draw this one character faithfully."""
    code = ord(ch)
    if any(low <= code <= high for low, high in _NEEDS_SHAPING):
        return False
    return code in _regular.face.charToGlyph


def glyph_safe(text: str) -> str:
    """`text` with every character the font cannot draw replaced by "?". Never raises."""
    if not text:
        return ""
    return "".join(ch if ch in _LAYOUT or has_glyph(ch) else "?" for ch in str(text))
