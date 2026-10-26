"""The consent document — the one layout behind preview, print and download.

A form is printed four ways from the same builder, so what an admin previews
is what a patient is handed:

- a **draft** — marked as a draft across the top, never to be signed;
- a **blank** published version — write-in lines for everything, for a
  clinic that takes consent on paper;
- a **pending** consent — the patient's details and the clinic's fields
  filled in, the signatures still to be made;
- a **signed** (or declined) consent — exactly as answered, with the drawn
  signatures, who witnessed them, and when.

Black ink first: hierarchy comes from weight, size and rules, so it reads the
same from a monochrome printer. The clinic's letterhead and the page footer are
the shared ones every document uses.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from reportlab.graphics.shapes import Drawing, PolyLine, Rect
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image, KeepTogether, Paragraph, Spacer, Table, TableStyle

from app.models.tenancy import TenantConfig
from app.schemas.common import zone
from app.schemas.consent import ConsentContent
from app.services.pdf import (
    _INK,
    _INK_2,
    _LINE,
    _STYLES,
    document,
    esc,
    form_header,
    paged,
)
from app.services.pdf_fonts import FONT_BOLD

log = logging.getLogger("pms.consent")

_H = ParagraphStyle(
    "ConsentHeading",
    parent=_STYLES["Normal"],
    fontName=FONT_BOLD,
    fontSize=10.5,
    leading=13,
    textColor=_INK,
    spaceBefore=13,
    spaceAfter=5,
    keepWithNext=1,
)
_P = ParagraphStyle("ConsentBody", parent=_STYLES["Normal"], fontSize=9.4, leading=13.4, textColor=_INK)
_BULLET = ParagraphStyle("ConsentBullet", parent=_P, leftIndent=11, bulletIndent=2, spaceBefore=1.5)
_LABEL = ParagraphStyle(
    "ConsentLabel",
    parent=_STYLES["Normal"],
    fontName=FONT_BOLD,
    fontSize=7,
    leading=9,
    textColor=_INK_2,
)
_VALUE = ParagraphStyle("ConsentValue", parent=_P, fontSize=9.6)
_SMALL = ParagraphStyle("ConsentSmall", parent=_P, fontSize=8, leading=11, textColor=_INK_2)
_FACTS = ParagraphStyle("ConsentFacts", parent=_SMALL, alignment=1)
_BAND = ParagraphStyle(
    "ConsentBand",
    parent=_STYLES["Normal"],
    fontName=FONT_BOLD,
    fontSize=8.6,
    leading=11,
    textColor=_INK,
)

_PATIENT_LABELS = {
    "full_name": "Patient name",
    "mrn": "Patient ID",
    "dob": "Date of birth",
    "sex": "Sex",
    "phone": "Phone",
    "email": "Email",
    "address": "Address",
}


@dataclass
class ConsentPrint:
    """What one printing of a consent document needs. `mode` decides how much
    of it is filled in; the rest stays as lines to write on."""

    content: ConsentContent
    version_number: int
    mode: str  # "draft" | "blank" | "pending" | "signed" | "declined" | "expired"
    patient: dict = field(default_factory=dict)
    field_values: dict = field(default_factory=dict)
    acknowledged: list[str] = field(default_factory=list)
    provider_name: str = ""
    requested_at: datetime | None = None
    signer_name: str = ""
    signer_relationship: str = ""
    signed_at: datetime | None = None
    witnessed_by: str = ""
    patient_signature: bytes | None = None
    provider_signature: bytes | None = None
    declined_at: datetime | None = None
    decline_reason: str = ""


def _when(value: datetime | None, tz: str | None, *, time: bool = True) -> str:
    if value is None:
        return ""
    local = value.astimezone(zone(tz)) if tz else value
    return local.strftime("%d %b %Y, %H:%M" if time else "%d %b %Y")


def _patient_value(key: str, snapshot: dict) -> str:
    value = snapshot.get(key)
    if not value:
        return ""
    if key == "dob":
        try:
            return date.fromisoformat(value).strftime("%d %b %Y")
        except ValueError:
            return str(value)
    if key == "sex":
        return str(value).capitalize()
    return str(value)


def _checkbox(checked: bool) -> Drawing:
    d = Drawing(9, 9)
    d.add(Rect(0, 0, 9, 9, strokeColor=_INK, strokeWidth=0.8, fillColor=None))
    if checked:
        d.add(PolyLine([1.8, 4.6, 3.8, 2.2, 7.4, 7.2], strokeColor=_INK, strokeWidth=1.3, fillColor=None))
    return d


def _paragraphs(body: str) -> list:
    """Plain text from the editor: blank lines separate paragraphs, "- " makes a bullet."""
    out: list = []
    for block in (body or "").strip().split("\n\n"):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        para: list[str] = []
        for line in lines:
            if line.startswith(("- ", "• ")):
                if para:
                    out.append(Paragraph(esc(" ".join(para)), _P))
                    para = []
                out.append(Paragraph(esc(line[2:]), _BULLET, bulletText="•"))
            else:
                para.append(line)
        if para:
            out.append(Paragraph(esc(" ".join(para)), _P))
        out.append(Spacer(1, 3))
    return out[:-1] if out else out


def _ruled(width: float, rows: list[list], *, line_under: bool = True) -> Table:
    table = Table(rows, colWidths=width, hAlign="LEFT")
    style = [
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if line_under:
        style.append(("LINEBELOW", (0, 0), (-1, -1), 0.5, _LINE))
    table.setStyle(TableStyle(style))
    return table


def _labelled(label: str, value: str, *, write_in: bool) -> list:
    """A label over its value — or over an empty line to write on."""
    return [
        Paragraph(esc(label).upper(), _LABEL),
        Paragraph(esc(value) if value else ("&nbsp;" if write_in else "—"), _VALUE),
    ]


def _signature_cell(png: bytes | None, *, height: float = 15 * mm) -> object:
    if png:
        try:
            return Image(io.BytesIO(png), width=55 * mm, height=height, kind="proportional", hAlign="LEFT")
        except Exception:  # noqa: BLE001 - a bad image still leaves the line to sign on
            log.exception("could not draw a consent signature; leaving the line blank")
    return Spacer(1, height)


def build_consent_pdf(
    doc_in: ConsentPrint,
    *,
    clinic: str,
    config: TenantConfig | None,
    logo: bytes | None,
    tz: str | None,
) -> bytes:
    content, mode = doc_in.content, doc_in.mode
    filled = mode not in ("draft", "blank")
    answered = mode in ("signed", "declined")

    buf = io.BytesIO()
    doc = document(buf, title=f"{content.title} — v{doc_in.version_number}", author=clinic or "Clinic")
    facts: list[str] = []
    if doc_in.requested_at and filled:
        facts.append(f"Requested {_when(doc_in.requested_at, tz, time=False)}")
    if doc_in.signed_at:
        facts.append(f"Signed {_when(doc_in.signed_at, tz)}")
    if not filled:
        facts.append("Blank form" if mode == "blank" else "Draft for review")
    story: list = form_header(doc, clinic=clinic, config=config, logo=logo, title=content.title)
    story += [
        Paragraph(esc(" · ".join(["Consent form", f"Version {doc_in.version_number}", *facts])), _FACTS),
        Spacer(1, 10),
    ]

    # --- what this paper is ----------------------------------------------------------
    band = {
        "draft": "DRAFT — not published. For review only; do not use with patients.",
        "declined": f"DECLINED {_when(doc_in.declined_at, tz)}"
        + (f" — {esc(doc_in.decline_reason)}" if doc_in.decline_reason else ""),
        "expired": "EXPIRED — this request lapsed unsigned. Request a new consent.",
    }.get(mode)
    if band:
        t = Table([[Paragraph(band, _BAND)]], colWidths=[doc.width])
        t.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 1, _INK),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story += [t, Spacer(1, 10)]

    # --- the patient -----------------------------------------------------------------
    per_row = 3
    cells = [
        _labelled(
            _PATIENT_LABELS[key], _patient_value(key, doc_in.patient) if filled else "", write_in=not filled
        )
        for key in content.patient_fields
    ]
    cells.append(
        _labelled(
            "Date",
            _when(doc_in.signed_at or doc_in.requested_at, tz, time=False) if filled else "",
            write_in=not filled,
        )
    )
    while len(cells) % per_row:
        cells.append("")
    story.append(_ruled(doc.width / per_row, [cells[i : i + per_row] for i in range(0, len(cells), per_row)]))

    if content.intro:
        story += [Spacer(1, 10), *_paragraphs(content.intro)]

    # --- the sections, numbered ---------------------------------------------------------
    for n, section in enumerate(content.sections, 1):
        block: list = [Paragraph(f"{n}.&nbsp;&nbsp;{esc(section.title)}", _H)]
        if section.body:
            block += _paragraphs(section.body)
        if section.kind == "fields":
            rows = []
            for f in section.fields:
                value = doc_in.field_values.get(f.id, "") if filled else ""
                rows.append(
                    [
                        _labelled(
                            f.label + (" *" if f.required and not filled else ""), value, write_in=not filled
                        )
                    ]
                )
                if f.multiline and not value:
                    rows.append([Paragraph("&nbsp;", _VALUE)])
            if section.body:
                block.append(Spacer(1, 4))
            block.append(_ruled(doc.width, rows))
        if section.kind == "acknowledgements":
            if section.body:
                block.append(Spacer(1, 4))
            rows = [
                [
                    _checkbox(answered and a.id in doc_in.acknowledged),
                    Paragraph(
                        esc(a.text) + (" <font color='#4b5563'>(required)</font>" if a.required else ""), _P
                    ),
                ]
                for a in section.statements
            ]
            t = Table(rows, colWidths=[7 * mm, doc.width - 7 * mm])
            t.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                        ("TOPPADDING", (0, 0), (0, -1), 4.5),
                    ]
                )
            )
            block.append(t)
        story.append(KeepTogether(block) if len(block) <= 4 else block[0])
        if len(block) > 4:
            story += block[1:]

    # --- the signatures -----------------------------------------------------------------
    sig: list = [
        Paragraph("Declaration", _H),
        Paragraph(esc(content.signatures.declaration), _P),
        Spacer(1, 12),
    ]
    half = doc.width / 2

    def signature_block(title: str, png: bytes | None, name: str, role: str, when: datetime | None) -> list:
        return [
            Paragraph(esc(title).upper(), _LABEL),
            _signature_cell(png),
            Table([[""]], colWidths=[half - 12], style=[("LINEABOVE", (0, 0), (-1, -1), 0.8, _INK)]),
            Paragraph(f"<b>{esc(name)}</b>" if name else "Name: ", _SMALL),
            Paragraph(esc(role), _SMALL) if role else Spacer(1, 0),
            Paragraph(f"Date: {_when(when, tz)}" if when else "Date: ", _SMALL),
        ]

    patient_name = doc_in.signer_name if answered else (doc_in.patient.get("full_name", "") if filled else "")
    relationship = doc_in.signer_relationship if answered and doc_in.signer_relationship != "Patient" else ""
    left = signature_block(
        "Patient (or authorised representative) signature",
        doc_in.patient_signature,
        patient_name,
        f"Signing as: {relationship}" if relationship else "",
        doc_in.signed_at,
    )
    right: list = []
    if content.signatures.provider:
        right = signature_block(
            "Clinician signature",
            doc_in.provider_signature,
            doc_in.provider_name if filled else "",
            "",
            doc_in.signed_at if doc_in.provider_signature else None,
        )
    pair = Table([[left, right]], colWidths=[half, half])
    pair.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    sig.append(pair)
    if content.signatures.witness or (answered and doc_in.witnessed_by):
        sig.append(Spacer(1, 12))
        witness_name = doc_in.witnessed_by.split("<")[0].strip() if answered else ""
        sig.append(_ruled(half, [[_labelled("Witnessed by (staff)", witness_name, write_in=not answered)]]))
    story.append(KeepTogether(sig))

    note_bits = [clinic, f"{content.title} v{doc_in.version_number}"]
    if doc_in.patient.get("full_name") and filled:
        note_bits.append(doc_in.patient["full_name"])
    doc.build(story, canvasmaker=paged(" · ".join(b for b in note_bits if b), confidential=filled))
    return buf.getvalue()
