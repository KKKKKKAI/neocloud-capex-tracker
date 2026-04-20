"""Restated-comparative extractor.

Walks the latest filings for each tracked company and pulls any
period values whose value differs materially from what's currently
in the DB. A "restated" value from a later filing supersedes the
original as-reported value per the policy in
`docs/RESTATEMENT_POLICY.md`.

Three sources of restatements this module captures:

1. **XBRL companyfacts** — the `capex.xbrl.timeseries` module was
   extended to surface both the original and the restated value per
   `(end_date, form)` bucket. No additional code here; already
   handled upstream.

2. **10-K / 20-F segment tables (annual restatements)** — a typical
   segment-reporting table in an annual filing lists three fiscal
   years (current + two prior). The prior-year rows are restated
   comparatives. We call `extract.segment.extract_segment_revenue`
   on the latest annual filing and surface any non-current-year
   values as restatement candidates for their `FY` cell.

3. **Manual / ad-hoc hooks** — for sources like MSFT's supplemental
   quarterly-segment 8-K, a separate targeted extractor can be
   plugged in later. Out of scope for MVP.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..coverage import get_dataset_treatment
from ..segment import extract_segment_revenue

REPO_ROOT = Path(__file__).resolve().parents[4]


@dataclass
class RestatedCandidate:
    """A potential restatement found by comparing a later filing to DB."""
    ticker: str
    metric_key: str
    fiscal_year: int
    period_type: str           # "FY" for the annual segment-table path
    restated_value_usd: float
    existing_value_usd: float | None
    existing_extraction_id: int | None
    delta_pct: float           # |restated - existing| / max(|existing|, 1)
    source_document_id: int
    source_filing_date: str
    source_url: str
    segment_name: str
    table_context: str = ""


def _latest_annual_source_doc(
    conn: sqlite3.Connection, ticker: str,
) -> dict[str, Any] | None:
    """Return the most recent 10-K / 20-F source document for a ticker."""
    row = conn.execute(
        """
        SELECT id, raw_path, form_type, filing_date, period_of_report,
               fiscal_year, source_url
        FROM source_documents
        WHERE ticker = ?
          AND form_type IN ('10-K', '20-F', 'HK-AR')
          AND raw_path LIKE 'data/_sources/%'
        ORDER BY filing_date DESC
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    return dict(row) if row else None


def _existing_annual_value_usd(
    conn: sqlite3.Connection,
    ticker: str, metric_key: str, fiscal_year: int,
) -> tuple[float | None, int | None]:
    """Return (value_usd, extraction_id) for the currently-winning row."""
    row = conn.execute(
        """
        SELECT e.id, e.value_usd
        FROM extractions e
        JOIN source_documents sd ON e.source_document_id = sd.id
        WHERE sd.ticker = ? AND e.metric_key = ? AND e.period_type = 'FY'
          AND sd.fiscal_year = ?
        ORDER BY sd.filing_date DESC, e.extracted_at DESC
        LIMIT 1
        """,
        (ticker, metric_key, fiscal_year),
    ).fetchone()
    if not row:
        return (None, None)
    return (row["value_usd"], row["id"])


def detect_annual_restatements(
    conn: sqlite3.Connection,
    ticker: str,
    metric_key: str = "cloud_segment_revenue",
    *,
    tolerance: float = 0.005,
) -> list[RestatedCandidate]:
    """Read the latest 10-K / 20-F / HK-AR and emit restatement candidates.

    Parses the segment table in the most recent annual filing, which
    typically lists 2–3 fiscal years of comparative values. For each
    prior-year value whose delta vs the currently-stored value exceeds
    `tolerance`, emits a `RestatedCandidate`.

    MVP caveat: only USD-reporting companies are scanned here. For
    CNY/HKD filers (BABA, BIDU, GDS, 0700) the segment-table values
    are in local currency, but the stored comparison value is in USD —
    without FX-at-period-end conversion we'd produce false positives.
    XBRL-path restatements still flow automatically for those filers
    via `xbrl/timeseries.py`; see docs/RESTATEMENT_POLICY.md (Known
    gaps section).
    """
    # Scope: USD-reporting companies only (FX-aware segment-table
    # restatement detection is a follow-up).
    co_row = conn.execute(
        "SELECT reporting_currency FROM companies WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    if not co_row or (co_row["reporting_currency"] or "").upper() != "USD":
        return []

    treatment = get_dataset_treatment(ticker, metric_key)
    if not treatment or not treatment.segment_names:
        return []
    sd = _latest_annual_source_doc(conn, ticker)
    if not sd:
        return []
    raw_path = sd["raw_path"]
    if not raw_path:
        return []
    filepath = REPO_ROOT / raw_path
    if not filepath.exists():
        return []
    try:
        results = extract_segment_revenue(
            str(filepath), ticker, treatment.segment_names, form_type=sd["form_type"],
        )
    except Exception:
        return []

    candidates: list[RestatedCandidate] = []
    # The source_doc's fiscal_year is the annual we're reading. Prior
    # fiscal years in the same segment table are restated comparatives.
    source_fy = sd["fiscal_year"]
    for r in results:
        fy = r.get("period_year")
        if not isinstance(fy, int):
            continue
        if fy >= source_fy:
            continue  # current year or future — not a restatement
        restated_val = float(r["value"])
        # Compare to what we currently have in the DB for this (fy, FY).
        existing_val_usd, existing_eid = _existing_annual_value_usd(
            conn, ticker, metric_key, fy,
        )
        if existing_val_usd is None:
            # No existing value — write this as a fresh extraction, not
            # a restatement. Let the regular extractor path handle it.
            continue
        denom = max(abs(existing_val_usd), 1.0)
        delta_pct = abs(restated_val - existing_val_usd) / denom
        if delta_pct <= tolerance:
            continue
        candidates.append(RestatedCandidate(
            ticker=ticker,
            metric_key=metric_key,
            fiscal_year=fy,
            period_type="FY",
            restated_value_usd=restated_val,
            existing_value_usd=existing_val_usd,
            existing_extraction_id=existing_eid,
            delta_pct=delta_pct,
            source_document_id=sd["id"],
            source_filing_date=sd["filing_date"],
            source_url=sd["source_url"] or "",
            segment_name=r.get("segment_name", ""),
            table_context=(r.get("table_context") or "")[:400],
        ))
    return candidates
