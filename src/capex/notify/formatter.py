"""HTML + plain-text email body for one (subscriber, filing) notification."""
from __future__ import annotations

import html
from dataclasses import dataclass

from .performance import PerformanceTriple

# The 6 headline metrics in the order they appear in the email table
DEFAULT_METRIC_ORDER = (
    "revenue",
    "capital_expenditures",
    "cloud_segment_revenue",
    "operating_cash_flow",
    "depreciation_amortization",
    "property_plant_equipment_net",
)

METRIC_LABELS = {
    "revenue": "Revenue",
    "capital_expenditures": "Capital Expenditure",
    "cloud_segment_revenue": "Cloud Segment Revenue",
    "operating_cash_flow": "Operating Cash Flow",
    "depreciation_amortization": "D&A",
    "property_plant_equipment_net": "PP&E (net)",
}

DASHBOARD_URL = "https://KKKKKKAI.github.io/neocloud-capex-tracker/"
REPO_WORKBOOK_URL_PREFIX = (
    "https://github.com/KKKKKKAI/neocloud-capex-tracker/tree/main/workbook"
)


@dataclass
class FilingContext:
    """Everything the formatter needs about the filing being announced."""

    ticker: str
    company_name: str
    form_type: str               # "10-Q", "10-K", "20-F", ...
    period_of_report: str        # ISO YYYY-MM-DD
    filing_date: str             # ISO YYYY-MM-DD
    source_url: str | None       # SEC EDGAR / HKEXnews URL
    period_label: str            # "Q1 FY2026"
    performances: list[PerformanceTriple]


def _fmt_value(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"${v:,.0f}M"
    return f"${v:,.1f}M"


def _fmt_delta(pct: float | None) -> str:
    if pct is None:
        return ""
    sign = "+" if pct >= 0 else ""
    return f"({sign}{pct:.1f}%)"


def _delta_color(pct: float | None, *, neutral_metrics: bool = False) -> str:
    if pct is None:
        return "#999"
    # For now: positive = green, negative = red. Capex going up isn't
    # always "good" but for a notification email this is the cleanest
    # signal; the recipient can always click through to the dashboard.
    return "#1a7f37" if pct >= 0 else "#cf222e"


def build_subject(ctx: FilingContext) -> str:
    """`📊 GOOGL Q1 FY2026 10-Q — revenue $109.9B (+12.1% YoY, -3.5% QoQ)`"""
    lead = next(
        (p for p in ctx.performances if p.metric_key == "revenue"),
        ctx.performances[0] if ctx.performances else None,
    )
    if lead is None:
        return f"📊 {ctx.ticker} {ctx.period_label} {ctx.form_type} extracted"
    val = lead.current.value or 0
    val_str = f"${val/1000:.1f}B" if val >= 1000 else f"${val:.0f}M"
    parts = [f"📊 {ctx.ticker} {ctx.period_label} {ctx.form_type} — revenue {val_str}"]
    deltas = []
    if lead.yoy_pct is not None:
        sign = "+" if lead.yoy_pct >= 0 else ""
        deltas.append(f"{sign}{lead.yoy_pct:.1f}% YoY")
    if lead.qoq_pct is not None:
        sign = "+" if lead.qoq_pct >= 0 else ""
        deltas.append(f"{sign}{lead.qoq_pct:.1f}% QoQ")
    if deltas:
        parts.append(f"({', '.join(deltas)})")
    return " ".join(parts)


def build_html(ctx: FilingContext) -> str:
    """Self-contained HTML with inline CSS (Gmail-safe)."""
    rows = []
    for p in ctx.performances:
        label = METRIC_LABELS.get(p.metric_key, p.metric_key)
        prior_q_label = (
            p.prior_qtr.period_label if p.prior_qtr else ""
        )
        prior_y_label = (
            p.prior_year.period_label if p.prior_year else ""
        )
        rows.append(
            f"<tr>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #d0d7de;'>"
            f"{html.escape(label)}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #d0d7de;text-align:right;font-variant-numeric:tabular-nums;'>"
            f"<b>{_fmt_value(p.current.value)}</b></td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #d0d7de;text-align:right;font-variant-numeric:tabular-nums;color:#57606a;'>"
            f"{_fmt_value(p.prior_qtr.value) if p.prior_qtr else '—'} "
            f"<span style='color:{_delta_color(p.qoq_pct)}'>{_fmt_delta(p.qoq_pct)}</span>"
            f"<br><span style='font-size:11px;color:#8b949e;'>{html.escape(prior_q_label)}</span>"
            f"</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #d0d7de;text-align:right;font-variant-numeric:tabular-nums;color:#57606a;'>"
            f"{_fmt_value(p.prior_year.value) if p.prior_year else '—'} "
            f"<span style='color:{_delta_color(p.yoy_pct)}'>{_fmt_delta(p.yoy_pct)}</span>"
            f"<br><span style='font-size:11px;color:#8b949e;'>{html.escape(prior_y_label)}</span>"
            f"</td>"
            f"</tr>"
        )

    source_link = (
        f"<a href='{html.escape(ctx.source_url)}' style='color:#0969da;'>View on SEC EDGAR ↗</a>"
        if ctx.source_url else ""
    )

    return (
        f"<div style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
        f"max-width:680px;color:#1f2328;line-height:1.5;'>"
        f"<h2 style='margin:0 0 4px 0;'>{html.escape(ctx.ticker)} — "
        f"{html.escape(ctx.company_name)}</h2>"
        f"<p style='margin:0 0 16px 0;color:#57606a;'>"
        f"{html.escape(ctx.period_label)} (period ending {html.escape(ctx.period_of_report)})<br>"
        f"Form: {html.escape(ctx.form_type)} · "
        f"Filed: {html.escape(ctx.filing_date)} · {source_link}"
        f"</p>"
        f"<table style='border-collapse:collapse;width:100%;font-size:14px;'>"
        f"<thead><tr style='background:#f6f8fa;'>"
        f"<th style='padding:8px 12px;text-align:left;border-bottom:2px solid #d0d7de;'>Metric</th>"
        f"<th style='padding:8px 12px;text-align:right;border-bottom:2px solid #d0d7de;'>This quarter</th>"
        f"<th style='padding:8px 12px;text-align:right;border-bottom:2px solid #d0d7de;'>Prior quarter</th>"
        f"<th style='padding:8px 12px;text-align:right;border-bottom:2px solid #d0d7de;'>Prior year</th>"
        f"</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        f"<p style='margin:20px 0 8px 0;font-size:14px;'>"
        f"<a href='{DASHBOARD_URL}' style='color:#0969da;text-decoration:none;'>Open dashboard ↗</a>"
        f" &nbsp;·&nbsp; "
        f"<a href='{REPO_WORKBOOK_URL_PREFIX}' style='color:#0969da;text-decoration:none;'>Excel workbook ↗</a>"
        f"</p>"
        f"<p style='margin:16px 0 0 0;font-size:12px;color:#8b949e;'>"
        f"— neocloud-capex-tracker auto-update<br>"
        f"To unsubscribe, edit data/_local/subscribers.yaml on the maintainer's machine."
        f"</p></div>"
    )


def build_text(ctx: FilingContext) -> str:
    """Plain-text mirror, ASCII table."""
    lines = [
        f"{ctx.ticker} — {ctx.company_name}",
        f"{ctx.period_label} (period ending {ctx.period_of_report})",
        f"Form: {ctx.form_type} · Filed: {ctx.filing_date}",
    ]
    if ctx.source_url:
        lines.append(f"Source: {ctx.source_url}")
    lines.append("")
    # Compute column widths
    metric_w = max(len(METRIC_LABELS.get(p.metric_key, p.metric_key))
                   for p in ctx.performances)
    metric_w = max(metric_w, len("Metric"))
    # Header row uses dynamic labels based on the first performance's
    # prior periods, so the reader sees "Prior qtr (Q4 FY2025)" etc.
    pq_label = (
        f"Prior qtr ({ctx.performances[0].prior_qtr.period_label})"
        if ctx.performances and ctx.performances[0].prior_qtr
        else "Prior quarter"
    )
    py_label = (
        f"Prior year ({ctx.performances[0].prior_year.period_label})"
        if ctx.performances and ctx.performances[0].prior_year
        else "Prior year"
    )
    pq_w = max(22, len(pq_label) + 2)
    py_w = max(22, len(py_label) + 2)
    lines.append(
        f"{'Metric'.ljust(metric_w)}   "
        f"{'This quarter'.rjust(14)}   "
        f"{pq_label.rjust(pq_w)}   "
        f"{py_label.rjust(py_w)}"
    )
    lines.append("-" * (metric_w + 14 + pq_w + py_w + 9))
    for p in ctx.performances:
        label = METRIC_LABELS.get(p.metric_key, p.metric_key)
        cur = _fmt_value(p.current.value).rjust(14)
        pq = (
            f"{_fmt_value(p.prior_qtr.value)} {_fmt_delta(p.qoq_pct)}".rjust(pq_w)
            if p.prior_qtr else "—".rjust(pq_w)
        )
        py = (
            f"{_fmt_value(p.prior_year.value)} {_fmt_delta(p.yoy_pct)}".rjust(py_w)
            if p.prior_year else "—".rjust(py_w)
        )
        lines.append(f"{label.ljust(metric_w)}   {cur}   {pq}   {py}")
    lines.extend([
        "",
        f"Dashboard: {DASHBOARD_URL}",
        f"Workbook:  {REPO_WORKBOOK_URL_PREFIX}",
        "",
        "— neocloud-capex-tracker auto-update",
        "To unsubscribe, edit data/_local/subscribers.yaml on the "
        "maintainer's machine.",
    ])
    return "\n".join(lines)
