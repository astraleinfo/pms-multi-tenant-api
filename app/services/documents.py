"""Printable invoices and practice reports.

The consultation record lives in `pdf.py`; these two share its palette and
type so that everything a clinic prints looks like it came from one place.

Amounts are printed with the ISO code ("INR 1,250.00") rather than a symbol:
the built-in PDF fonts have no rupee sign, and a code reads the same on every
printer and in every language.
"""

from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.models.enums import InvoiceStatus
from app.models.ops import Invoice
from app.models.tenancy import TenantConfig
from app.services.pdf import (
    _ACCENT,
    _ACCENT_SOFT,
    _BODY,
    _BODY_SM,
    _CLINIC,
    _EYEBROW,
    _FOOT,
    _HEADER_BG,
    _INK,
    _INK_2,
    _INK_3,
    _LABEL,
    _LINE,
    _ROW_ALT,
    _SECTION,
    _STYLES,
    _TD,
    _TH,
    _TITLE,
)

_OK = colors.HexColor("#1f7a4d")
_OK_SOFT = colors.HexColor("#e6f4ec")
_WARN = colors.HexColor("#8a5a00")
_WARN_SOFT = colors.HexColor("#fdf3dc")
_VOID = colors.HexColor("#8a1f11")
_VOID_SOFT = colors.HexColor("#fdecea")

_RIGHT = ParagraphStyle("Right", parent=_BODY, alignment=2)
_TD_RIGHT = ParagraphStyle("CellRight", parent=_TD, alignment=2)
_TH_RIGHT = ParagraphStyle("HeadRight", parent=_TH, alignment=2)
_TOTAL = ParagraphStyle(
    "Total", parent=_BODY, fontName="Helvetica-Bold", fontSize=12, leading=15, alignment=2
)
_TOTAL_LABEL = ParagraphStyle(
    "TotalLabel", parent=_BODY, fontName="Helvetica-Bold", fontSize=10.5, leading=15
)
_KPI_VALUE = ParagraphStyle(
    "KpiValue", parent=_STYLES["Normal"], fontName="Helvetica-Bold", fontSize=15, leading=18,
    textColor=_INK,
)
_KPI_LABEL = ParagraphStyle(
    "KpiLabel", parent=_STYLES["Normal"], fontName="Helvetica-Bold", fontSize=6.8, leading=9,
    textColor=_INK_3,
)
_KPI_HINT = ParagraphStyle(
    "KpiHint", parent=_STYLES["Normal"], fontSize=7.4, leading=9.5, textColor=_INK_2,
)

_STATUS_LOOK = {
    InvoiceStatus.DRAFT: ("DRAFT", _WARN, _WARN_SOFT),
    InvoiceStatus.ISSUED: ("ISSUED", _ACCENT, _ACCENT_SOFT),
    InvoiceStatus.PARTIALLY_PAID: ("PART PAID", _WARN, _WARN_SOFT),
    InvoiceStatus.PAID: ("PAID", _OK, _OK_SOFT),
    InvoiceStatus.VOID: ("VOID", _VOID, _VOID_SOFT),
}


def money(amount: float | Decimal | int | None, code: str) -> str:
    return f"{code} {float(amount or 0):,.2f}".strip()


def _esc(text: str | None) -> str:
    """Paragraph text is a mini-markup, so user text is escaped before it goes in."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plain(table: Table, extra: list | None = None) -> Table:
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        *(extra or []),
    ]))
    return table


def _grid(rows: list[list], widths: list[float], *, numeric_from: int | None = None) -> Table:
    """A zebra-striped data table with a shaded header row."""
    table = Table(rows, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, _LINE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, _LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    style += [("BACKGROUND", (0, i), (-1, i), _ROW_ALT) for i in range(2, len(rows), 2)]
    table.setStyle(TableStyle(style))
    return table


def _letterhead(doc, *, clinic: str, config: TenantConfig | None, right: list) -> Table:
    """Clinic name and contact on the left, the document's own facts on the right."""
    contact = []
    if config is not None:
        if config.address:
            contact.append(_esc(config.address).replace("\n", "<br/>"))
        line = " · ".join(_esc(v) for v in (config.contact_phone, config.contact_email) if v)
        if line:
            contact.append(line)
    left = [Paragraph(_esc(clinic or "Clinic").upper(), _CLINIC)]
    if contact:
        left.append(Spacer(1, 3))
        left.append(Paragraph("<br/>".join(contact), _BODY_SM))
    return _plain(
        Table([[left, right]], colWidths=[doc.width * 0.58, doc.width * 0.42]),
    )


def _footer_for(note: str):
    def draw(canvas, doc) -> None:  # noqa: ANN001 - reportlab's callback signature
        canvas.saveState()
        canvas.setStrokeColor(_LINE)
        canvas.setLineWidth(0.5)
        y = 14 * mm
        canvas.line(doc.leftMargin, y + 8, doc.pagesize[0] - doc.rightMargin, y + 8)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(_INK_3)
        canvas.drawString(doc.leftMargin, y - 2, note)
        canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, y - 2, f"Page {doc.page}")
        canvas.restoreState()

    return draw


def _stamp(label: str, ink, ground) -> Table:
    stamp = Table([[Paragraph(
        label,
        ParagraphStyle("Stamp", parent=_BODY, fontName="Helvetica-Bold", fontSize=8.5,
                       textColor=ink, alignment=1, leading=10),
    )]])
    stamp.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ground),
        ("BOX", (0, 0), (-1, -1), 0.8, ink),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return stamp


# --- the invoice ----------------------------------------------------------------


def build_invoice_pdf(
    invoice: Invoice,
    totals: dict[str, float],
    *,
    clinic: str,
    config: TenantConfig | None,
    currency: str,
) -> bytes:
    """One invoice, as the patient is handed it."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=16 * mm, bottomMargin=22 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
        title=f"Invoice {invoice.number}", author=clinic,
    )
    status = InvoiceStatus(invoice.status)
    label, ink, ground = _STATUS_LOOK[status]
    story: list = []

    facts = _plain(
        Table(
            [
                [Paragraph("INVOICE", ParagraphStyle(
                    "InvWord", parent=_TITLE, alignment=2, textColor=_ACCENT, fontSize=20, leading=23,
                ))],
                [Paragraph(f"<b>{_esc(invoice.number)}</b>", _RIGHT)],
                [Paragraph(
                    f"Issued {invoice.issued_at.strftime('%d %b %Y')}<br/>"
                    f"Due {invoice.due_at.strftime('%d %b %Y')}",
                    ParagraphStyle("Dates", parent=_BODY_SM, alignment=2, textColor=_INK_2),
                )],
            ],
            colWidths=[doc.width * 0.42],
        ),
        [("TOPPADDING", (0, 1), (-1, -1), 2)],
    )
    story.append(_letterhead(doc, clinic=clinic, config=config, right=[facts]))
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1.4, color=_ACCENT))
    story.append(Spacer(1, 12))

    patient = invoice.patient
    bill_to = [
        Paragraph("BILLED TO", _LABEL),
        Paragraph(f"<b>{_esc(patient.full_name)}</b>", _BODY),
        Paragraph(
            " · ".join(v for v in (f"Record {_esc(patient.mrn)}", _esc(patient.phone)) if v),
            _BODY_SM,
        ),
    ]
    about = [
        Paragraph("COUNTER", _LABEL),
        Paragraph(invoice.department.value.title() if invoice.department else "Clinic", _BODY),
        Spacer(1, 4),
        _stamp(label, ink, ground),
    ]
    story.append(_plain(Table([[bill_to, about]], colWidths=[doc.width * 0.7, doc.width * 0.3])))
    story.append(Spacer(1, 14))

    # --- lines
    rows: list[list] = [[
        Paragraph("#", _TH), Paragraph("Description", _TH), Paragraph("Qty", _TH_RIGHT),
        Paragraph("Unit price", _TH_RIGHT), Paragraph("Tax", _TH_RIGHT), Paragraph("Amount", _TH_RIGHT),
    ]]
    for n, line in enumerate(invoice.line_items, start=1):
        amount = Decimal(line.qty) * Decimal(line.unit_price)
        rows.append([
            Paragraph(str(n), _TD),
            Paragraph(_esc(line.description), _TD),
            Paragraph(str(line.qty), _TD_RIGHT),
            Paragraph(money(line.unit_price, currency), _TD_RIGHT),
            Paragraph(f"{float(line.tax_pct):g}%", _TD_RIGHT),
            Paragraph(money(amount, currency), _TD_RIGHT),
        ])
    if len(rows) == 1:
        rows.append([Paragraph("", _TD), Paragraph("No lines on this invoice.", _TD), "", "", "", ""])
    story.append(_grid(rows, [doc.width * w for w in (0.06, 0.42, 0.08, 0.16, 0.10, 0.18)]))
    story.append(Spacer(1, 10))

    # --- totals
    total_rows = [
        [Paragraph("Subtotal", _BODY), Paragraph(money(totals["subtotal"], currency), _RIGHT)],
        [Paragraph("Tax", _BODY), Paragraph(money(totals["tax"], currency), _RIGHT)],
        [Paragraph("Total", _TOTAL_LABEL), Paragraph(money(totals["total"], currency), _TOTAL)],
        [Paragraph("Paid", _BODY), Paragraph(money(totals["paid"], currency), _RIGHT)],
        [Paragraph("Balance due", _TOTAL_LABEL), Paragraph(money(totals["balance"], currency), _TOTAL)],
    ]
    totals_table = Table(total_rows, colWidths=[doc.width * 0.2, doc.width * 0.22])
    totals_table.setStyle(TableStyle([
        ("LINEABOVE", (0, 2), (-1, 2), 0.75, _INK_3),
        ("LINEABOVE", (0, 4), (-1, 4), 0.75, _INK_3),
        ("BACKGROUND", (0, 4), (-1, 4), _ACCENT_SOFT),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(_plain(
        Table([["", totals_table]], colWidths=[doc.width * 0.58, doc.width * 0.42]),
    ))

    # --- payments
    if invoice.payments:
        story.append(Paragraph("PAYMENTS RECEIVED", _SECTION))
        pay_rows: list[list] = [[
            Paragraph("Date", _TH), Paragraph("Method", _TH), Paragraph("Reference", _TH),
            Paragraph("Amount", _TH_RIGHT),
        ]]
        for p in invoice.payments:
            pay_rows.append([
                Paragraph(p.at.strftime("%d %b %Y") if p.at else "—", _TD),
                Paragraph(p.method.value.upper() if p.method else "—", _TD),
                Paragraph(_esc(p.reference) or "—", _TD),
                Paragraph(money(p.amount, currency), _TD_RIGHT),
            ])
        story.append(_grid(pay_rows, [doc.width * w for w in (0.22, 0.2, 0.36, 0.22)]))

    # --- terms
    story.append(Spacer(1, 18))
    terms = []
    if status == InvoiceStatus.VOID:
        terms.append("This invoice has been voided and is not payable.")
    elif totals["balance"] > 0:
        terms.append(
            f"Please settle the balance of {money(totals['balance'], currency)} "
            f"by {invoice.due_at.strftime('%d %b %Y')}."
        )
    else:
        terms.append("Paid in full — thank you.")
    if config is not None and config.contact_phone:
        terms.append(f"Questions about this invoice: {_esc(config.contact_phone)}.")
    story.append(KeepTogether([Paragraph(" ".join(terms), _FOOT)]))

    doc.build(
        story,
        onFirstPage=_footer_for(f"{clinic} · Invoice {invoice.number}"),
        onLaterPages=_footer_for(f"{clinic} · Invoice {invoice.number}"),
    )
    return buf.getvalue()


# --- the practice report ----------------------------------------------------------


def _kpis(doc, items: list[tuple[str, str, str]]) -> Table:
    """A row of figure tiles: label, value, a one-line hint."""
    per_row = 4
    cells = [
        [Paragraph(label.upper(), _KPI_LABEL), Paragraph(value, _KPI_VALUE), Paragraph(hint, _KPI_HINT)]
        for label, value, hint in items
    ]
    while len(cells) % per_row:
        cells.append("")
    rows = [cells[i:i + per_row] for i in range(0, len(cells), per_row)]
    width = doc.width / per_row
    table = Table(rows, colWidths=[width] * per_row)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.6, _LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, _LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return table


def _bar_chart(width: float, series: list[dict]) -> Drawing:
    """Encounters per day. A drawing, so it stays sharp at any zoom and prints clean."""
    height = 150
    drawing = Drawing(width, height)
    values = [int(p.get("value", 0)) for p in series] or [0]
    chart = VerticalBarChart()
    chart.x, chart.y = 28, 22
    chart.width, chart.height = width - 40, height - 40
    chart.data = [values]
    chart.bars[0].fillColor = _ACCENT
    chart.bars[0].strokeColor = None
    chart.barSpacing = 1
    chart.valueAxis.valueMin = 0
    top = max(values)
    chart.valueAxis.valueMax = max(4, top + (top // 4 or 1))
    chart.valueAxis.labels.fontSize = 6.5
    chart.valueAxis.labels.fillColor = _INK_3
    chart.valueAxis.strokeColor = _LINE
    chart.valueAxis.gridStrokeColor = _LINE
    chart.valueAxis.visibleGrid = True
    step = max(1, len(values) // 8)
    chart.categoryAxis.categoryNames = [
        (p.get("date", "")[5:] if i % step == 0 else "") for i, p in enumerate(series)
    ] or [""]
    chart.categoryAxis.labels.fontSize = 6.5
    chart.categoryAxis.labels.fillColor = _INK_3
    chart.categoryAxis.strokeColor = _LINE
    drawing.add(chart)
    if not any(values):
        # On a white plate: centred on the plot, the note would otherwise sit
        # on a gridline and read as struck through.
        note = "No encounters in this period"
        plate_w, cx, cy = 150, chart.x + chart.width / 2, chart.y + chart.height / 2
        drawing.add(Rect(cx - plate_w / 2, cy - 8, plate_w, 18, fillColor=colors.white,
                         strokeColor=_LINE, strokeWidth=0.5, rx=3, ry=3))
        drawing.add(String(cx, cy - 2, note, textAnchor="middle", fontSize=8, fillColor=_INK_3))
    return drawing


def _ranked(doc, title: str, rows: list[dict], *, value_label: str, fmt=str) -> list:
    if not rows:
        return []
    data: list[list] = [[Paragraph(title, _TH), Paragraph(value_label, _TH_RIGHT)]]
    data += [
        [Paragraph(_esc(str(r.get("label", "—")).replace("_", " ").capitalize()), _TD),
         Paragraph(fmt(r.get("value", 0)), _TD_RIGHT)]
        for r in rows
    ]
    return [_grid(data, [doc.width * 0.7, doc.width * 0.3])]


def build_report_pdf(
    data: dict,
    *,
    clinic: str,
    config: TenantConfig | None,
    currency: str,
    generated_by: str,
) -> bytes:
    """The practice report for one window — the figures the reports page shows,
    in a form that can be filed, emailed to an owner, or printed for a meeting."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=16 * mm, bottomMargin=22 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
        title=f"Practice report — {clinic}", author=clinic,
    )
    rng = data["range"]
    start, end = date.fromisoformat(rng["from"]), date.fromisoformat(rng["to"])
    core = data["core"]
    modules = data.get("modules") or {}
    cash = lambda v: money(v, currency)  # noqa: E731 - a one-line formatter

    story: list = []
    period = _plain(Table([
        [Paragraph(
            f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}<br/>"
            f"{rng['days']} days · generated {datetime.now().strftime('%d %b %Y, %H:%M')}",
            ParagraphStyle("Period", parent=_BODY_SM, alignment=2, textColor=_INK_2),
        )],
    ], colWidths=[doc.width * 0.42]))
    story.append(_letterhead(doc, clinic=clinic, config=config, right=[period]))
    story.append(Spacer(1, 8))
    story.append(Paragraph("PRACTICE REPORT", _EYEBROW))
    story.append(Paragraph("How the practice ran", _TITLE))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1.4, color=_ACCENT))
    story.append(Spacer(1, 12))

    story.append(_kpis(doc, [
        ("Patients", f"{core.get('patients', 0):,}", f"{core.get('new_patients', 0)} new this month"),
        ("Encounters", f"{core.get('encounters', 0):,}", "recorded in the period"),
        ("Prescriptions", f"{core.get('prescriptions', 0):,}", "written in the period"),
        ("Collected", cash(core.get("revenue", 0)), "payments received"),
        ("No-shows", f"{core.get('no_shows', 0):,}", "booked and missed"),
        ("Average wait", f"{core.get('avg_wait_minutes', 0):g} min",
         f"longest {core.get('max_wait_minutes', 0):g} min"),
        ("Consultation", f"{core.get('avg_consultation_minutes', 0):g} min", "average length"),
        ("Never visited", f"{core.get('never_visited', 0):,}", "registered, not yet seen"),
    ]))

    story.append(Paragraph("VISITS PER DAY", _SECTION))
    story.append(_bar_chart(doc.width, core.get("visits_series") or []))

    top = core.get("top_diagnoses") or []
    if top:
        story.append(Paragraph("MOST COMMON DIAGNOSES", _SECTION))
        story += _ranked(doc, "Diagnosis", top, value_label="Encounters")

    billing = modules.get("billing")
    if billing:
        story.append(Paragraph("BILLING", _SECTION))
        story.append(_kpis(doc, [
            ("Billed", cash(billing["billed"]), f"{billing['count']} live invoices"),
            ("Paid", cash(billing["paid"]), "against those invoices"),
            ("Outstanding", cash(billing["outstanding"]), f"{billing['unpaid']} unpaid"),
            ("Drafts", f"{billing['drafts']}", "not yet issued"),
        ]))
        if billing.get("revenue_mix"):
            story.append(Spacer(1, 8))
            story += _ranked(doc, "Revenue by source", billing["revenue_mix"],
                             value_label="Billed", fmt=cash)

    pharmacy = modules.get("pharmacy")
    if pharmacy:
        story.append(Paragraph("PHARMACY", _SECTION))
        story.append(_kpis(doc, [
            ("Units dispensed", f"{pharmacy['dispensed_units']:,}", "in the period"),
            ("Dispensed value", cash(pharmacy["dispensed_value"]), "at selling price"),
            ("Waiting", f"{pharmacy['prescriptions_waiting']}", "prescriptions at the counter"),
            ("Fully dispensed", f"{pharmacy['fully_dispensed']}", "prescriptions"),
        ]))

    lab = modules.get("lab")
    if lab:
        story.append(Paragraph("LAB", _SECTION))
        story.append(_kpis(doc, [
            ("Orders", f"{lab['total']}", f"{lab['ordered']} not started"),
            ("In progress", f"{lab['pending']}", f"{lab['resulted']} awaiting verification"),
            ("Verified", f"{lab['verified']}", "released to clinicians"),
            ("Abnormal", f"{lab['abnormal']}", "with a flagged result"),
        ]))

    inventory = modules.get("inventory")
    if inventory:
        story.append(Paragraph("INVENTORY", _SECTION))
        low = inventory.get("below_reorder") or []
        expiring = inventory.get("expiring_soon") or []
        story.append(Paragraph(
            f"{inventory.get('items', 0)} stock lines · {len(low)} below reorder level · "
            f"{len(expiring)} batches expiring soon", _BODY,
        ))
        if low:
            story.append(Spacer(1, 6))
            rows = [[Paragraph("Below reorder level", _TH), Paragraph("On hand", _TH_RIGHT),
                     Paragraph("Reorder at", _TH_RIGHT)]]
            rows += [[Paragraph(_esc(r["name"]), _TD), Paragraph(str(r["on_hand"]), _TD_RIGHT),
                      Paragraph(str(r["reorder_level"]), _TD_RIGHT)] for r in low[:15]]
            story.append(_grid(rows, [doc.width * 0.6, doc.width * 0.2, doc.width * 0.2]))

    advanced = modules.get("advanced_reports")
    if advanced and advanced.get("clinicians"):
        story.append(Paragraph("CLINICIANS", _SECTION))
        rows = [[Paragraph("Clinician", _TH), Paragraph("Encounters", _TH_RIGHT),
                 Paragraph("Prescriptions", _TH_RIGHT), Paragraph("Billed", _TH_RIGHT)]]
        rows += [
            [Paragraph(_esc(c["name"]), _TD), Paragraph(str(c["encounters"]), _TD_RIGHT),
             Paragraph(str(c["prescriptions"]), _TD_RIGHT), Paragraph(cash(c["revenue"]), _TD_RIGHT)]
            for c in advanced["clinicians"]
        ]
        story.append(_grid(rows, [doc.width * w for w in (0.4, 0.18, 0.2, 0.22)]))

    story.append(Spacer(1, 16))
    story.append(Paragraph(
        f"Prepared for {_esc(generated_by)}. Figures are computed from the clinic's own records "
        "for the period shown; totals on other screens for the same period will match.",
        _FOOT,
    ))

    note = f"{clinic} · Practice report {start.isoformat()} to {end.isoformat()}"
    doc.build(story, onFirstPage=_footer_for(note), onLaterPages=_footer_for(note))
    return buf.getvalue()


# --- the platform's own bill to a clinic ---------------------------------------------


def build_subscription_invoice_pdf(bill, *, clinic: str, hold_on: date) -> bytes:
    """The monthly subscription bill, from the platform to one clinic."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=16 * mm, bottomMargin=22 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
        title=f"Subscription bill {bill.number}", author="Astrale Practice OS",
    )
    status = str(getattr(bill.status, "value", bill.status))
    look = {
        "open": ("DUE", _ACCENT, _ACCENT_SOFT),
        "paid": ("PAID", _OK, _OK_SOFT),
        "void": ("VOID", _VOID, _VOID_SOFT),
    }[status]
    code = bill.currency
    story: list = []

    facts = _plain(Table([
        [Paragraph("BILL", ParagraphStyle(
            "BillWord", parent=_TITLE, alignment=2, textColor=_ACCENT, fontSize=20, leading=23,
        ))],
        [Paragraph(f"<b>{_esc(bill.number)}</b>", _RIGHT)],
        [Paragraph(
            f"Issued {bill.issued_on.strftime('%d %b %Y')}<br/>Due {bill.due_on.strftime('%d %b %Y')}",
            ParagraphStyle("BillDates", parent=_BODY_SM, alignment=2, textColor=_INK_2),
        )],
    ], colWidths=[doc.width * 0.42]), [("TOPPADDING", (0, 1), (-1, -1), 2)])
    issuer = [
        Paragraph("ASTRALE PRACTICE OS", _CLINIC),
        Spacer(1, 3),
        Paragraph("Monthly subscription for the modules switched on for your clinic.", _BODY_SM),
    ]
    story.append(_plain(Table([[issuer, [facts]]], colWidths=[doc.width * 0.58, doc.width * 0.42])))
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1.4, color=_ACCENT))
    story.append(Spacer(1, 12))

    story.append(_plain(Table([[
        [Paragraph("BILLED TO", _LABEL), Paragraph(f"<b>{_esc(clinic)}</b>", _BODY),
         Paragraph(
             f"Period {bill.period_start.strftime('%d %b %Y')} – "
             f"{bill.period_end.strftime('%d %b %Y')}",
             _BODY_SM,
         )],
        [Spacer(1, 12), _stamp(*look)],
    ]], colWidths=[doc.width * 0.7, doc.width * 0.3])))
    story.append(Spacer(1, 14))

    rows: list[list] = [[Paragraph("Module", _TH), Paragraph("Monthly", _TH_RIGHT)]]
    rows += [
        [Paragraph(_esc(line["name"]), _TD), Paragraph(money(line["amount"], code), _TD_RIGHT)]
        for line in bill.lines or []
    ]
    story.append(_grid(rows, [doc.width * 0.7, doc.width * 0.3]))
    story.append(Spacer(1, 10))

    totals = Table(
        [[Paragraph("Total", _TOTAL_LABEL), Paragraph(money(bill.amount, code), _TOTAL)]],
        colWidths=[doc.width * 0.2, doc.width * 0.22],
    )
    totals.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _ACCENT_SOFT),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(_plain(Table([["", totals]], colWidths=[doc.width * 0.58, doc.width * 0.42])))
    story.append(Spacer(1, 18))

    if status == "paid":
        note = "Paid — thank you."
        if bill.payment_reference:
            note += f" Reference {_esc(bill.payment_reference)}."
    elif status == "void":
        note = "This bill has been withdrawn and is not payable."
    else:
        note = (
            f"Please pay by {bill.due_on.strftime('%d %b %Y')}. If it is still unpaid on "
            f"{hold_on.strftime('%d %b %Y')}, the clinic is put on hold until it is settled."
        )
    story.append(Paragraph(note, _FOOT))

    footer = _footer_for(f"Astrale Practice OS · Subscription bill {bill.number}")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
