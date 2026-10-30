"""One consultation as a printable OPD summary, on a hospital form.

Black ink, a bordered patient box and ruled section titles — the look of the
paper a clinic hands a patient. It reads evidence first: the complaint, what
was found, then the diagnosis, the medicines and the review, closed by the
prescriber's name, qualifications and registration number over a line to sign.
(Prescription-writing guidance asks for a handwritten signature, so the
document leaves the line rather than printing one.)

A section with nothing to say is left out, and so is a label with nothing after
it: a clinic that has recorded no vitals, no allergies and no prescription
gets a shorter page, never one with holes in it. Free text flows as ordinary
paragraphs so a long note runs onto the next page; every table cell is a
`Paragraph` for the same reason.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from datetime import timedelta

from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, Spacer, Table, TableStyle

from app.models.clinical import Encounter, Patient, Prescription
from app.models.identity import User
from app.models.tenancy import TenantConfig
from app.schemas.common import local_now
from app.services.documents import money
from app.services.pdf import (
    _BLACK,
    _FORM_CELL,
    _GRID_HEAD,
    _INK_2,
    _NO_ALLERGIES,
    _STYLES,
    _age_years,
    _display,
    _fmt,
    allergy_box,
    document,
    esc,
    form_header,
    info_box,
    paged,
    section_title,
)
from app.services.pdf_fonts import FONT_BOLD

_TH = ParagraphStyle("RxHead", parent=_STYLES["Normal"], fontName=FONT_BOLD, fontSize=7.8, leading=10)
_TD = ParagraphStyle("RxCell", parent=_STYLES["Normal"], fontSize=8.8, leading=11.6)
_TD_RIGHT = ParagraphStyle("RxCellRight", parent=_TD, alignment=TA_RIGHT)
_TD_NOTE = ParagraphStyle("RxCellNote", parent=_TD, fontSize=7.6, leading=10, textColor=_INK_2)
_SMALL = ParagraphStyle("SignSmall", parent=_FORM_CELL, fontSize=8.2, leading=11, textColor=_INK_2)

#: Under a medicine the patient buys elsewhere, in place of a price.
_ELSEWHERE = "To be purchased separately"

#: The structured exam payloads a clinical type adds to `clinical_ext`, as one line each.
_LISTS = (("lesion_sites", "Lesion sites"), ("procedures", "Procedures"), ("treatments", "Treatments"))


def consultation_reference(encounter: Encounter) -> str:
    """A short code to quote when asking about this visit: `CON-` and the last six
    characters of its id. The tail, not the head — ids are UUIDv7, whose head is a
    timestamp and is the same for every visit made in the same few hours."""
    return "CON-" + encounter.id.hex[-6:].upper()


def _number(value: object) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)


def _present(value: object) -> bool:
    return value is not None and str(value).strip() != ""


#: The four vitals, in reading order: the encounter key, how it is printed, the label the
#: shared field definition gives it, and the unit that definition implies.
_VITALS = (
    ("bp", "BP", "Blood pressure", " mmHg"),
    ("pulse", "Pulse", "Pulse (bpm)", "/min"),
    ("temperature", "Temp", "Temperature (°F)", " °F"),
    ("spo2", "SpO₂", "SpO₂ (%)", "%"),
)

#: What a consultation prints in its own places — the complaint, notes, diagnosis and
#: follow-up as sections, the vitals as one line — so they are not repeated as clinic fields.
_PRINTED_ELSEWHERE = {"chief_complaint", "notes", "diagnosis", "follow_up_days"}
_PRINTED_ELSEWHERE |= {key for key, *_ in _VITALS}


def vital_labels(schema: Sequence) -> dict[str, str]:
    """The label each vital has in this clinic's schema. A clinic can define its own field
    under one of the vitals' keys (a temperature in °C, a blood pressure with the unit typed
    in), and that field replaces the shared one."""
    keys = {key for key, *_ in _VITALS}
    return {d.field_key: d.label for d in schema if d.field_key in keys}


def exam_fields(schema: Sequence, ext: dict) -> list[tuple[str, object]]:
    """The clinic's own encounter fields that have a value, as `(label, value)` in schema order.
    A list value (a multi-select) is kept; a structured payload (a dict — a tooth chart) is not,
    because the structured exam is printed from `clinical_ext` itself."""
    return [
        (d.label, ext[d.field_key])
        for d in schema
        if d.field_key not in _PRINTED_ELSEWHERE
        and ext.get(d.field_key) not in (None, "", [])
        and not isinstance(ext.get(d.field_key), dict)
    ]


def _vitals_line(ext: dict, labels: Mapping[str, str]) -> str:
    """Whichever vitals were recorded, in reading order — empty when none were. A vital whose
    field the clinic defined itself prints under the clinic's own label with no unit assumed;
    one typed with its unit already ("120/80 mmHg") is not given a second."""
    parts = []
    for key, short, default_label, unit in _VITALS:
        value = ext.get(key)
        if not _present(value):
            continue
        text = esc(_number(value))
        own = labels.get(key)
        if own is not None and own != default_label:
            parts.append(f"{esc(own)} {text}")
        elif isinstance(value, str) and any(ch.isalpha() for ch in value):
            parts.append(f"{short} {text}")
        else:
            parts.append(f"{short} {text}{unit}")
    return " · ".join(parts)


# The names the tooth chart shows on screen, keyed by what is stored in `clinical_ext.tooth_chart`.
# A state not listed here prints as stored: the column is open, and a newer build may have written one.
_TOOTH_STATE_LABELS = {
    "healthy": "Sound", "watch": "Watch", "caries": "Decay", "filling": "Filled", "crown": "Crowned",
    "extract": "For extraction", "mobility": "Mobility", "fracture": "Fracture",
}


def _structured_lines(ext: dict) -> list[tuple[str, str]]:
    """The discipline-specific exam payloads — a tooth chart, visual acuity, a
    refraction, lesion sites, procedures — as `(label, text)` lines."""
    lines: list[tuple[str, str]] = []
    chart = ext.get("tooth_chart")
    if isinstance(chart, dict) and chart:
        named = (
            (tooth, _TOOTH_STATE_LABELS.get(state, state) if isinstance(state, str) else state)
            for tooth, state in sorted(chart.items())
        )
        lines.append(("Tooth chart", ", ".join(f"{tooth} {name}" for tooth, name in named)))
    acuity = ext.get("va")
    if isinstance(acuity, dict):
        text = " · ".join(f"{eye.upper()} {acuity[eye]}" for eye in ("od", "os") if _present(acuity.get(eye)))
        if text:
            lines.append(("Visual acuity", text))
    refraction = ext.get("refraction")
    if isinstance(refraction, dict):
        text = " · ".join(
            f"{key.replace('_', ' ')} {val}" for key, val in refraction.items() if _present(val)
        )
        if text:
            lines.append(("Refraction", text))
    for key, label in _LISTS:
        values = ext.get(key)
        if isinstance(values, list) and values:
            lines.append((label, ", ".join(str(v) for v in values)))
    return lines


def _weight(patient: Patient) -> str:
    weight = (patient.clinical_ext or {}).get("weight")
    return f"{weight:g} kg" if isinstance(weight, (int, float)) and not isinstance(weight, bool) else ""


def _paragraph(text: str, *, label: str = "") -> Paragraph:
    """Typed text as a paragraph, line breaks kept, with an optional bold label in front."""
    body = esc(text).replace("\n", "<br/>")
    return Paragraph(f"<b>{esc(label)}:</b> {body}" if label else body, _FORM_CELL)


def _medicines(doc, prescription: Prescription, currency: str) -> Table:
    """The medicines, as a grid. A line the clinic hands over and has priced shows
    its unit price and amount, totalled underneath; a line the patient buys
    elsewhere shows no price, only a note saying so. With no priced line at all —
    a clinic that only prescribes — the price columns are left out and the page
    is a plain prescription."""
    priced = [i for i in prescription.items if i.dispense and i.unit_price is not None]
    heads = ["#", "Medicine", "Dosage", "Frequency", "Duration", "Instructions", "Qty"]
    widths = [0.05, 0.27, 0.12, 0.17, 0.11, 0.20, 0.08]
    if priced:
        heads += ["Unit price", "Amount"]
        widths = [0.04, 0.22, 0.10, 0.13, 0.09, 0.14, 0.06, 0.11, 0.11]

    rows: list[list] = [[Paragraph(h, _TH) for h in heads]]
    for n, item in enumerate(prescription.items, start=1):
        medicine: list = [Paragraph(
            f"<b>{esc(item.medicine)}</b>" + (f"<br/>{esc(item.strength)}" if item.strength else ""), _TD
        )]
        if not item.dispense:
            medicine.append(Paragraph(_ELSEWHERE, _TD_NOTE))
        row = [
            Paragraph(str(n), _TD), medicine,
            Paragraph(esc(item.dosage) or "—", _TD), Paragraph(esc(item.frequency) or "—", _TD),
            Paragraph(esc(item.duration) or "—", _TD), Paragraph(esc(item.instructions) or "—", _TD),
            Paragraph(str(item.qty), _TD),
        ]
        if priced:
            has_price = item.dispense and item.unit_price is not None
            row += [
                Paragraph(money(item.unit_price, currency) if has_price else "—", _TD_RIGHT),
                Paragraph(money(item.qty * item.unit_price, currency) if has_price else "—", _TD_RIGHT),
            ]
        rows.append(row)

    style = [
        ("GRID", (0, 0), (-1, -1), 0.6, _BLACK),
        ("BACKGROUND", (0, 0), (-1, 0), _GRID_HEAD),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4.5), ("RIGHTPADDING", (0, 0), (-1, -1), 4.5),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]
    if priced:
        total = sum(i.qty * i.unit_price for i in priced)
        rows.append([Paragraph("<b>Total</b>", _TD_RIGHT), *[""] * (len(heads) - 2),
                     Paragraph(f"<b>{money(total, currency)}</b>", _TD_RIGHT)])
        style.append(("SPAN", (0, len(rows) - 1), (-2, len(rows) - 1)))

    table = Table(rows, colWidths=[doc.width * w for w in widths], repeatRows=1)
    table.setStyle(TableStyle(style))
    return table


def _sign_off(doc, doctor: User | None, on) -> KeepTogether:
    """The prescriber's name, qualifications and registration number under a line
    to sign on, at the bottom right."""
    who: list = [Paragraph(f"<b>{esc(doctor.name if doctor else 'Attending clinician')}</b>", _FORM_CELL)]
    if doctor and (doctor.qualifications or "").strip():
        who.append(Paragraph(esc(doctor.qualifications.strip()), _SMALL))
    if doctor and (doctor.registration_no or "").strip():
        who.append(Paragraph(f"Reg. No. {esc(doctor.registration_no.strip())}", _SMALL))
    who.append(Paragraph(f"Date: {_fmt(on)}", _SMALL))
    width = 72 * mm
    block = Table([["", Spacer(1, 20 * mm)], ["", who]], colWidths=[doc.width - width, width],
                  spaceBefore=14)
    block.setStyle(TableStyle([
        ("LINEABOVE", (1, 1), (1, 1), 0.9, _BLACK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (1, 1), (1, 1), 3),
    ]))
    return KeepTogether([block])


def build_consultation_pdf(
    encounter: Encounter,
    prescription: Prescription | None,
    patient: Patient,
    *,
    tenant_name: str,
    doctor: User | None,
    tz: str | None = None,
    config: TenantConfig | None = None,
    logo: bytes | None = None,
    exam_fields: Sequence[tuple[str, object]] = (),
    vital_labels: Mapping[str, str] | None = None,
) -> bytes:
    """Render one consultation as a PDF and return its bytes.

    `prescription` is optional: with none, the medicines section is left out.
    `doctor` is the clinician the visit was recorded under — their name,
    qualifications and registration number close the page. `exam_fields` are
    the clinic's own encounter fields that have a value, already labelled and
    in order (see `exam_fields`); `vital_labels` is each vital's label in the clinic's
    schema (see `vital_labels`). The structured exam payloads and the vitals
    themselves are read from `encounter.clinical_ext` here.
    """
    buf = io.BytesIO()
    doc = document(buf, title=f"Consultation — {patient.full_name}", author=tenant_name or "Clinic")
    ext = encounter.clinical_ext or {}
    reference = consultation_reference(encounter)

    story: list = form_header(
        doc, clinic=tenant_name, config=config, logo=logo, title="OPD consultation summary",
    )
    story.append(info_box(doc, [
        ("Patient", patient.full_name),
        ("Record no.", patient.mrn),
        ("Age / Sex", f"{_age_years(patient.dob, encounter.date)} yrs / {patient.sex.value.title()}"),
        ("Date", _fmt(encounter.date)),
        ("Weight", _weight(patient)),
        ("Blood group", patient.blood_group),
        ("Doctor", doctor.name if doctor else ""),
        ("Phone", patient.phone),
        ("Reference no.", reference),
    ]))

    allergies = (patient.allergies or "").strip()
    if allergies.lower() not in _NO_ALLERGIES:
        story += [Spacer(1, 7), allergy_box(doc, allergies)]

    complaint = (encounter.chief_complaint or "").strip()
    if complaint:
        story += [section_title(doc, "History"), _paragraph(complaint, label="Chief complaints")]

    examination: list = []
    vitals = _vitals_line(ext, vital_labels or {})
    if vitals:
        examination.append(Paragraph(f"<b>Vitals:</b> {vitals}", _FORM_CELL))
    notes = (encounter.notes or "").strip()
    if notes:
        examination.append(_paragraph(notes, label="Findings"))
    lines = [(label, _display(value)) for label, value in exam_fields if _present(value)]
    for label, text in (*lines, *_structured_lines(ext)):
        examination.append(_paragraph(text, label=label))
    if examination:
        story.append(section_title(doc, "Examination"))
        for n, block in enumerate(examination):
            story += [block] if n == 0 else [Spacer(1, 3), block]

    diagnosis = (encounter.diagnosis or "").strip()
    if diagnosis:
        story += [section_title(doc, "Diagnosis"), Paragraph(f"<b>{esc(diagnosis)}</b>", _FORM_CELL)]

    if prescription is not None and prescription.items:
        currency = config.currency.value if config is not None and config.currency else ""
        story += [section_title(doc, "Medication advised"), _medicines(doc, prescription, currency)]

    if encounter.follow_up_days:
        review = encounter.date + timedelta(days=encounter.follow_up_days)
        story += [
            section_title(doc, "Follow-up"),
            Paragraph(f"Review on <b>{_fmt(review)}</b> (in {encounter.follow_up_days} days).", _FORM_CELL),
        ]

    story.append(_sign_off(doc, doctor, encounter.date))

    printed = local_now(tz).strftime("%d %b %Y, %H:%M")
    note = f"Printed {printed} · {reference} · {patient.full_name}"
    doc.build(story, canvasmaker=paged(note, confidential=True))
    return buf.getvalue()
