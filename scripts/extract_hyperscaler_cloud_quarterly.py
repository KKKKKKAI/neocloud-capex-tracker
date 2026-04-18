#!/usr/bin/env python3
"""Extract quarterly cloud segment revenue from hyperscaler 10-Q filings.

Iterates data/_sources/<TICKER>/_raw/*10-Q*.htm for AMZN / MSFT / GOOGL /
ORCL, runs `extract_segment_quarterly`, and writes results to the DB as
`metric_key='cloud_segment_revenue'` with `extracting_model='segment-quarterly@0.1.0'`.

Writes:
- Q1 values for Q1 10-Qs (from 2-value segment tables)
- Q2 + H1 for Q2 10-Qs (current-year 3M and 6M YTD)
- Q3 + 9M for Q3 10-Qs (current-year 3M and 9M YTD)

After this script, run `capex reconcile --metric cloud_segment_revenue`
to derive Q4 via identity FY − 9M.

Usage:
    python scripts/extract_hyperscaler_cloud_quarterly.py [--dry-run]
                                                          [--tickers AMZN,MSFT,GOOGL,ORCL]
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from capex.db import Database
from capex.extract.segment_quarterly import extract_segment_quarterly
from capex.fx.rates import normalize_to_usd

# Per-company segment name candidates (tried in order).
SEGMENT_NAMES = {
    "AMZN": ["AWS"],
    "MSFT": ["Intelligent Cloud"],
    "GOOGL": ["Google Cloud"],
    "ORCL": [
        "Cloud Services and License Support",
        "Cloud Services",
        "Cloud",
    ],
}

ACTOR = "segment-quarterly@0.1.0"
FILENAME_RE = re.compile(
    r"\[(\d{4}\.\d{2}\.\d{2})\]\[(\w+)\]\[(Q[1-3])\]\[10-Q\]\.htm$"
)


def _parse_filename(path: str) -> tuple[str, str, str] | None:
    """Return (filing_date, ticker, period_token) or None."""
    m = FILENAME_RE.search(path)
    if not m:
        return None
    filing_date = m.group(1).replace(".", "-")
    return filing_date, m.group(2), m.group(3)


def _find_source_doc(
    conn, ticker: str, period_token: str, filing_date: str,
) -> dict | None:
    """Find the source_documents row for a 10-Q filing by filing_date."""
    row = conn.execute(
        """
        SELECT id, period_of_report, fiscal_year
        FROM source_documents
        WHERE ticker = ? AND form_type = '10-Q' AND period_token = ?
          AND filing_date = ?
        LIMIT 1
        """,
        (ticker, period_token, filing_date),
    ).fetchone()
    if row:
        return {"id": row[0], "period_of_report": row[1], "fiscal_year": row[2]}
    # Fallback: match on period_token + a ±60 day filing window
    row = conn.execute(
        """
        SELECT id, period_of_report, fiscal_year, filing_date
        FROM source_documents
        WHERE ticker = ? AND form_type = '10-Q' AND period_token = ?
          AND ABS(JULIANDAY(filing_date) - JULIANDAY(?)) <= 60
        ORDER BY ABS(JULIANDAY(filing_date) - JULIANDAY(?)) ASC
        LIMIT 1
        """,
        (ticker, period_token, filing_date, filing_date),
    ).fetchone()
    if row:
        return {"id": row[0], "period_of_report": row[1], "fiscal_year": row[2]}
    return None


def _basis_months(period_type: str) -> int:
    return {"Q1": 3, "Q2": 3, "Q3": 3, "3M_reported": 3, "H1": 6, "9M": 9}.get(
        period_type, 3
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tickers", default="AMZN,MSFT,GOOGL,ORCL")
    args = ap.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    for t in tickers:
        if t not in SEGMENT_NAMES:
            print(f"unknown ticker (no segment names configured): {t}",
                  file=sys.stderr)
            return 2

    db = Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    pending: list[tuple[dict, dict, list]] = []
    empty_count = 0
    total = 0

    with db.connect() as conn:
        for ticker in tickers:
            raw_dir = REPO_ROOT / "data" / "_sources" / ticker / "_raw"
            filings = sorted(
                glob.glob(str(raw_dir / f"*[{ticker}]*10-Q*.htm"))
            )
            segs = SEGMENT_NAMES[ticker]
            for fp in filings:
                parsed = _parse_filename(fp)
                if not parsed:
                    continue
                filing_date, parsed_ticker, period_token = parsed
                if parsed_ticker != ticker:
                    continue
                total += 1
                sdoc = _find_source_doc(conn, ticker, period_token, filing_date)
                if sdoc is None:
                    print(f"  no source_doc match for {fp}", file=sys.stderr)
                    empty_count += 1
                    continue
                results = extract_segment_quarterly(
                    fp, segs, period_token=period_token,
                )
                if not results:
                    empty_count += 1
                    continue
                pending.append((
                    {"ticker": ticker, "source_document_id": sdoc["id"],
                     "period_of_report": sdoc["period_of_report"],
                     "fiscal_year": sdoc["fiscal_year"],
                     "filing_path": fp, "period_token": period_token},
                    sdoc,
                    results,
                ))

    print(f"total 10-Qs scanned: {total}; extracted: {len(pending)}; empty: {empty_count}")
    if args.dry_run:
        for meta, _sdoc, results in pending[:10]:
            print("  ", meta["ticker"], meta["period_of_report"],
                  [(r.period_type, int(r.value_millions)) for r in results])
        print("dry-run; not committing")
        return 0

    inserted = 0
    skipped = 0
    with db.mutating() as conn:
        for meta, sdoc, results in pending:
            ticker = meta["ticker"]
            for r in results:
                # FX normalize (USD already = USD, but go through rates to
                # populate fx_rate_date uniformly).
                value_usd, fx_rate, fx_date = normalize_to_usd(
                    r.value_millions, "USD", meta["period_of_report"],
                )
                existing = conn.execute(
                    """
                    SELECT id FROM extractions
                    WHERE source_document_id = ? AND metric_key = ?
                      AND extracting_model = ? AND period_type = ?
                    """,
                    (sdoc["id"], "cloud_segment_revenue", ACTOR, r.period_type),
                ).fetchone()
                if existing:
                    skipped += 1
                    continue
                cur = conn.execute(
                    """
                    INSERT INTO extractions (
                        source_document_id, metric_key, value, value_text,
                        unit, quote, locator_page, locator_section,
                        extraction_type, confidence, extracting_model,
                        protocol_version, extracted_at, value_usd, fx_rate,
                        fx_rate_date, reporting_currency, period_type,
                        basis_period_months, reporting_convention
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        sdoc["id"],
                        "cloud_segment_revenue",
                        r.value_millions,
                        f"${r.value_millions:,.0f} million",
                        "USD_millions",
                        r.quote[:240],
                        None,
                        f'Segment table, row "{r.segment_name}"',
                        "direct",
                        None,
                        ACTOR,
                        "0.1.0-draft",
                        now,
                        value_usd,
                        fx_rate,
                        fx_date,
                        "USD",
                        r.period_type,
                        _basis_months(r.period_type),
                        "three_month_column",
                    ),
                )
                row_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO audit_log (ts, actor, action, target_table, target_id, payload)
                    VALUES (?, ?, 'segment_quarterly_extracted', 'extractions', ?, ?)
                    """,
                    (
                        now, ACTOR, row_id,
                        json.dumps({
                            "ticker": ticker,
                            "period_type": r.period_type,
                            "value_millions": r.value_millions,
                            "segment_name": r.segment_name,
                            "filing": Path(meta["filing_path"]).name,
                        }, sort_keys=True),
                    ),
                )
                inserted += 1

    print(f"inserted {inserted} cloud_segment_revenue rows, skipped {skipped} existing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
