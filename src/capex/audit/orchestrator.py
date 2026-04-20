"""Orchestrator — walks the expected-cell universe, runs all checks,
produces the CellRecord list that `report.write_report` consumes."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import checks as audit_checks
from .report import METRIC_NAMES, CellRecord

# Short alias for readability.
C = audit_checks  # noqa: N816

REPO_ROOT = Path(__file__).resolve().parents[3]

# Company fiscal year end month fallback (used for universe generation).
DEFAULT_PERIODS = ["Q1", "Q2", "Q3", "Q4", "H1", "9M", "FY"]
ANNUAL_ONLY_PERIODS = ["FY"]
SEMI_ANNUAL_PERIODS = ["H1", "H2", "FY"]


def build_universe(
    conn: sqlite3.Connection,
    start_year: int = 2015,
    end_year: int = 2025,
    ticker_filter: set[str] | None = None,
    metric_filter: set[str] | None = None,
) -> list[tuple[str, int, str, str]]:
    """Return list of (ticker, fiscal_year, metric_key, period_type) tuples
    that SHOULD exist given coverage.yaml configuration."""
    coverage = C.load_coverage() or {}
    companies = (coverage.get("companies") or {})
    datasets = coverage.get("datasets") or {}
    cloud_ds = datasets.get("cloud_segment_revenue") or {}
    cloud_excluded = {
        e.get("ticker") for e in (cloud_ds.get("companies_excluded") or [])
        if isinstance(e, dict)
    }

    out: list[tuple[str, int, str, str]] = []
    for ticker, cfg in companies.items():
        if ticker_filter and ticker not in ticker_filter:
            continue
        coverage_start = cfg.get("coverage_start", "2015-01-01")
        try:
            start_y = max(start_year, int(coverage_start[:4]))
        except ValueError:
            start_y = start_year
        qconv = (cfg.get("quarterly_convention") or {}).get("default", "")
        if qconv == "semi_annual":
            periods = SEMI_ANNUAL_PERIODS
        elif qconv in ("three_month_column", "ytd_cumulative",
                       "standalone_quarterly"):
            periods = DEFAULT_PERIODS
        else:
            periods = ANNUAL_ONLY_PERIODS
        for metric in METRIC_NAMES:
            if metric_filter and metric not in metric_filter:
                continue
            if metric == "cloud_segment_revenue" and ticker in cloud_excluded:
                continue
            if ticker == "0700" and metric in (
                "depreciation_amortization",
                "property_plant_equipment_net",
            ):
                continue
            for fy in range(start_y, end_year + 1):
                for pt in periods:
                    out.append((ticker, fy, metric, pt))
    return out


def load_cells(
    conn: sqlite3.Connection,
    universe: list[tuple[str, int, str, str]],
) -> dict[tuple[str, int, str, str], dict[str, Any]]:
    """Load extraction details for every cell in the universe.
    Returns {cell_key: row_dict_or_None}.

    Precedence: (a) non-derived beats reconcile-derived; (b) for
    otherwise-equal rows, newest `source_documents.filing_date` wins
    so a restated comparative from a later filing supersedes the
    original as-reported value. See docs/RESTATEMENT_POLICY.md.
    """
    out: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    rows = conn.execute(
        """
        SELECT e.id AS extraction_id, e.value, e.value_usd,
               e.reporting_currency, e.fx_rate,
               e.period_type, e.extracting_model, e.extraction_type,
               e.locator_section, e.quote, e.basis_period_months,
               sd.ticker, sd.fiscal_year, sd.period_of_report,
               sd.form_type, sd.filing_date, sd.source_url,
               sd.id AS source_document_id,
               (SELECT ev.excerpt_text FROM extraction_evidence ev
                WHERE ev.extraction_id = e.id
                  AND ev.excerpt_role = 'primary_value'
                LIMIT 1) AS evidence_quote,
               e.metric_key
        FROM extractions e
        JOIN source_documents sd ON e.source_document_id = sd.id
        WHERE e.period_type != ''
        """
    ).fetchall()

    def _is_derived(row) -> bool:
        return (row.get("extracting_model") or "").startswith("reconcile-derived")

    def _newer(a: dict, b: dict) -> bool:
        """True iff `a` should beat `b` in the selector."""
        a_derived = _is_derived(a)
        b_derived = _is_derived(b)
        if a_derived != b_derived:
            return b_derived   # non-derived > derived
        # tie on type → newer filing wins; fallback: newer source_document id
        af = a.get("filing_date") or ""
        bf = b.get("filing_date") or ""
        if af != bf:
            return af > bf
        return (a.get("source_document_id") or 0) > (b.get("source_document_id") or 0)

    for r in rows:
        key = (r["ticker"], r["fiscal_year"], r["metric_key"], r["period_type"])
        row = dict(r)
        existing = out.get(key)
        if existing is None or _newer(row, existing):
            out[key] = row
    return out


def source_doc_exists(
    conn: sqlite3.Connection, ticker: str, fy: int, period_type: str,
) -> bool:
    """Heuristic: is there ANY source_document we could extract from?"""
    if period_type == "FY":
        forms = ("10-K", "20-F", "HK-AR")
    elif period_type in ("Q1", "Q2", "Q3", "H1", "9M", "3M_reported"):
        forms = ("10-Q", "6-K", "HK-IR")
    elif period_type == "Q4":
        forms = ("10-K", "20-F", "HK-AR", "6-K")
    else:
        forms = ("10-K", "10-Q", "20-F", "6-K", "HK-AR", "HK-IR")
    placeholders = ",".join("?" * len(forms))
    r = conn.execute(
        f"SELECT 1 FROM source_documents WHERE ticker=? AND fiscal_year=? "
        f"AND form_type IN ({placeholders}) LIMIT 1",
        (ticker, fy, *forms),
    ).fetchone()
    return r is not None


def _classify(results: list[C.CheckResult], row: dict | None) -> str:
    if row is None:
        # Gap — classification based on source_doc availability
        gap_res = next((r for r in results if r.check_name == "gap"), None)
        if gap_res:
            return gap_res.details.get("status", "gap_unfixable")
        return "gap_unfixable"
    if any(not r.passed for r in results
           if r.check_name not in ("gap",)):
        return "flagged"
    if row.get("extracting_model", "").startswith("reconcile-derived"):
        return "derived"
    return "verified"


def audit_cells(
    conn: sqlite3.Connection,
    start_year: int = 2015, end_year: int = 2025,
    ticker_filter: set[str] | None = None,
    metric_filter: set[str] | None = None,
) -> list[CellRecord]:
    universe = build_universe(
        conn, start_year, end_year, ticker_filter, metric_filter,
    )
    cells = load_cells(conn, universe)

    # Group by (ticker, fy, metric) for identity checks
    groups: dict[tuple[str, int, str], dict[str, float]] = defaultdict(dict)
    for key, row in cells.items():
        ticker, fy, metric, pt = key
        groups[(ticker, fy, metric)][pt] = abs(row.get("value_usd") or 0)

    # Previous-period lookup for continuity check
    series_order: dict[tuple[str, str], list[tuple[int, str, float]]] = defaultdict(list)
    for key, row in cells.items():
        ticker, fy, metric, pt = key
        if pt not in ("Q1", "Q2", "Q3", "Q4"):
            continue
        val = row.get("value_usd")
        if val is None:
            continue
        series_order[(ticker, metric)].append((fy, pt, val))
    for k in series_order:
        series_order[k].sort()

    records: list[CellRecord] = []
    for cell_key in universe:
        ticker, fy, metric, pt = cell_key
        row = cells.get(cell_key)
        present = row is not None
        src_ok = source_doc_exists(conn, ticker, fy, pt)
        results: list[C.CheckResult] = []

        results.append(C.check_gap(ticker, metric, fy, pt, present, src_ok))
        if row is None:
            classification = _classify(results, None)
            records.append(CellRecord(
                ticker=ticker, metric_key=metric, fiscal_year=fy,
                period_type=pt, value_usd=None, extraction_id=None,
                extracting_model=None, classification=classification,
                check_results=results,
            ))
            continue

        val_usd = row.get("value_usd")
        results.append(C.check_identity(groups[(ticker, fy, metric)]))
        results.append(C.check_range(ticker, metric, pt, val_usd or 0))
        # Continuity: find immediately prior period
        prior = _find_prior(series_order, ticker, metric, fy, pt)
        if prior:
            pfy, ppt, pval = prior
            plabel = f"{pfy}{ppt}"
            tlabel = f"{fy}{pt}"
            results.append(C.check_continuity(
                ticker, metric, plabel, pval, tlabel, val_usd or 0,
            ))
        # Cross-source
        results.append(C.check_cross_source(val_usd, row.get("evidence_quote")))
        # Sign
        results.append(C.check_sign(
            ticker, metric, fy, row.get("value") or 0, pt,
        ))
        # Currency
        results.append(C.check_currency(
            row.get("reporting_currency") or "USD",
            row.get("value"), val_usd, row.get("fx_rate"),
        ))
        # Segment def
        results.append(C.check_segment_def(
            ticker, metric,
            row.get("locator_section") or "",
            row.get("quote") or "",
        ))
        # Period-type semantics: compute XBRL duration from basis_period_months
        basis = row.get("basis_period_months")
        if basis:
            dur_days = int(basis) * 30  # approximate
        else:
            dur_days = None
        results.append(C.check_period_type(pt, dur_days))

        classification = _classify(results, row)
        records.append(CellRecord(
            ticker=ticker, metric_key=metric, fiscal_year=fy,
            period_type=pt, value_usd=val_usd,
            extraction_id=row.get("extraction_id"),
            extracting_model=row.get("extracting_model"),
            classification=classification,
            check_results=results,
        ))
    return records


def _find_prior(
    series_order: dict[tuple[str, str], list[tuple[int, str, float]]],
    ticker: str, metric: str, fy: int, pt: str,
) -> tuple[int, str, float] | None:
    if pt not in ("Q1", "Q2", "Q3", "Q4"):
        return None
    series = series_order.get((ticker, metric)) or []
    # Find index of (fy, pt)
    for i, (yfy, ypt, _yv) in enumerate(series):
        if yfy == fy and ypt == pt:
            if i == 0:
                return None
            return series[i - 1]
    return None
