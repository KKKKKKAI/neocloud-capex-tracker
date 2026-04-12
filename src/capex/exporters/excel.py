"""Excel workbook exporter — the visible deliverable.

Reads the extractions DB and produces an 8-sheet Excel workbook:

    1. Revenue (Annual)      — one row per company, one column per FY
    2. Revenue (Quarterly)   — de-cumulated standalone quarterly values
    3. Capex (Annual)        — same layout as Revenue Annual
    4. Capex (Quarterly)     — de-cumulated standalone quarterly capex
    5. All Metrics (Annual)  — company × metric × year pivot
    6. All Metrics (Quarterly) — same but quarterly
    7. Data Quality          — flags and notes
    8. Metadata              — coverage, FX rates, concepts used

Usage:
    from capex.exporters.excel import export_workbook
    export_workbook("workbook/capex_tracker.xlsx")

    # Or via CLI:
    capex export
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = REPO_ROOT / "workbook" / "capex_tracker.xlsx"

# Metrics that are flow (cumulative YTD in 10-Q, need de-cumulation)
FLOW_METRICS = {
    "capital_expenditures",
    "revenue",
    "operating_cash_flow",
    "depreciation_amortization",
}
# Metrics that are stock (point-in-time, no de-cumulation)
STOCK_METRICS = {"property_plant_equipment_net"}

# Coverage start overrides (filter out pre-restructuring noise)
COVERAGE_START_OVERRIDES = {
    "NBIS": "2024-01-01",  # post-Yandex restructuring
}


def export_workbook(
    output_path: str | Path | None = None,
    db_path: str | Path | None = None,
) -> Path:
    """Generate the Excel workbook from the extractions DB."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError as exc:
        raise ImportError(
            "openpyxl is required for Excel export. "
            "Install with: pip install openpyxl"
        ) from exc

    output_path = Path(output_path or DEFAULT_OUTPUT)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    db_path = db_path or (REPO_ROOT / "data" / "db" / "capex.db")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    wb = Workbook()

    # Remove default sheet
    wb.remove(wb.active)

    # Header style
    header_font = Font(bold=True, size=11)
    header_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    num_fmt = '#,##0'

    # Build the sheets
    s = (header_font, header_fill, num_fmt)
    _build_metric_sheet(wb, conn, "Revenue (Annual)", "revenue", "annual", *s)
    _build_metric_sheet(wb, conn, "Revenue (Quarterly)", "revenue", "quarterly", *s)
    _build_metric_sheet(wb, conn, "Capex (Annual)", "capital_expenditures", "annual", *s)
    _build_metric_sheet(wb, conn, "Capex (Quarterly)", "capital_expenditures", "quarterly", *s)
    _build_all_metrics_sheet(wb, conn, "All Metrics (Annual)", "annual", *s)
    _build_all_metrics_sheet(wb, conn, "All Metrics (Quarterly)", "quarterly", *s)
    _build_data_quality_sheet(wb, conn, header_font, header_fill)
    _build_metadata_sheet(wb, conn, header_font, header_fill)

    wb.save(str(output_path))
    conn.close()
    return output_path


# --------------------------------------------------------------------
# Sheet builders
# --------------------------------------------------------------------


def _build_metric_sheet(wb, conn, sheet_name, metric_key, cadence, hfont, hfill, nfmt):
    """Build a single-metric sheet: rows=companies, cols=periods."""
    ws = wb.create_sheet(sheet_name)

    data = _get_metric_data(conn, metric_key, cadence)
    if not data:
        ws.append(["No data available"])
        return

    # Get all unique periods and companies
    all_periods = sorted(set(p for company_data in data.values() for p in company_data))
    companies = sorted(data.keys())

    # Header row
    headers = ["Company"] + all_periods
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = hfont
        cell.fill = hfill

    # Data rows
    for row_idx, ticker in enumerate(companies, 2):
        ws.cell(row=row_idx, column=1, value=ticker)
        for col_idx, period in enumerate(all_periods, 2):
            val = data[ticker].get(period)
            if val is not None:
                cell = ws.cell(row=row_idx, column=col_idx, value=round(val))
                cell.number_format = nfmt

    # Auto-width for company column
    ws.column_dimensions["A"].width = 12


def _build_all_metrics_sheet(wb, conn, sheet_name, cadence, hfont, hfill, nfmt):
    """Build a sheet with all metrics: rows=company+metric, cols=periods."""
    ws = wb.create_sheet(sheet_name)

    metrics = ["revenue", "capital_expenditures", "operating_cash_flow",
               "depreciation_amortization", "property_plant_equipment_net"]

    all_data = {}
    all_periods = set()
    for mk in metrics:
        mdata = _get_metric_data(conn, mk, cadence)
        for ticker, periods in mdata.items():
            key = (ticker, mk)
            all_data[key] = periods
            all_periods.update(periods.keys())

    all_periods = sorted(all_periods)
    headers = ["Company", "Metric"] + all_periods
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = hfont
        cell.fill = hfill

    row_idx = 2
    for ticker in sorted(set(k[0] for k in all_data)):
        for mk in metrics:
            key = (ticker, mk)
            if key not in all_data:
                continue
            ws.cell(row=row_idx, column=1, value=ticker)
            ws.cell(row=row_idx, column=2, value=mk)
            for col_idx, period in enumerate(all_periods, 3):
                val = all_data[key].get(period)
                if val is not None:
                    cell = ws.cell(row=row_idx, column=col_idx, value=round(val))
                    cell.number_format = nfmt
            row_idx += 1

    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 30


def _make_font(bold=False, size=11):
    from openpyxl.styles import Font
    return Font(bold=bold, size=size)


def _build_data_quality_sheet(wb, conn, hfont, hfill):
    """Build data quality flags sheet."""
    ws = wb.create_sheet("Data Quality")

    headers = ["Company", "Issue", "Details"]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = hfont
        cell.fill = hfill

    flags = []

    # Check for companies with sparse data
    for row in conn.execute("""
        SELECT sd.ticker, COUNT(DISTINCT e.metric_key) as n_metrics,
               COUNT(e.id) as n_extractions,
               MIN(sd.period_of_report) as earliest
        FROM extractions e
        JOIN source_documents sd ON e.source_document_id = sd.id
        GROUP BY sd.ticker
        ORDER BY sd.ticker
    """):
        ticker = row["ticker"]
        if row["n_metrics"] < 5:
            flags.append((ticker, "Missing metrics",
                         f"Only {row['n_metrics']}/5 headline metrics available"))
        if row["n_extractions"] < 10:
            flags.append((ticker, "Sparse data",
                         f"Only {row['n_extractions']} data points (earliest: {row['earliest']})"))

    # NBIS pre-restructuring flag
    flags.append(("NBIS", "Pre-restructuring data",
                 "CIK includes Yandex history. Data before 2024 is pre-restructuring "
                 "and not comparable. Filtered in annual/quarterly sheets."))

    # META cloud exclusion
    flags.append(("META", "No cloud segment",
                 "Meta is an AI infra buyer, not a cloud vendor. "
                 "Excluded from cloud_segment_revenue dataset."))

    for i, (ticker, issue, details) in enumerate(flags, 2):
        ws.cell(row=i, column=1, value=ticker)
        ws.cell(row=i, column=2, value=issue)
        ws.cell(row=i, column=3, value=details)

    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 25
    ws.column_dimensions["C"].width = 80


def _build_metadata_sheet(wb, conn, hfont, hfill):
    """Build metadata sheet with extraction info."""
    ws = wb.create_sheet("Metadata")

    # Summary stats
    ws.cell(row=1, column=1, value="Metadata").font = _make_font(bold=True, size=14)
    ws.cell(row=3, column=1, value="Generated").font = hfont
    ws.cell(row=3, column=2,
            value=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

    total = conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0]
    ws.cell(row=4, column=1, value="Total extractions").font = hfont
    ws.cell(row=4, column=2, value=total)

    n_cos = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM source_documents"
    ).fetchone()[0]
    ws.cell(row=5, column=1, value="Companies").font = hfont
    ws.cell(row=5, column=2, value=n_cos)

    # Per-company coverage
    ws.cell(row=7, column=1, value="Company Coverage").font = _make_font(bold=True, size=12)
    cov_headers = ["Company", "Currency", "Earliest", "Latest", "Extractions", "Source"]
    for col, h in enumerate(cov_headers, 1):
        cell = ws.cell(row=8, column=col, value=h)
        cell.font = hfont
        cell.fill = hfill

    row = 9
    for r in conn.execute("""
        SELECT sd.ticker, c.reporting_currency,
               MIN(sd.period_of_report) as earliest,
               MAX(sd.period_of_report) as latest,
               COUNT(e.id) as n_ext,
               GROUP_CONCAT(DISTINCT e.extracting_model) as models
        FROM extractions e
        JOIN source_documents sd ON e.source_document_id = sd.id
        JOIN companies c ON sd.ticker = c.ticker
        GROUP BY sd.ticker
        ORDER BY sd.ticker
    """):
        ws.cell(row=row, column=1, value=r["ticker"])
        ws.cell(row=row, column=2, value=r["reporting_currency"])
        ws.cell(row=row, column=3, value=r["earliest"])
        ws.cell(row=row, column=4, value=r["latest"])
        ws.cell(row=row, column=5, value=r["n_ext"])
        ws.cell(row=row, column=6, value=r["models"])
        row += 1

    # FX rates used
    row += 1
    ws.cell(row=row, column=1, value="FX Rates Used").font = _make_font(bold=True, size=12)
    row += 1
    fx_headers = ["Currency Pair", "Date", "Rate", "Source"]
    for col, h in enumerate(fx_headers, 1):
        cell = ws.cell(row=row, column=col, value=h)
        cell.font = hfont
        cell.fill = hfill
    row += 1
    for r in conn.execute(
        "SELECT currency_pair, rate_date, rate, source "
        "FROM fx_rates ORDER BY currency_pair, rate_date"
    ):
        ws.cell(row=row, column=1, value=r[0])
        ws.cell(row=row, column=2, value=r[1])
        ws.cell(row=row, column=3, value=round(r[2], 6))
        ws.cell(row=row, column=4, value=r[3])
        row += 1

    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 15
    ws.column_dimensions["C"].width = 15
    ws.column_dimensions["D"].width = 15
    ws.column_dimensions["E"].width = 15
    ws.column_dimensions["F"].width = 25


# --------------------------------------------------------------------
# Data helpers
# --------------------------------------------------------------------


def _get_metric_data(
    conn, metric_key: str, cadence: str
) -> dict[str, dict[str, float]]:
    """Get metric data organized as {ticker: {period: value_usd}}.

    For annual cadence: only FY-end periods (period_token='AR').
    For quarterly: de-cumulates flow metrics to standalone quarterly.
    Uses value_usd for cross-company comparison.
    """
    is_flow = metric_key in FLOW_METRICS

    if cadence == "annual":
        return _get_annual_data(conn, metric_key)
    else:
        return _get_quarterly_data(conn, metric_key, is_flow)


def _get_annual_data(
    conn, metric_key: str
) -> dict[str, dict[str, float]]:
    """Get annual (FY-end) data for a metric."""
    result: dict[str, dict[str, float]] = {}

    # Get FYE months
    fye_months = {}
    for r in conn.execute("SELECT ticker, fiscal_year_end_month FROM companies"):
        fye_months[r[0]] = r[1]

    # Deduplicate: same logic as quarterly — one row per (ticker, period)
    rows = conn.execute("""
        SELECT sd.ticker, sd.period_of_report, sd.period_token,
               sd.fiscal_year, e.value_usd, e.value,
               e.reporting_currency
        FROM extractions e
        JOIN source_documents sd ON e.source_document_id = sd.id
        WHERE e.metric_key = ?
        AND sd.period_token = 'AR'
        AND e.id = (
            SELECT e2.id FROM extractions e2
            JOIN source_documents sd2 ON e2.source_document_id = sd2.id
            WHERE sd2.ticker = sd.ticker
            AND sd2.period_of_report = sd.period_of_report
            AND e2.metric_key = e.metric_key
            AND sd2.period_token = 'AR'
            ORDER BY e2.value_usd DESC NULLS LAST, e2.extracted_at DESC
            LIMIT 1
        )
        ORDER BY sd.ticker, sd.period_of_report
    """, (metric_key,)).fetchall()

    for r in rows:
        ticker = r["ticker"]
        period = r["period_of_report"]
        fy = r["fiscal_year"]

        # Apply coverage start filter
        start = COVERAGE_START_OVERRIDES.get(ticker)
        if start and period < start:
            continue

        # Filter to actual FYE month
        fye_m = fye_months.get(ticker, 12)
        period_month = int(period.split("-")[1])
        if period_month != fye_m:
            continue

        val = r["value_usd"] if r["value_usd"] is not None else r["value"]
        if val is None:
            continue

        # Use fiscal year as the column label
        label = f"FY{fy}"
        result.setdefault(ticker, {})[label] = abs(val)  # capex is sometimes negative

    return result


def _get_quarterly_data(
    conn, metric_key: str, is_flow: bool
) -> dict[str, dict[str, float]]:
    """Get quarterly data, de-cumulating flow metrics."""
    result: dict[str, dict[str, float]] = {}

    # Get FYE months
    fye_months = {}
    for r in conn.execute("SELECT ticker, fiscal_year_end_month FROM companies"):
        fye_months[r[0]] = r[1]

    # Deduplicate: for the same (ticker, period_of_report), take only
    # one extraction — prefer the highest value_usd (avoids nulls) and
    # the latest extraction. This prevents duplicate entries (e.g. from
    # both claude-code and xbrl-companyfacts) from breaking de-cumulation.
    rows = conn.execute("""
        SELECT sd.ticker, sd.period_of_report, sd.period_token,
               sd.fiscal_year, sd.form_type,
               e.value_usd, e.value, e.reporting_currency
        FROM extractions e
        JOIN source_documents sd ON e.source_document_id = sd.id
        WHERE e.metric_key = ?
        AND e.id = (
            SELECT e2.id FROM extractions e2
            JOIN source_documents sd2 ON e2.source_document_id = sd2.id
            WHERE sd2.ticker = sd.ticker
            AND sd2.period_of_report = sd.period_of_report
            AND e2.metric_key = e.metric_key
            ORDER BY e2.value_usd DESC NULLS LAST, e2.extracted_at DESC
            LIMIT 1
        )
        ORDER BY sd.ticker, sd.fiscal_year, sd.period_of_report
    """, (metric_key,)).fetchall()

    # Group by ticker + fiscal year for de-cumulation
    by_ticker_fy: dict[str, dict[int, list]] = {}
    for r in rows:
        ticker = r["ticker"]
        start = COVERAGE_START_OVERRIDES.get(ticker)
        if start and r["period_of_report"] < start:
            continue

        fy = r["fiscal_year"]
        by_ticker_fy.setdefault(ticker, {}).setdefault(fy, []).append(r)

    for ticker, fy_data in by_ticker_fy.items():
        for _fy, points in fy_data.items():
            points.sort(key=lambda x: x["period_of_report"])

            # Skip fiscal years that have ONLY an AR entry — those are
            # annual-only data that can't be decomposed into quarters.
            tokens = {p["period_token"] for p in points}
            if tokens == {"AR"}:
                continue

            prev_val = 0
            for p in points:
                val = p["value_usd"] if p["value_usd"] is not None else p["value"]
                if val is None:
                    continue
                val = abs(val)

                token = p["period_token"]
                period = p["period_of_report"]

                if is_flow:
                    # De-cumulate
                    if token == "Q1":
                        quarterly_val = val
                        prev_val = val
                    elif token in ("Q2", "Q3"):
                        quarterly_val = val - prev_val
                        prev_val = val
                    elif token == "AR":
                        quarterly_val = val - prev_val
                        prev_val = 0
                    else:
                        quarterly_val = val
                else:
                    # Stock metric — use as-is
                    quarterly_val = val

                # Label: "2024Q1", "2024Q2", etc.
                label = f"{period[:4]}Q{_quarter_from_token(token)}"
                result.setdefault(ticker, {})[label] = quarterly_val

    return result


def _quarter_from_token(token: str) -> str:
    mapping = {"Q1": "1", "Q2": "2", "Q3": "3", "AR": "4", "H1": "2", "H2": "4"}
    return mapping.get(token, "?")
