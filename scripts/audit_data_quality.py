#!/usr/bin/env python3
"""Data-quality audit CLI.

Usage:
    python scripts/audit_data_quality.py                 # dry-run, writes report
    python scripts/audit_data_quality.py --apply         # apply mechanical fixes
    python scripts/audit_data_quality.py --with-llm      # + LLM re-verify flagged
    python scripts/audit_data_quality.py --ticker AMZN   # scope to one ticker
    python scripts/audit_data_quality.py --metric revenue
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from capex.audit import fixes, orchestrator, report, restatement
from capex.db import Database


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Apply mechanical fixes (default: dry-run).")
    ap.add_argument("--with-llm", action="store_true",
                    help="Run LLM re-verification on flagged items.")
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--metric", default=None)
    ap.add_argument("--start-year", type=int, default=2015)
    ap.add_argument("--end-year", type=int, default=2025)
    ap.add_argument("--output", default=str(
        REPO_ROOT / "output" / "data_quality_report.md"))
    args = ap.parse_args()

    run_id = datetime.now(timezone.utc).strftime("audit-%Y%m%d-%H%M%S")
    db = Database()

    if args.apply:
        # Safety backup
        import shutil
        bak = db.path.parent / f"{db.path.name}.pre_audit.bak"
        shutil.copy2(db.path, bak)
        print(f"backup: {bak}")

    with db.connect() as conn:
        conn.row_factory = sqlite3.Row
        ticker_filter = {args.ticker} if args.ticker else None
        metric_filter = {args.metric} if args.metric else None
        print(f"Building cell universe ({args.start_year}-{args.end_year})...")
        cells = orchestrator.audit_cells(
            conn, args.start_year, args.end_year,
            ticker_filter, metric_filter,
        )

    counts = {}
    for c in cells:
        counts[c.classification] = counts.get(c.classification, 0) + 1
    print(f"Universe: {len(cells)} cells")
    for k, v in counts.items():
        print(f"  {k}: {v}")

    applied: list[dict] = []
    if counts.get("flagged", 0) > 0 or counts.get("gap_fixable", 0) > 0:
        applied = fixes.apply_fixes(cells, run_id, apply=args.apply)

    # Record audit_verdicts rows (always — provides the trail)
    if args.apply:
        fixes.record_verdicts(cells, run_id, applied, db)

    if args.with_llm and counts.get("flagged", 0) > 0:
        try:
            from capex.audit import llm_reverify
            print("Running LLM re-verification...")
            llm_reverify.reverify(cells, run_id=run_id, apply=args.apply)
        except Exception as exc:
            print(f"LLM re-verify unavailable: {exc}", file=sys.stderr)

    # Restatement detection — always on. Reads each ticker's latest
    # annual filing and surfaces any period value that differs from
    # what's in the DB. With --apply, writes back the restated values
    # so the filing_date DESC selector promotes them on next read.
    print("Running restatement reporter (observational only)...")
    rsum = restatement.detect(db=db)
    print(f"  Restatement findings: {rsum.total}")

    output = Path(args.output)
    report.write_report(cells, applied, run_id, output,
                       restatement_summary=rsum)
    print(f"report: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
