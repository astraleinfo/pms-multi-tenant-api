"""Rendering a consultation (encounter + prescription) as a printable PDF.

Pure-Python via reportlab — no system/native dependencies, so it runs
anywhere the API itself runs. `build_consultation_pdf` is the only entry
point: everything else is layout detail for the one document it produces.

Every table cell holds a `Paragraph`, never a raw string — a raw string in a
reportlab `Table` does not wrap, so a value longer than its column silently
overruns into its neighbour. Wrapping fixes that regardless of how long a
clinician's free-text turns out to be.
"""

from __future__ import annotations

import io
from datetime import date, timedelta

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.models.clinical import Encounter, Patient, Prescription
from app.models.tenancy import TenantConfig
from app.schemas.common import local_now

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
_ALERT_BORDER = colors.HexColor("#e8998e")
_ALERT_INK = colors.HexColor("#8a1f11")
_ROW_ALT = colors.HexColor("#f7f8fa")
_HEADER_BG = colors.HexColor("#f0f2f6")

_STYLES = getSampleStyleSheet()

_CLINIC = ParagraphStyle(
    "ClinicName", parent=_STYLES["Normal"], fontName="Helvetica-Bold", fontSize=13.5,
    textColor=_INK, leading=16,
)
_EYEBROW = ParagraphStyle(
    "Eyebrow", parent=_STYLES["Normal"], fontName="Helvetica-Bold", fontSize=8,
    textColor=_ACCENT, leading=10, spaceAfter=1,
)
_TITLE = ParagraphStyle(
    "ConsultationTitle", parent=_STYLES["Normal"], fontName="Helvetica-Bold", fontSize=17,
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
    "SectionHeading", parent=_STYLES["Normal"], fontName="Helvetica-Bold", fontSize=9,
    textColor=_ACCENT, spaceBefore=14, spaceAfter=6, leading=11,
)
_BODY = ParagraphStyle("Body", parent=_STYLES["Normal"], fontSize=9.5, leading=13, textColor=_INK)
_BODY_SM = ParagraphStyle("BodySmall", parent=_STYLES["Normal"], fontSize=8.5, leading=12, textColor=_INK)
_LABEL = ParagraphStyle(
    "Label", parent=_STYLES["Normal"], fontName="Helvetica-Bold", fontSize=6.9,
    textColor=_INK_3, leading=9, spaceAfter=1,
)
_ALERT = ParagraphStyle(
    "Allergies", parent=_STYLES["Normal"], fontName="Helvetica-Bold", fontSize=9.5,
    leading=13, textColor=_ALERT_INK,
)
_TH = ParagraphStyle(
    "TableHead", parent=_STYLES["Normal"], fontName="Helvetica-Bold", fontSize=7.6,
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


def _letterhead_lines(config: TenantConfig | None) -> list[str]:
    """Address, phone/email, and hours — each one left out when the clinic
    has not set it, rather than printing a label with nothing after it."""
    if config is None:
        return []
    lines: list[str] = []
    if config.address:
        lines.append(config.address)
    contact = " · ".join(p for p in (config.contact_phone, config.contact_email) if p)
    if contact:
        lines.append(contact)
    if config.day_start and config.day_end:
        lines.append(f"Hours {config.day_start}–{config.day_end}")
    return lines


def _weight_display(patient: Patient) -> str:
    """`clinical_ext["weight"]` is a plain float once validated against the
    schema (§3.3) — never a raw request body, so no further checking here."""
    weight = (patient.clinical_ext or {}).get("weight")
    if not isinstance(weight, (int, float)):
        return "—"
    return f"{weight:g} kg"


_FOOTER_NOTE = "Generated by Astrale PMS — not valid without a clinician's signature."


def _footer(canvas, doc) -> None:  # noqa: ANN001 - reportlab's callback signature
    canvas.saveState()
    canvas.setStrokeColor(_LINE)
    canvas.setLineWidth(0.5)
    y = 14 * mm
    canvas.line(doc.leftMargin, y + 8, doc.pagesize[0] - doc.rightMargin, y + 8)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(_INK_3)
    canvas.drawString(doc.leftMargin, y - 2, _FOOTER_NOTE)
    canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, y - 2, f"Page {doc.page}")
    canvas.restoreState()


def _logo_flowable(logo: bytes | None) -> Image | None:
    """The clinic's mark, scaled to fit an 18mm square without distorting it
    — or `None` when there isn't one or the bytes turn out not to decode,
    which prints the letterhead exactly as it did before logos existed
    rather than failing the whole document over a bad image."""
    if not logo:
        return None
    try:
        return Image(io.BytesIO(logo), width=18 * mm, height=18 * mm, kind="proportional")
    except Exception:
        return None


def build_consultation_pdf(
    encounter: Encounter,
    prescription: Prescription | None,
    patient: Patient,
    *,
    tenant_name: str,
    doctor_name: str,
    tz: str | None = None,
    config: TenantConfig | None = None,
    logo: bytes | None = None,
) -> bytes:
    """Render one consultation as a PDF and return its bytes.

    `prescription` is optional: when the encounter has none, the medicines
    section is left out entirely rather than shown empty. `config` is
    optional for the same reason `tenant_name` alone used to be enough — a
    caller that has not fetched it yet still gets a valid PDF, just without
    the address/phone/hours line under the clinic name. `logo` is the
    clinic's own mark, already read from wherever it lives (§ storage) —
    this function only lays out bytes it is handed.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=16 * mm, bottomMargin=22 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
        title=f"Consultation — {patient.full_name}",
    )
    story: list = []

    # --- letterhead -------------------------------------------------------
    clinic_cell: list = [Paragraph((tenant_name or "Clinic").upper(), _CLINIC)]
    letterhead_lines = _letterhead_lines(config)
    if letterhead_lines:
        clinic_cell.append(Paragraph("<br/>".join(letterhead_lines), _CLINIC_META))

    date_cell = Paragraph(
        f"Issued {local_now(tz).strftime('%d %b %Y, %H:%M')}<br/>"
        f"Visit date {encounter.date.strftime('%d %b %Y')}",
        _DATE_RIGHT,
    )
    logo_cell = _logo_flowable(logo)
    if logo_cell is not None:
        logo_width = 22 * mm
        header = Table(
            [[logo_cell, clinic_cell, date_cell]],
            colWidths=[logo_width, doc.width * 0.6 - logo_width, doc.width * 0.4],
        )
    else:
        header = Table([[clinic_cell, date_cell]], colWidths=[doc.width * 0.6, doc.width * 0.4])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header)
    story.append(Spacer(1, 6))
    story.append(Paragraph("CONSULTATION RECORD", _EYEBROW))
    story.append(Paragraph(encounter.diagnosis or encounter.chief_complaint or "Consultation", _TITLE))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1.4, color=_ACCENT))
    story.append(Spacer(1, 12))

    # --- patient block ------------------------------------------------------
    story.append(Paragraph("PATIENT", _SECTION))
    dob_line = f"{patient.dob.isoformat()} · {_age_years(patient.dob, encounter.date)} yrs"
    patient_rows = [
        [_field("Name", patient.full_name), _field("Record no.", patient.mrn)],
        [_field("Date of birth", dob_line), _field("Sex", patient.sex.value.title())],
        [_field("Blood group", patient.blood_group or "—"), _field("Weight", _weight_display(patient))],
        [_field("Phone", patient.phone or "—"), ""],
    ]
    patient_table = Table(patient_rows, colWidths=[doc.width * 0.5, doc.width * 0.5])
    patient_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, _LINE),
    ]))
    story.append(patient_table)

    if patient.allergies and patient.allergies.strip().lower() not in {"none", "none recorded", "nil"}:
        story.append(Spacer(1, 8))
        alert = Table(
            [[Paragraph(f"ALLERGY ALERT — {patient.allergies}", _ALERT)]],
            colWidths=[doc.width],
        )
        alert.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), _ALERT_BG),
            ("BOX", (0, 0), (-1, -1), 0.75, _ALERT_BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(alert)

    # --- encounter block ------------------------------------------------------
    story.append(Paragraph("ASSESSMENT", _SECTION))
    assessment_rows = [
        [_field("Chief complaint", encounter.chief_complaint or "—")],
        [_field("Diagnosis", encounter.diagnosis or "—")],
        [_field("Clinical notes", encounter.notes or "—")],
    ]
    if encounter.follow_up_days:
        follow_up_date = encounter.date + timedelta(days=encounter.follow_up_days)
        assessment_rows.append([
            _field("Follow-up", f"{follow_up_date.isoformat()} — in {encounter.follow_up_days} days"),
        ])
    assessment_table = Table(assessment_rows, colWidths=[doc.width])
    assessment_table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(assessment_table)

    # --- prescription block -------------------------------------------------
    if prescription is not None and prescription.items:
        story.append(Paragraph("PRESCRIPTION", _SECTION))
        col_labels = ["Medicine", "Strength", "Dosage", "Frequency", "Duration", "Instructions", "Qty"]
        rows: list[list] = [[Paragraph(h, _TH) for h in col_labels]]
        for item in prescription.items:
            rows.append([
                Paragraph(item.medicine, _TD),
                Paragraph(item.strength or "—", _TD),
                Paragraph(item.dosage or "—", _TD),
                Paragraph(item.frequency or "—", _TD),
                Paragraph(item.duration or "—", _TD),
                Paragraph(item.instructions or "—", _TD),
                Paragraph(str(item.qty), _TD),
            ])
        # Generous room for the two columns that run long in practice
        # (frequency, instructions); everything wraps, so nothing overruns.
        col_widths = [doc.width * w for w in (0.18, 0.10, 0.12, 0.16, 0.11, 0.25, 0.08)]
        rx_table = Table(rows, colWidths=col_widths, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
            ("LINEBELOW", (0, 0), (-1, 0), 0.75, _LINE),
            ("LINEBELOW", (0, 1), (-1, -1), 0.5, _LINE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        for i in range(1, len(rows)):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), _ROW_ALT))
        rx_table.setStyle(TableStyle(style))
        story.append(rx_table)

    # --- footer / signature -------------------------------------------------
    story.append(Spacer(1, 30))
    story.append(KeepTogether([
        HRFlowable(width="45%", thickness=0.75, color=_INK_3),
        Spacer(1, 3),
        Paragraph(doctor_name or "Attending clinician", ParagraphStyle(
            "SigName", parent=_BODY, fontName="Helvetica-Bold",
        )),
        Paragraph("PRESCRIBING CLINICIAN — SIGNATURE", _LABEL),
    ]))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
