#!/usr/bin/env python3
"""LLM dual-agent sweep — re-extract every filing we have on disk
with the multi-period Agent A prompt, so prior-period comparatives
land in the DB as `llm-dual-agent-restated@0.1.0` rows alongside
the primary period.

Replaces `sweep_xbrl_restatements.py` (deleted). See
`docs/RESTATEMENT_POLICY.md` for the approach.

Usage:
    # Dry-run, print Agent A output for one ticker+metric
    python scripts/sweep_llm_restatements.py \\
        --ticker MSFT --metric cloud_segment_revenue --validate-only

    # Commit for one ticker+metric
    python scripts/sweep_llm_restatements.py \\
        --ticker MSFT --metric cloud_segment_revenue

    # Full known-restater sweep
    python scripts/sweep_llm_restatements.py --all-known-restaters
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from capex.adapters.cli_backend import CLIBackend
from capex.db import Database
from capex.extract.extractors.llm_headless import LLMHeadlessExtractor
from capex.extract.writer import write_extractions

KNOWN_RESTATERS = ("MSFT", "ORCL", "BABA", "BIDU", "GDS")
DEFAULT_METRICS = (
    "cloud_segment_revenue",
    "revenue",
    "capital_expenditures",
    "operating_cash_flow",
    "depreciation_amortization",
    "property_plant_equipment_net",
)


def _list_filings(db: Database, ticker: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, form_type, filing_date, period_of_report,
                   fiscal_year, raw_path
            FROM source_documents
            WHERE ticker = ? AND raw_path LIKE 'data/_sources/%'
            ORDER BY filing_date ASC
            """,
            (ticker,),
        ).fetchall()
    return [dict(r) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--metric", default=None)
    ap.add_argument("--metrics", default=None,
                    help="comma list; overrides --metric")
    ap.add_argument("--all-known-restaters", action="store_true",
                    help="Ticker scope = known_restaters list.")
    ap.add_argument("--validate-only", action="store_true",
                    help="Print Agent A output; do not write to DB.")
    args = ap.parse_args()

    if args.all_known_restaters:
        tickers = list(KNOWN_RESTATERS)
    elif args.ticker:
        tickers = [args.ticker]
    else:
        print("must pass --ticker T or --all-known-restaters", file=sys.stderr)
        return 2

    if args.metrics:
        metrics = [m.strip() for m in args.metrics.split(",") if m.strip()]
    elif args.metric:
        metrics = [args.metric]
    else:
        metrics = list(DEFAULT_METRICS)

    try:
        backend = CLIBackend.auto()
    except Exception as e:
        print(f"LLM backend unavailable: {e}", file=sys.stderr)
        return 2

    db = Database()
    extractor = LLMHeadlessExtractor()
    total_primary = 0
    total_restated = 0
    total_filings = 0
    for ticker in tickers:
        filings = _list_filings(db, ticker)
        print(f"\n=== {ticker}: {len(filings)} filings on disk ===")
        for f in filings:
            for metric in metrics:
                total_filings += 1
                print(
                    f"  [{f['filing_date']}] {ticker} {f['form_type']:5s} "
                    f"por={f['period_of_report']} / metric={metric}"
                )
                try:
                    candidates = extractor.extract(
                        ticker=ticker, metric_key=metric,
                        period=f["period_of_report"],
                        form_type=f["form_type"],
                        backend=backend, db=db,
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"    error: {e}")
                    continue
                if not candidates:
                    print("    (no verified periods)")
                    continue
                primary = sum(
                    1 for c in candidates
                    if c.extracting_model == "llm-dual-agent"
                )
                restated = sum(
                    1 for c in candidates
                    if c.extracting_model == "llm-dual-agent-restated@0.1.0"
                )
                print(
                    f"    → {primary} primary + {restated} restated"
                )
                if args.validate_only:
                    for c in candidates:
                        print(
                            f"      • {c.extracting_model}: "
                            f"value={c.value}  usd={getattr(c, 'value_usd', '—')}  "
                            f"{c.value_text[:80]}"
                        )
                    continue
                result_dicts = [c.to_writer_dict() for c in candidates]
                summary = write_extractions(result_dicts, db=db)
                total_primary += primary
                total_restated += restated
                if summary.get("errors"):
                    for err in summary["errors"]:
                        print(f"    writer error: {err}")

    print(
        f"\n=== TOTAL — primary: {total_primary} restated: "
        f"{total_restated} across {total_filings} filing×metric calls "
        f"{'(validate-only)' if args.validate_only else ''} ==="
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
