"""Optional PDF summary. One short document: score, what we fixed, what to check, totals by category.

reportlab is the only dependency and it is optional: if it is missing or the PDF fails, the pipeline
still delivers the Excel report, the CSV, and the log. Uses built-in fonts, so there are no font files to ship.
"""
from datetime import date
from xml.sax.saxutils import escape

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

SLATE = colors.HexColor("#2F4A63")
OCHRE_BG, OCHRE_LINE, OCHRE_INK = colors.HexColor("#F6EFE0"), colors.HexColor("#D9C9A3"), colors.HexColor("#6B5320")
LINE, MUTED, INK = colors.HexColor("#DCD7CD"), colors.HexColor("#5C5850"), colors.HexColor("#2B2B2B")
MAX_CATEGORY_ROWS = 30
FIXED = {"duplicates": "Exact duplicate rows removed", "duplicate_ids": "Rows with a repeated ID removed (first kept)"}
CHECK = {"missing_dates": "Rows with no date", "unreadable_dates": "Dates we couldn't read",
         "missing_amounts": "Rows with no amount", "unreadable_amounts": "Amounts we couldn't read",
         "outliers": "Unusual amounts (kept, but flagged)"}
ORDER = {"month": "month-first", "day": "day-first"}
SOURCE = {"chosen": "your choice", "detected": "detected from the file",
          "conflict": "the file mixes both orders, so the default was used",
          "assumed": "the file gives no clue, so the default was used"}


def _styles():
    base = getSampleStyleSheet()["Normal"]
    s = {"body": ParagraphStyle("body", parent=base, fontName="Helvetica", fontSize=10, leading=14, textColor=INK)}
    s["muted"] = ParagraphStyle("muted", parent=s["body"], textColor=MUTED)
    s["title"] = ParagraphStyle("title", parent=s["body"], fontSize=20, leading=24, spaceAfter=2)
    s["h2"] = ParagraphStyle("h2", parent=s["body"], fontName="Helvetica-Bold", fontSize=12, leading=16,
                             spaceBefore=14, spaceAfter=6)
    s["score"] = ParagraphStyle("score", parent=s["body"], fontSize=30, leading=34, textColor=SLATE)
    s["warn"] = ParagraphStyle("warn", parent=s["body"], textColor=OCHRE_INK)
    return s


def _p(text, style):
    return Paragraph(escape(str(text)), style)


def _table(rows, widths, header=None, accent=SLATE, styles=None):
    data = ([header] if header else []) + rows
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [("FONT", (0, 0), (-1, -1), "Helvetica", 10), ("TEXTCOLOR", (0, 0), (-1, -1), INK),
             ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
             ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), accent), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                  ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 10)]
    t.setStyle(TableStyle(style + (styles or [])))
    return t


def _money(value) -> str:
    return f"{value:,.2f}"


def _category_rows(cleaned: pd.DataFrame, mapping: dict):
    cat = next((c for c in cleaned.columns if c.lower() == "category"), None)
    if cat is None:
        return None
    amount = cleaned[mapping["amount"]]
    kept = ~cleaned["amount_outlier_flag"].astype(bool)
    rows = []
    for name, group in cleaned.groupby(cat, dropna=True):
        rows.append([str(name), _money(amount[group.index].sum()), _money(amount[group.index][kept[group.index]].sum())])
    rows.sort(key=lambda r: r[0].lower())
    shown, extra = rows[:MAX_CATEGORY_ROWS], len(rows) - MAX_CATEGORY_ROWS
    none_mask = cleaned[cat].isna()
    if none_mask.any():
        shown.append(["No category", _money(amount[none_mask].sum()), _money(amount[none_mask & kept].sum())])
    shown.append(["Total", _money(amount.sum()), _money(amount[kept].sum())])
    return shown, max(extra, 0)


def write_pdf(path, job: dict, cleaned: pd.DataFrame, mapping: dict) -> None:
    s = _styles()
    issues = job.get("issues", [])
    count = {i["type"]: i["count"] for i in issues}
    out_rows = next((st["rows_out"] for st in job["stages"] if st["name"] == "clean"), None)
    story = [_p("Data quality report", s["title"]), _p(job["input"]["filename"], s["muted"]), Spacer(1, 10)]

    head = Table([[Paragraph(f"{job['quality_score']}<font size=14 color='#5C5850'> / 100</font>", s["score"]),
                   [_p(f"Rows in: {job['input']['rows']:,}", s["body"]),
                    _p(f"Rows after cleaning: {out_rows:,}" if out_rows is not None else "", s["body"]),
                    _p(f"Slash dates read {ORDER[job['date_order']]} ({SOURCE[job['date_order_source']]})", s["muted"])]]],
                colWidths=[60 * mm, 110 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [head, _p("Quality score: the share of input rows with a valid date, a valid amount, and no flag.", s["muted"])]

    if count.get("ambiguous_dates"):
        other = "day" if job["date_order"] == "month" else "month"
        note = (f"{count['ambiguous_dates']:,} dates such as 03/04/23 could be read two ways and were read "
                f"{ORDER[job['date_order']]}. If your file uses {ORDER[other]} dates, re-run with that order.")
        box = Table([[_p(note, s["warn"])]], colWidths=[170 * mm])
        box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), OCHRE_BG), ("BOX", (0, 0), (-1, -1), 0.5, OCHRE_LINE),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        story += [Spacer(1, 10), box]

    fixed = [[_p(label, s["body"]), f"{count[t]:,}"] for t, label in FIXED.items() if count.get(t)]
    story.append(_p("What we fixed", s["h2"]))
    if fixed:
        story.append(_table(fixed, [140 * mm, 30 * mm]))
    story.append(_p("Dates, amounts, and text were made consistent.", s["muted"]))

    check = [[_p(label, s["body"]), f"{count[t]:,}"] for t, label in CHECK.items() if count.get(t)]
    if check:
        story += [_p("Please check these", s["h2"]),
                  _table(check, [140 * mm, 30 * mm], accent=OCHRE_INK, styles=[("BACKGROUND", (0, 0), (-1, -1), OCHRE_BG)])]

    cats = _category_rows(cleaned, mapping)
    if cats:
        rows, extra = cats
        story += [_p("Totals by category", s["h2"]),
                  _table([[_p(r[0], s["body"]), r[1], r[2]] if r[0] != "Total" else r for r in rows],
                         [80 * mm, 45 * mm, 45 * mm], header=["Category", "All rows", "Excluding flagged"],
                         styles=[("FONT", (0, len(rows)), (-1, len(rows)), "Helvetica-Bold", 10)])]
        if extra:
            story.append(_p(f"{extra} more categories are listed in the Excel report.", s["muted"]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(20 * mm, 12 * mm, f"Generated {date.today().isoformat()}. Full detail is in the Excel report.")
        canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, f"Page {doc.page}")
        canvas.restoreState()

    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                      bottomMargin=20 * mm, title="Data quality report", author="Report generator").build(
        story, onFirstPage=footer, onLaterPages=footer)
