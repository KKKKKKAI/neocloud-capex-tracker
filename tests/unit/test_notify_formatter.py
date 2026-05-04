"""HTML + plain-text + subject-line generator."""
from __future__ import annotations

from capex.notify.formatter import (
    FilingContext,
    build_html,
    build_subject,
    build_text,
)
from capex.notify.performance import CellSnapshot, PerformanceTriple


def _triple(metric_key: str, current: float, prior_q: float | None,
            prior_y: float | None) -> PerformanceTriple:
    return PerformanceTriple(
        metric_key=metric_key,
        current=CellSnapshot(value=current, period_type="Q1",
                             fiscal_year=2026, period_label="Q1 FY2026"),
        prior_qtr=CellSnapshot(value=prior_q, period_type="Q4",
                               fiscal_year=2025, period_label="Q4 FY2025")
        if prior_q is not None else None,
        prior_year=CellSnapshot(value=prior_y, period_type="Q1",
                                fiscal_year=2025, period_label="Q1 FY2025")
        if prior_y is not None else None,
    )


def _ctx() -> FilingContext:
    return FilingContext(
        ticker="GOOGL", company_name="Alphabet Inc.",
        form_type="10-Q", period_of_report="2026-03-31",
        filing_date="2026-04-30",
        source_url="https://www.sec.gov/Archives/edgar/x.htm",
        period_label="Q1 FY2026",
        performances=[
            _triple("revenue", 109896, 113828, 98000),
            _triple("capital_expenditures", 35674, 27851, 14400),
            _triple("cloud_segment_revenue", 20028, 17664, 12260),
        ],
    )


def test_subject_includes_ticker_period_form_revenue_yoy_qoq():
    s = build_subject(_ctx())
    assert "GOOGL" in s
    assert "Q1 FY2026" in s
    assert "10-Q" in s
    assert "$109.9B" in s   # revenue rendered in B
    assert "+12.1% YoY" in s
    assert "-3.5% QoQ" in s


def test_subject_handles_no_revenue():
    ctx = _ctx()
    ctx.performances = [_triple("capital_expenditures", 100, 90, 80)]
    s = build_subject(ctx)
    # Falls back to the first performance metric
    assert "GOOGL" in s
    assert "10-Q" in s


def test_html_contains_metric_rows_and_links():
    h = build_html(_ctx())
    # Header
    assert "GOOGL" in h
    assert "Alphabet Inc." in h
    assert "Q1 FY2026" in h
    assert "Filed: 2026-04-30" in h
    assert "View on SEC EDGAR" in h
    # Each metric label and current value present
    assert "Revenue" in h
    assert "$109,896M" in h
    assert "Capital Expenditure" in h
    assert "$35,674M" in h
    assert "Cloud Segment Revenue" in h
    assert "$20,028M" in h
    # Prior quarter and prior year labels
    assert "Q4 FY2025" in h
    assert "Q1 FY2025" in h
    # Delta colors (positive green, negative red)
    assert "#1a7f37" in h     # green for +12.1% YoY
    assert "#cf222e" in h     # red for -3.5% QoQ
    # CTA links
    assert "Open dashboard" in h
    assert "Excel workbook" in h


def test_text_mirrors_html_content():
    t = build_text(_ctx())
    assert "GOOGL" in t
    assert "Alphabet Inc." in t
    assert "Q1 FY2026" in t
    assert "$109,896M" in t
    assert "+12.1%" in t   # YoY positive
    assert "-3.5%" in t    # QoQ negative
    assert "Q4 FY2025" in t
    assert "Q1 FY2025" in t


def test_html_handles_missing_prior_year():
    ctx = _ctx()
    ctx.performances = [_triple("revenue", 100, 90, None)]
    h = build_html(ctx)
    # Missing prior year column shows em-dash
    assert "—" in h


def test_html_escapes_company_name():
    ctx = _ctx()
    ctx.company_name = "Acme & Co. <Test>"
    h = build_html(ctx)
    assert "&amp;" in h
    assert "&lt;Test&gt;" in h
