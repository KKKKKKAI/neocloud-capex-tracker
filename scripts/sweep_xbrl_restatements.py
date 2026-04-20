#!/usr/bin/env python3
"""Bulk XBRL restatement sweep.

For each (ticker, metric) pair in scope, call
`xbrl.timeseries.fetch_concept_timeseries` (which now returns both
the original entries AND every later-filed restatement per
`(start, end, duration)`) and `write_timeseries_to_db` (which now
writes restated rows under `extracting_model='restated-xbrl'` with
`source_document_id` pointing at the restating filing's accession).

The `filing_date DESC` selector in
`exporters/interactive_chart._load_quarterly/_load_annual`,
`audit/orchestrator.load_cells`, and `reconcile._group_rows` will
automatically promote these restated rows on the next chart /
audit / Excel refresh.

Default scope: MSFT, GOOGL, AMZN × (capital_expenditures, revenue,
operating_cash_flow, depreciation_amortization,
property_plant_equipment_net).

Usage:
    python scripts/sweep_xbrl_restatements.py               # dry-run
    python scripts/sweep_xbrl_restatements.py --apply       # commit
    python scripts/sweep_xbrl_restatements.py --tickers MSFT,AMZN
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from capex.db import Database
from capex.extract.extractors.xbrl import CONCEPT_MAP
from capex.xbrl.timeseries import fetch_concept_timeseries, write_timeseries_to_db

DEFAULT_TICKERS = ("MSFT", "GOOGL", "AMZN")
DEFAULT_METRICS = (
    "capital_expenditures",
    "revenue",
    "operating_cash_flow",
    "depreciation_amortization",
    "property_plant_equipment_net",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Write restated rows to DB (default: dry-run).")
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    ap.add_argument("--metrics", default=",".join(DEFAULT_METRICS))
    ap.add_argument("--start-date", default="2015-01-01")
    args = ap.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    metrics = [m.strip() for m in args.metrics.split(",") if m.strip()]

    db = Database()
    with db.connect() as conn:
        companies = {
            r["ticker"]: dict(r) for r in conn.execute(
                "SELECT ticker, edgar_cik, reporting_currency FROM companies"
            )
        }

    totals = {"inserted": 0, "restated": 0, "skipped": 0, "errors": 0}
    by_ticker_metric: list[dict] = []

    for ticker in tickers:
        info = companies.get(ticker)
        if not info or not info.get("edgar_cik"):
            print(f"[skip] {ticker}: no CIK")
            continue
        cik = info["edgar_cik"]
        ccy = info.get("reporting_currency") or "USD"

        for metric in metrics:
            concepts = CONCEPT_MAP.get(metric, [])
            if not concepts:
                continue

            # Merge series from all candidate concepts; same-period
            # entries from different concepts are unlikely to overlap
            # so we just pass them through sequentially.
            all_entries: list[dict] = []
            for concept in concepts:
                try:
                    series = fetch_concept_timeseries(
                        cik=cik, concept=concept,
                        start_date=args.start_date,
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"  [error] {ticker}/{metric}/{concept}: {e}")
                    totals["errors"] += 1
                    continue
                all_entries.extend(series)

            if not all_entries:
                print(f"  [empty] {ticker}/{metric}")
                continue

            restated_in_series = sum(
                1 for p in all_entries if p.get("is_restatement")
            )
            originals = len(all_entries) - restated_in_series

            if not args.apply:
                print(
                    f"  [dry-run] {ticker}/{metric}: "
                    f"{originals} originals + {restated_in_series} restatements "
                    f"from XBRL"
                )
                by_ticker_metric.append({
                    "ticker": ticker, "metric": metric,
                    "originals": originals,
                    "restated": restated_in_series,
                })
                continue

            summary = write_timeseries_to_db(
                all_entries, ticker=ticker, metric_key=metric,
                reporting_currency=ccy, db=db,
            )
            totals["inserted"] += summary.get("inserted", 0)
            totals["restated"] += summary.get("restated", 0)
            totals["skipped"] += summary.get("skipped", 0)
            totals["errors"] += len(summary.get("errors") or [])
            by_ticker_metric.append({
                "ticker": ticker, "metric": metric,
                "inserted": summary.get("inserted", 0),
                "restated": summary.get("restated", 0),
                "skipped": summary.get("skipped", 0),
            })
            print(
                f"  [done]  {ticker:6s}/{metric:30s} "
                f"inserted={summary.get('inserted', 0)}  "
                f"restated={summary.get('restated', 0)}  "
                f"skipped={summary.get('skipped', 0)}"
            )

    print()
    print("=" * 60)
    if args.apply:
        print(
            f"TOTALS — inserted: {totals['inserted']} "
            f"restated: {totals['restated']} "
            f"skipped: {totals['skipped']} "
            f"errors: {totals['errors']}"
        )
    else:
        t_restated = sum(x.get("restated", 0) for x in by_ticker_metric)
        print(f"DRY-RUN TOTAL — {t_restated} restatements would be written")
        print("Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
