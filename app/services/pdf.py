"""What every printed document shares, and the patient record.

Pure-Python via reportlab — no system/native dependencies, so it runs
anywhere the API itself runs. The page furniture here — palette, type,
letterheads, footer, the escaping every typed string goes through — is used by
the consultation (`consultation_pdf.py`), the consent form, the invoices and
the practice report; `build_patient_record_pdf` is the one document built here.

Every table cell holds a `Paragraph`, never a raw string — a raw string in a
reportlab `Table` does not wrap, so a value longer than its column silently
overruns into its neighbour. Wrapping fixes that regardless of how long a
clinician's free-text turns out to be.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from reportlab.graphics.shapes import Circle, Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Frame,
    HRFlowable,
    Image,
    PageTemplate,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.core.brand import PLATFORM_NAME, PRODUCT_NAME
from app.models.clinical import Patient
from app.models.tenancy import TenantConfig
from app.schemas.common import local_now, stamp
from app.services.colour import DEFAULT_BRAND, MIN_CONTRAST, contrast_with_white, darken, parse_hex, tint
from app.services.pdf_fonts import FONT, FONT_BOLD, glyph_safe

log = logging.getLogger("pms.pdf")

# --- palette -----------------------------------------------------------------
# One accent, used sparingly — a document meant to be printed and filed, not a
# marketing piece. Mirrors the product's own "royal blue on cool white" mark.
_INK = colors.HexColor("#14181f")
_INK_2 = colors.HexColor("#4b5563")
_INK_3 = colors.HexColor("#8792a2")
_LINE = colors.HexColor("#d8dde5")
_ACCENT = colors.HexColor("#2b4fd8")
_ACCENT_SOFT = colors.HexColor("#eef1fc")
_ALERT_BG = colors.HexColor("#fdecea")
_ALERT_INK = colors.HexColor("#8a1f11")
_ROW_ALT = colors.HexColor("#f7f8fa")
_HEADER_BG = colors.HexColor("#f0f2f6")

_STYLES = getSampleStyleSheet()
# Every other style derives from "Normal", so repointing it here is what puts the
# whole document — including a style that never names a face — in the embedded font.
_STYLES["Normal"].fontName = FONT

_CLINIC = ParagraphStyle(
    "ClinicName", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=13.5,
    textColor=_INK, leading=16,
)
_EYEBROW = ParagraphStyle(
    "Eyebrow", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=8,
    textColor=_ACCENT, leading=10, spaceAfter=1,
)
_TITLE = ParagraphStyle(
    "ConsultationTitle", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=17,
    textColor=_INK, leading=20,
)
_DATE_RIGHT = ParagraphStyle(
    "DateRight", parent=_STYLES["Normal"], fontSize=9.5, textColor=_INK_2,
    alignment=2, leading=13,
)
_CLINIC_META = ParagraphStyle(
    "ClinicMeta", parent=_STYLES["Normal"], fontSize=8, textColor=_INK_2, leading=11.5,
    spaceBefore=2,
)
_SECTION = ParagraphStyle(
    "SectionHeading", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=9,
    textColor=_ACCENT, spaceBefore=14, spaceAfter=6, leading=11,
    keepWithNext=1,  # a heading is never the last line on a page
)
_BODY = ParagraphStyle("Body", parent=_STYLES["Normal"], fontSize=9.5, leading=13, textColor=_INK)
_BODY_SM = ParagraphStyle("BodySmall", parent=_STYLES["Normal"], fontSize=8.5, leading=12, textColor=_INK)
_LABEL = ParagraphStyle(
    "Label", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=6.9,
    textColor=_INK_3, leading=9, spaceAfter=1,
)
_ALERT = ParagraphStyle(
    "Allergies", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=9.5,
    leading=13, textColor=_ALERT_INK,
)
_TH = ParagraphStyle(
    "TableHead", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=7.6,
    textColor=_INK_2, leading=10,
)
_TD = ParagraphStyle("TableCell", parent=_STYLES["Normal"], fontSize=8.6, leading=11.5, textColor=_INK)
_FOOT = ParagraphStyle("Footer", parent=_STYLES["Normal"], fontSize=7.5, textColor=_INK_3, leading=10)


def _age_years(dob: date, on: date) -> int:
    years = on.year - dob.year
    if (on.month, on.day) < (dob.month, dob.day):
        years -= 1
    return max(years, 0)


def _field(label: str, value: str) -> Table:
    """One label-over-value pair, laid out as a tiny two-row table so the
    label never wraps away from its value and long values wrap in place."""
    t = Table(
        [[Paragraph(label.upper(), _LABEL)], [Paragraph(value or "—", _BODY)]],
        colWidths=[None],
    )
    t.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    return t


def _letterhead_lines(config: TenantConfig | None, *, hours: bool = True) -> list[str]:
    """Address, phone/email, and (where a patient would want them) hours — each
    one left out when the clinic has not set it, rather than printing a label
    with nothing after it."""
    if config is None:
        return []
    lines: list[str] = []
    if config.address:
        lines.extend(part.strip() for part in config.address.splitlines() if part.strip())
    contact = " · ".join(p for p in (config.contact_phone, config.contact_email) if p)
    if contact:
        lines.append(contact)
    if hours and config.day_start and config.day_end:
        lines.append(f"Hours {config.day_start}–{config.day_end}")
    return lines


def _weight_display(patient: Patient) -> str:
    """`clinical_ext["weight"]` is a plain float once validated against the
    schema (§3.3) — never a raw request body, so no further checking here."""
    weight = (patient.clinical_ext or {}).get("weight")
    if not isinstance(weight, (int, float)):
        return "—"
    return f"{weight:g} kg"


# --- the page every document is printed on -------------------------------------
# One letterhead and one footer for everything a clinic prints, so a consultation,
# an invoice and a report filed together read as one practice's paperwork.

PAGE_MARGINS = {"topMargin": 16 * mm, "bottomMargin": 22 * mm, "leftMargin": 18 * mm, "rightMargin": 18 * mm}

_KIND = ParagraphStyle(
    "DocKind", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=7.6,
    textColor=_ACCENT, leading=10, alignment=2,
)
_REF = ParagraphStyle(
    "DocRef", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=11.5,
    textColor=_INK, leading=14, alignment=2,
)
_FACTS = ParagraphStyle(
    "DocFacts", parent=_STYLES["Normal"], fontSize=8, textColor=_INK_2, leading=11.5, alignment=2,
)


def esc(text: object) -> str:
    """Paragraph text is a mini-markup, so anything a person typed is escaped before it goes in.
    A character the document font cannot draw becomes "?" rather than an empty box."""
    plain = glyph_safe(str(text if text is not None else ""))
    return plain.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


#: where a branded document's first-page content starts, measured from the top edge — the
#: coloured band behind it runs to the page edge, so it needs far less margin than a form.
_BAND_TOP = 8 * mm


class _Document(SimpleDocTemplate):
    """A4 whose frame has no inner padding. reportlab's default frame pads by
    6pt, so a table `doc.width` wide overhangs the paragraphs on both sides —
    headings and tables then start at two different left edges.

    A branded document asks for a coloured band with `set_band`: it is painted
    across the top of page one, edge to edge, behind the header the document puts
    there; every later page gets a thin strip of the same colour."""

    _band: tuple[Theme, float] | None = None

    def set_band(self, theme: Theme, height: float) -> None:
        self._band = (theme, height)

    def _paint_first(self, canvas, doc) -> None:  # noqa: ANN001 - reportlab's signature
        if self._band is None:
            return
        theme, height = self._band
        width, top = self.pagesize
        canvas.saveState()
        canvas.setFillColor(theme.accent)
        canvas.rect(0, top - height, width, height, stroke=0, fill=1)
        canvas.setFillColor(theme.strip)
        canvas.rect(0, top - height - 1.4 * mm, width, 1.4 * mm, stroke=0, fill=1)
        canvas.restoreState()

    def _paint_later(self, canvas, doc) -> None:  # noqa: ANN001 - reportlab's signature
        if self._band is None:
            return
        width, top = self.pagesize
        canvas.saveState()
        canvas.setFillColor(self._band[0].accent)
        canvas.rect(0, top - 3.5 * mm, width, 3.5 * mm, stroke=0, fill=1)
        canvas.restoreState()

    def build(self, flowables, canvasmaker=pdf_canvas.Canvas):  # noqa: ANN001 - reportlab's signature
        self._calc()

        def frame(top: float) -> Frame:
            return Frame(
                self.leftMargin, self.bottomMargin, self.width, self.pagesize[1] - self.bottomMargin - top,
                id="normal", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
            )

        first_top = _BAND_TOP if self._band else self.topMargin
        # the names SimpleDocTemplate moves between: page one, then every later page
        self.addPageTemplates([
            PageTemplate(id="First", frames=[frame(first_top)], onPage=self._paint_first,
                         pagesize=self.pagesize),
            PageTemplate(id="Later", frames=[frame(self.topMargin)], onPage=self._paint_later,
                         pagesize=self.pagesize),
        ])
        BaseDocTemplate.build(self, flowables, canvasmaker=canvasmaker)


def document(buf: io.BytesIO, *, title: str, author: str) -> _Document:
    return _Document(buf, pagesize=A4, title=title, author=author, **PAGE_MARGINS)


def _initials(name: str) -> str:
    words = [w for w in (name or "").replace("&", " ").split() if w[:1].isalnum()]
    return ("".join(w[0] for w in words[:2]) or "C").upper()


def monogram(name: str, size: float = 15 * mm) -> Drawing:
    """The clinic's initials on a soft accent tile — the letterhead's mark for a
    clinic that has not uploaded a logo, so every letterhead has the same shape.
    Drawn, not embedded: a clinic without a logo still has no image in its PDFs."""
    d = Drawing(size, size)
    d.add(Rect(0, 0, size, size, rx=3.2 * mm, ry=3.2 * mm, fillColor=_ACCENT_SOFT, strokeColor=None))
    text = _initials(name)
    font = size * (0.36 if len(text) > 1 else 0.44)
    d.add(String(size / 2, size / 2 - font * 0.35, text, textAnchor="middle",
                 fontName=FONT_BOLD, fontSize=font, fillColor=_ACCENT))
    return d


def letterhead(
    doc: SimpleDocTemplate,
    *,
    clinic: str,
    config: TenantConfig | None,
    logo: bytes | None,
    kind: str,
    ref: str = "",
    facts: list[str] | None = None,
    hours: bool = False,
    gap: float = 12,
) -> list:
    """The top of every page one: the clinic on the left — its logo (or its
    initials), its name and how to reach it — and on the right what this paper
    is, its reference, and its dates. Closed by the accent rule."""
    mark = _logo_flowable(logo, size=15 * mm) or monogram(clinic)
    lines = _letterhead_lines(config, hours=hours)
    who: list = [Paragraph(esc(clinic or "Clinic"), _CLINIC)]
    if lines:
        who.append(Paragraph("<br/>".join(esc(line) for line in lines), _CLINIC_META))

    what: list = [Paragraph(esc(kind).upper(), _KIND)]
    if ref:
        what.append(Paragraph(esc(ref), _REF))
    if facts:
        what.append(Spacer(1, 2))
        what.append(Paragraph("<br/>".join(facts), _FACTS))

    mark_w = 19 * mm
    head = Table(
        [[mark, who, what]],
        colWidths=[mark_w, doc.width * 0.6 - mark_w, doc.width * 0.4],
    )
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (1, 0), (1, 0), 1),
    ]))
    return [head, Spacer(1, 9), HRFlowable(width="100%", thickness=1.4, color=_ACCENT), Spacer(1, gap)]


def allergy_band(doc: SimpleDocTemplate, text: str) -> Table:
    """The one thing on a clinical page that must not be missed: a red rule and
    a red wash, the full width of the page."""
    band = Table([[Paragraph(text, _ALERT)]], colWidths=[doc.width])
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _ALERT_BG),
        ("LINEBEFORE", (0, 0), (0, -1), 3, _ALERT_INK),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return band


def person(label: str) -> str:
    """A principal's label as a person reads it: "Hema Nair <admin@…>" → "Hema Nair"."""
    return label.split("<")[0].strip() or label


# --- the Form family -----------------------------------------------------------------
# The consultation and the consent form are forms: read closely, signed, and
# photocopied. Black ink on white — hierarchy comes from weight, boxes and rules,
# never from colour, so they read the same from a monochrome printer.

_BLACK = colors.black
_GRID_HEAD = colors.HexColor("#e5e5e5")

_FORM_NAME = ParagraphStyle(
    "FormClinicName", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=14.5, leading=17,
    textColor=_BLACK,
)
_FORM_META = ParagraphStyle(
    "FormClinicMeta", parent=_STYLES["Normal"], fontSize=7.8, leading=11, textColor=_INK_2, spaceBefore=2,
)
_FORM_TITLE = ParagraphStyle(
    "FormTitle", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=11, leading=14, alignment=1,
    textColor=_BLACK,
)
_FORM_SECTION = ParagraphStyle(
    "FormSection", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=8.6, leading=11, textColor=_BLACK,
)
_FORM_CELL = ParagraphStyle(
    "FormCell", parent=_STYLES["Normal"], fontSize=9, leading=12.4, textColor=_BLACK,
)
_FORM_ALERT = ParagraphStyle(
    "FormAlert", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=9.4, leading=12.8, textColor=_BLACK,
)


def identity_lines(config: TenantConfig | None, *, gstin: bool = False) -> list[str]:
    """The clinic's registration number, and its GSTIN where the document is a bill —
    each left out when the clinic has not set it."""
    if config is None:
        return []
    lines: list[str] = []
    if (config.registration_no or "").strip():
        lines.append(f"Clinic Reg. No. {config.registration_no.strip()}")
    if gstin and (config.gstin or "").strip():
        lines.append(f"GSTIN {config.gstin.strip()}")
    return lines


def form_monogram(name: str, size: float = 15 * mm) -> Drawing:
    """The clinic's initials in a black ring — the Form header's mark for a clinic
    with no logo of its own, so the header is the same shape either way."""
    d = Drawing(size, size)
    d.add(Circle(size / 2, size / 2, size / 2 - 1, fillColor=colors.white,
                 strokeColor=_BLACK, strokeWidth=1.8))
    text = _initials(name)
    font = size * (0.34 if len(text) > 1 else 0.42)
    d.add(String(size / 2, size / 2 - font * 0.35, text, textAnchor="middle",
                 fontName=FONT_BOLD, fontSize=font, fillColor=_BLACK))
    return d


def form_header(
    doc: SimpleDocTemplate,
    *,
    clinic: str,
    config: TenantConfig | None,
    logo: bytes | None,
    title: str,
    gstin: bool = False,
) -> list:
    """The top of a form's first page: the clinic's logo (or initials), its name
    in capitals and how to reach it, a heavy rule, then what this paper is,
    centred. Lines the clinic has not filled in are left out."""
    mark = _logo_flowable(logo, size=15 * mm) or form_monogram(clinic)
    lines = [*_letterhead_lines(config, hours=True), *identity_lines(config, gstin=gstin)]
    who: list = [Paragraph(esc(clinic or "Clinic").upper(), _FORM_NAME)]
    if lines:
        who.append(Paragraph("<br/>".join(esc(line) for line in lines), _FORM_META))
    mark_w = 19 * mm
    head = Table([[mark, who]], colWidths=[mark_w, doc.width - mark_w])
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return [
        head, Spacer(1, 7), HRFlowable(width="100%", thickness=2.2, color=_BLACK), Spacer(1, 8),
        Paragraph(esc(title).upper(), _FORM_TITLE), Spacer(1, 9),
    ]


def info_box(doc: SimpleDocTemplate, items: list[tuple[str, object]]) -> Table:
    """A bordered box of `Label: value` pairs in two columns. A pair whose value is
    empty is dropped and the rest close up — a form never prints a label with
    nothing after it."""
    cells = [
        Paragraph(f"<b>{esc(label)}:</b> {esc(value)}", _FORM_CELL)
        for label, value in items
        if value is not None and str(value).strip()
    ]
    if len(cells) % 2:
        cells.append("")
    rows = [cells[i:i + 2] for i in range(0, len(cells), 2)] or [["", ""]]
    box = Table(rows, colWidths=[doc.width / 2] * 2)
    box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, _BLACK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    return box


def section_title(doc: SimpleDocTemplate, text: str) -> Table:
    """A form's section heading: bold capitals over a rule. Always kept with what follows."""
    title = Table([[Paragraph(esc(text).upper(), _FORM_SECTION)]], colWidths=[doc.width],
                  spaceBefore=11, spaceAfter=4)
    title.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.9, _BLACK),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    title.keepWithNext = 1
    return title


# --- the Branded family ----------------------------------------------------------------
# Bills and reports: documents of tables and figures, which a clinic puts its own
# colour on. A coloured band carries the clinic's identity across the top of page one
# and a matching strip closes every page. Whatever the colour, nothing is told by it
# alone — a status is always spelled out in words.


@dataclass(frozen=True)
class Theme:
    """One brand colour and the steps derived from it: the band, a darker step for text
    on a pale ground, a pale wash for cards and table heads, a paler one still, and the
    strip under the band."""

    accent: colors.Color
    accent_dark: colors.Color
    tint: colors.Color
    tint_soft: colors.Color
    strip: colors.Color


def _theme_from(hex_colour: str) -> Theme:
    return Theme(
        accent=colors.HexColor(hex_colour),
        accent_dark=colors.HexColor(darken(hex_colour, 0.28)),
        tint=colors.HexColor(tint(hex_colour, 0.16)),
        tint_soft=colors.HexColor(tint(hex_colour, 0.07)),
        strip=colors.HexColor(tint(hex_colour, 0.45)),
    )


def theme_for(config: TenantConfig | None) -> Theme:
    """The clinic's theme. A row that holds no colour, one that is not a colour, or one white
    text cannot be read on (anything stale or hand-edited — the API refuses to store it) gets
    the product's default; a document is never refused for its decoration."""
    raw = getattr(config, "brand_color", None) or DEFAULT_BRAND
    try:
        parse_hex(raw)
        if contrast_with_white(raw) < MIN_CONTRAST:
            raw = DEFAULT_BRAND
    except ValueError:
        raw = DEFAULT_BRAND
    return _theme_from(raw)


#: What the platform's own documents (the subscription bill) are printed in.
PLATFORM_THEME = _theme_from("#2b4fd8")

_BAND_NAME = ParagraphStyle(
    "BandClinicName", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=14.5, leading=17,
    textColor=colors.white,
)
_BAND_TITLE = ParagraphStyle(
    "BandTitle", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=19, leading=22,
    textColor=colors.white, alignment=2,
)
_BAND_REF = ParagraphStyle(
    "BandRef", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=10.5, leading=13,
    textColor=colors.white, alignment=2,
)


def band_monogram(name: str, theme: Theme, size: float = 15 * mm) -> Drawing:
    """The clinic's initials on a white tile — the band's mark for a clinic with no logo."""
    d = Drawing(size, size)
    d.add(Rect(0, 0, size, size, rx=3 * mm, ry=3 * mm, fillColor=colors.white, strokeColor=None))
    text = _initials(name)
    font = size * (0.36 if len(text) > 1 else 0.44)
    d.add(String(size / 2, size / 2 - font * 0.35, text, textAnchor="middle",
                 fontName=FONT_BOLD, fontSize=font, fillColor=theme.accent))
    return d


def band_header(
    doc: _Document,
    *,
    theme: Theme,
    clinic: str,
    config: TenantConfig | None,
    logo: bytes | None,
    title: str,
    reference: str = "",
    facts: Sequence[str] = (),
    gstin: bool = True,
) -> list:
    """The top of a branded document's first page, over the band the document paints behind
    it: the clinic's mark, name, address, contact and registration lines on the left in white,
    what this paper is and its number on the right. Lines the clinic has not filled in are
    left out. `doc` is told how tall the band must be."""
    size = 15 * mm
    mark = _logo_flowable(logo, size=size - 3 * mm)
    if mark is not None:
        tile = Table([[mark]], colWidths=[size], rowHeights=[size])
        tile.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.white), ("ROUNDEDCORNERS", [3 * mm] * 4),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        mark = tile
    else:
        mark = band_monogram(clinic, theme, size)

    meta = ParagraphStyle("BandMeta", parent=_FORM_META, textColor=theme.tint, spaceBefore=2)
    lines = [*_letterhead_lines(config, hours=False), *identity_lines(config, gstin=gstin)]
    who: list = [Paragraph(esc(clinic or "Clinic"), _BAND_NAME)]
    if lines:
        who.append(Paragraph("<br/>".join(esc(line) for line in lines), meta))
    what: list = [Paragraph(esc(title).upper(), _BAND_TITLE)]
    if reference:
        what.append(Paragraph(esc(reference), _BAND_REF))
    for fact in facts:
        what.append(Paragraph(esc(fact), ParagraphStyle("BandFact", parent=meta, alignment=2)))

    mark_w = size + 4 * mm
    head = Table([[mark, who, what]], colWidths=[mark_w, doc.width * 0.58 - mark_w, doc.width * 0.42])
    return _on_band(doc, theme, head)


def _on_band(doc: _Document, theme: Theme, head: Table) -> list:
    """Set a branded header on its band: the band is sized to the header, and
    the story picks up below the strip."""
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    _, head_height = head.wrap(doc.width, 10_000)
    pad = 7 * mm
    doc.set_band(theme, _BAND_TOP + head_height + pad)
    return [head, Spacer(1, pad + 1.4 * mm + 6 * mm)]


def _platform_logo_flowable(logo: bytes | None) -> tuple[Image, bool] | None:
    """The platform's logo for its band, and whether it is a lockup (at least
    twice as wide as tall, so it spells the name itself). Drawn straight on the
    band, not on a white tile like a clinic's: the platform's logo is made for
    a dark ground. `None` when there is none or it will not decode."""
    if not logo:
        return None
    try:
        width, height = ImageReader(io.BytesIO(logo)).getSize()
        wide = width >= 2 * height
        box = (55 * mm, 9 * mm) if wide else (15 * mm, 15 * mm)
        image = Image(io.BytesIO(logo), width=box[0], height=box[1], kind="proportional")
    except Exception:  # noqa: BLE001 - the bill still prints, with the wordmark
        log.exception("could not draw the platform logo; printing the wordmark")
        return None
    image.hAlign = "LEFT"
    return image, wide


def platform_band_header(doc: _Document, *, logo: bytes | None, title: str, reference: str = "") -> list:
    """The top of a document the platform itself issues (the subscription bill),
    in the platform's colours: its logo — the one set in the console, else the
    built-in one — with what the product is under it, and on the right what
    this paper is. A lockup stands alone over the product line; a mark alone,
    or no usable logo at all, sits beside the name the way a clinic's does."""
    theme = PLATFORM_THEME
    meta = ParagraphStyle("BandMeta", parent=_FORM_META, textColor=theme.tint, spaceBefore=2)
    what: list = [Paragraph(esc(title).upper(), _BAND_TITLE)]
    if reference:
        what.append(Paragraph(esc(reference), _BAND_REF))

    left = doc.width * 0.58
    drawn = _platform_logo_flowable(logo)
    if drawn is not None and drawn[1]:
        who: list = [drawn[0], Spacer(1, 2.2 * mm), Paragraph(esc(PRODUCT_NAME), meta)]
        head = Table([[who, what]], colWidths=[left, doc.width * 0.42])
    else:
        size = 15 * mm
        mark = drawn[0] if drawn is not None else band_monogram(PLATFORM_NAME, theme, size)
        who = [Paragraph(esc(PLATFORM_NAME), _BAND_NAME), Paragraph(esc(PRODUCT_NAME), meta)]
        mark_w = size + 4 * mm
        head = Table([[mark, who, what]], colWidths=[mark_w, left - mark_w, doc.width * 0.42])
    return _on_band(doc, theme, head)


def allergy_box(doc: SimpleDocTemplate, allergies: str) -> Table:
    """The one thing on a clinical form that must not be missed: a heavy black box
    — it stands out on a photocopy, where a colour wash would not."""
    box = Table([[Paragraph(f"ALLERGIES: {esc(allergies)}", _FORM_ALERT)]], colWidths=[doc.width])
    box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.8, _BLACK),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return box


def paged(note: str, *, confidential: bool = False, band: colors.Color | None = None):
    """A canvas that knows how many pages there are, so the footer can say
    "Page 2 of 3" — paper gets separated, and a reader should know when a page
    is missing. `note` names the document on every page. With a `band` colour the
    footer is a filled strip with the note and the page number in white, the
    branded documents' close; without one it is the form's rule and fine print."""
    note = glyph_safe(note)  # drawn straight on the canvas, so it skips `esc`
    fine_print = "Confidential medical information" if confidential else ""

    class _Paged(pdf_canvas.Canvas):
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003 - reportlab's signature
            super().__init__(*args, **kwargs)
            self._pages: list[dict] = []

        def showPage(self) -> None:  # noqa: N802 - reportlab's name
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._footer(total)
                super().showPage()
            super().save()

        def _footer(self, total: int) -> None:
            width = self._pagesize[0]
            left, right = PAGE_MARGINS["leftMargin"], width - PAGE_MARGINS["rightMargin"]
            y = 13 * mm
            self.saveState()
            if band is not None:
                self.setFillColor(band)
                self.rect(0, 0, width, 11 * mm, stroke=0, fill=1)
                self.setFillColor(colors.white)
                self.setFont(FONT, 7.4)
                self.drawString(left, 4.2 * mm, note)
                self.drawRightString(right, 4.2 * mm, f"Page {self._pageNumber} of {total}")
                self.restoreState()
                return
            self.setStrokeColor(_LINE)
            self.setLineWidth(0.5)
            self.line(left, y + 9, right, y + 9)
            self.setFont(FONT, 7.3)
            self.setFillColor(_INK_3)
            self.drawString(left, y, note)
            if fine_print:
                self.drawCentredString((left + right) / 2, y - 9, fine_print)
            self.drawRightString(right, y, f"Page {self._pageNumber} of {total}")
            self.restoreState()

    return _Paged


def _logo_flowable(logo: bytes | None, size: float = 18 * mm) -> Image | None:
    """The clinic's mark, scaled to fit a square (18mm unless told) without distorting it
    — or `None` when there isn't one or the bytes turn out not to decode,
    which prints the letterhead exactly as it did before logos existed
    rather than failing the whole document over a bad image."""
    if not logo:
        return None
    try:
        return Image(io.BytesIO(logo), width=size, height=size, kind="proportional")
    except Exception:  # noqa: BLE001 - the document still prints, see docstring
        # Uploads are checked to be an image, so this is not the usual case —
        # it is a decoder missing a format, and then every PDF loses its logo.
        log.exception("could not draw the clinic logo; printing without it")
        return None


# --- the patient record ---------------------------------------------------------

_NAME = ParagraphStyle(
    "PatientName", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=18,
    textColor=_INK, leading=22,
)
_SUB = ParagraphStyle("PatientSub", parent=_STYLES["Normal"], fontSize=9.5, textColor=_INK_2, leading=13)
_OK_LINE = ParagraphStyle(
    "NoAllergies", parent=_STYLES["Normal"], fontSize=8.6, textColor=colors.HexColor("#1f7a4d"), leading=11,
)
_MUTED = ParagraphStyle("Muted", parent=_TD, textColor=_INK_3)
_TD_BOLD = ParagraphStyle("CellBold", parent=_TD, fontName=FONT_BOLD)

#: how many rows of each history a printed record carries before it says "and N earlier"
_RECENT = 12

_NO_ALLERGIES = {"", "none", "none recorded", "nil", "nkda", "no known allergies"}


def _fmt(d: date | None) -> str:
    return d.strftime("%d %b %Y") if d else "—"


def _status(value: object) -> str:
    text = str(getattr(value, "value", value) or "")
    return text.replace("_", " ").capitalize() or "—"


def _display(value: object) -> str:
    """A custom field's value as a person would write it on paper."""
    if value is None or value == "" or value == []:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "—"
    try:
        return _fmt(date.fromisoformat(str(value)))
    except ValueError:
        return str(value)


def _data_table(rows: list[list], widths: list[float]) -> Table:
    """Hairline rows under a quiet header — a ledger, not a spreadsheet."""
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, _LINE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, _LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


#: The record's headings sit over tables that can run to a page or more, so
#: they are not glued to the whole table (`keepWithNext` moves the lot to the
#: next page and leaves half a page blank). The break before them makes sure a
#: heading lands with at least its column heads and a row or two under it, and
#: the table then carries on over the page, its header repeated.
_RECORD_SECTION = ParagraphStyle("RecordSection", parent=_SECTION, keepWithNext=0)
_SECTION_ROOM = 72


def _section(title: str, count: int | None = None) -> list:
    suffix = f'  <font color="#8792a2">{count}</font>' if count else ""
    return [CondPageBreak(_SECTION_ROOM), Paragraph(f"{esc(title).upper()}{suffix}", _RECORD_SECTION)]


def _more(shown: int, total: int, what: str) -> list:
    if total <= shown:
        return []
    return [Spacer(1, 3), Paragraph(f"…and {total - shown} earlier {what}, not printed here.", _FOOT)]


#: a note's author by the role they wrote it in, as the clinic would say it
_ROLE_WORD = {
    "doctor": "Clinician", "tenant_admin": "Practice admin", "receptionist": "Reception",
    "pharmacist": "Pharmacist", "lab_tech": "Lab technician",
}


def _when(value, tz: str | None) -> str:
    """`2026-10-08 14:05` → `08 Oct 2026, 14:05`."""
    text = stamp(value, tz) or ""
    try:
        return f"{_fmt(date.fromisoformat(text[:10]))}, {text[11:16]}"
    except ValueError:
        return text or "—"


def _notes_table(doc, notes: list, tz: str | None) -> list:
    """Newest first, as the record shows them. A correction says which note it
    corrects, and a corrected note says it was corrected — both are printed,
    because the paper is the record too."""
    by_id = {n.id: n for n in notes}
    corrected = {n.amends_id for n in notes if n.amends_id}
    rows = [[Paragraph(h, _TH) for h in ("Written", "Note")]]
    for n in notes[:_RECENT]:
        role = _ROLE_WORD.get(n.author_role, "")
        written = (
            f"<b>{esc(_when(n.created_at, tz))}</b><br/>{esc(n.author_label) or '—'}"
            + (f"<br/><font color='#8792a2'>{esc(role)}</font>" if role else "")
        )
        body = esc(n.body).replace("\n", "<br/>")
        if n.amends_id:
            earlier = by_id.get(n.amends_id)
            what = f"the note of {_when(earlier.created_at, tz)}" if earlier else "an earlier note"
            body = f"<font color='#8792a2'>Correction to {esc(what)}</font><br/>{body}"
        if n.id in corrected:
            body += "<br/><font color='#8792a2'>Corrected later — the correction is printed above.</font>"
        rows.append([Paragraph(written, _TD), Paragraph(body, _TD)])
    return [
        _data_table(rows, [doc.width * w for w in (0.26, 0.74)]),
        *_more(_RECENT, len(notes), "notes"),
    ]


def build_patient_record_pdf(
    patient: Patient,
    *,
    clinic: str,
    config: TenantConfig | None,
    logo: bytes | None,
    fields: list[tuple[str, object]],
    encounters: list,
    prescriptions: list,
    lab_orders: list | None,
    appointments: list,
    names: dict,
    today: date,
    tz: str | None,
    printed_by: str,
    notes: list | None = None,
) -> bytes:
    """One patient's file as it stands today — who they are, what this clinic
    knows about them, and what has happened since they registered.

    It is a clinical document: billing is not part of it, and is printed on its
    own invoices. `lab_orders` is `None` when the reader's role may not see the
    lab, and then the section is left out rather than printed empty — an empty
    section would read as "nothing ordered". `fields` are the clinic's
    own patient fields, already labelled and in order. `notes` follows the
    same rule as the lab: `None` for a role that may not read them, and the
    section is left out.
    """
    buf = io.BytesIO()
    doc = document(buf, title=f"Patient record — {patient.full_name}", author=clinic or "Clinic")
    who = lambda uid: esc(names.get(uid, "—"))  # noqa: E731 - a one-line lookup

    story: list = letterhead(
        doc, clinic=clinic, config=config, logo=logo,
        kind="Patient record", ref=patient.mrn,
        facts=[
            f"Printed {local_now(tz).strftime('%d %b %Y, %H:%M')}",
            # the person, not their sign-in: "Hema Nair <admin@…>" → "Hema Nair"
            f"by {esc(person(printed_by))}",
        ],
        gap=6,
    )

    # --- who --------------------------------------------------------------------
    facts = [
        f"{_age_years(patient.dob, today)} years",
        _status(patient.sex),
        f"Blood group {esc(patient.blood_group)}" if patient.blood_group else "",
        f"Registered {_fmt(patient.registered_at)}",
    ]
    story.append(Paragraph(esc(patient.full_name), _NAME))
    story.append(Spacer(1, 2))
    story.append(Paragraph(" · ".join(f for f in facts if f), _SUB))
    story.append(Spacer(1, 9))

    allergies = (patient.allergies or "").strip()
    if allergies.lower() not in _NO_ALLERGIES:
        story.append(allergy_band(doc, f"ALLERGIES — {esc(allergies)}"))
    else:
        story.append(Paragraph("No known allergies", _OK_LINE))

    # --- details: contact first, then the clinic's own fields ---------------------
    details: list[tuple[str, str]] = [
        ("Date of birth", _fmt(patient.dob)),
        ("Phone", patient.phone or "—"),
        ("Email", patient.email or "—"),
        ("Weight", _weight_display(patient)),
        *((label, _display(value)) for label, value in fields),
    ]
    details = [(k, v) for k, v in details if not (k == "Weight" and v == "—")]
    if patient.address:
        details.append(("Address", patient.address))
    story.extend(_section("Details"))
    per_row = 3
    cells = [_field(label, esc(value)) for label, value in details]
    while len(cells) % per_row:
        cells.append("")
    grid = Table(
        [cells[i:i + per_row] for i in range(0, len(cells), per_row)],
        colWidths=[doc.width / per_row] * per_row,
    )
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, _LINE),
    ]))
    story.append(grid)

    # --- upcoming -----------------------------------------------------------------
    if appointments:
        story.extend(_section("Upcoming appointments", len(appointments)))
        rows = [[Paragraph(h, _TH) for h in ("Date", "Time", "Clinician", "Reason")]]
        rows += [[
            Paragraph(_fmt(a.date), _TD_BOLD), Paragraph(esc(a.time), _TD),
            Paragraph(who(a.doctor_id), _TD), Paragraph(esc(a.reason) or "—", _TD),
        ] for a in appointments]
        story.append(_data_table(rows, [doc.width * w for w in (0.18, 0.12, 0.28, 0.42)]))

    # --- notes ---------------------------------------------------------------------
    if notes is not None:
        story.extend(_section("Clinical notes", len(notes)))
        if notes:
            story += _notes_table(doc, notes, tz)
        else:
            story.append(Paragraph("No notes written.", _MUTED))

    # --- consultations --------------------------------------------------------------
    story.extend(_section("Consultations", len(encounters)))
    if encounters:
        rows = [[Paragraph(h, _TH) for h in ("Date", "Clinician", "Complaint", "Diagnosis", "Status")]]
        for e in encounters[:_RECENT]:
            follow = (
                f"<br/><font name='{FONT}' color='#8792a2'>Follow-up in {e.follow_up_days} days</font>"
                if e.follow_up_days else ""
            )
            rows.append([
                Paragraph(_fmt(e.date), _TD), Paragraph(who(e.doctor_id), _TD),
                Paragraph(esc(e.chief_complaint) or "—", _TD),
                Paragraph((esc(e.diagnosis) or "—") + follow, _TD_BOLD if e.diagnosis else _TD),
                Paragraph(_status(e.status), _MUTED),
            ])
        story.append(_data_table(rows, [doc.width * w for w in (0.14, 0.19, 0.27, 0.28, 0.12)]))
        story += _more(_RECENT, len(encounters), "consultations")
    else:
        story.append(Paragraph("No consultations recorded.", _MUTED))

    # --- prescriptions ----------------------------------------------------------------
    if prescriptions:
        story.extend(_section("Prescriptions", len(prescriptions)))
        rows = [[Paragraph(h, _TH) for h in ("Issued", "Medicines", "Status")]]
        for rx in prescriptions[:_RECENT]:
            lines = "<br/>".join(
                f"<b>{esc(i.medicine)}</b> {esc(i.strength)}"
                + (f" — {esc(i.dosage)}" if i.dosage else "")
                + (f", {esc(i.frequency)}" if i.frequency else "")
                + (f", {esc(i.duration)}" if i.duration else "")
                for i in rx.items
            ) or "—"
            issued = rx.issued_at.date() if rx.issued_at else None
            rows.append([
                Paragraph(_fmt(issued), _TD), Paragraph(lines, _TD), Paragraph(_status(rx.status), _MUTED),
            ])
        story.append(_data_table(rows, [doc.width * w for w in (0.14, 0.68, 0.18)]))
        story += _more(_RECENT, len(prescriptions), "prescriptions")

    # --- lab ----------------------------------------------------------------------------
    if lab_orders:
        story.extend(_section("Lab orders", len(lab_orders)))
        rows = [[Paragraph(h, _TH) for h in ("Ordered", "Test", "Result", "Status")]]
        for order in lab_orders[:_RECENT]:
            verified = str(getattr(order.status, "value", order.status)) == "verified"
            if verified and order.results:
                result = "<br/>".join(
                    f"{esc(r.get('analyte'))}: <b>{esc(r.get('value'))}</b>"
                    + (f" {esc(r['unit'])}" if r.get("unit") else "")
                    + (f" <font color='#8a1f11'>({esc(r['flag'])})</font>"
                       if r.get("flag") and r.get("flag") != "normal" else "")
                    for r in order.results
                )
            else:
                result = "<font color='#8792a2'>Held until verified</font>" if not verified else "—"
            rows.append([
                Paragraph(_fmt(order.ordered_at.date() if order.ordered_at else None), _TD),
                Paragraph(f"<b>{esc(order.test_name)}</b>", _TD),
                Paragraph(result, _TD), Paragraph(_status(order.status), _MUTED),
            ])
        story.append(_data_table(rows, [doc.width * w for w in (0.14, 0.26, 0.44, 0.16)]))
        story += _more(_RECENT, len(lab_orders), "lab orders")

    story.append(Spacer(1, 16))
    story.append(Paragraph(
        f"A copy of {esc(patient.full_name)}'s record at {esc(clinic)}, as it stood when printed. "
        "It contains confidential medical information — keep it with the patient's file.",
        _FOOT,
    ))
    note = f"{clinic} · Patient record {patient.mrn} · {patient.full_name}"
    doc.build(story, canvasmaker=paged(note, confidential=True))
    return buf.getvalue()
