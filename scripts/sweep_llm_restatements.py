#!/usr/bin/env python3
"""LLM dual-agent restatement sweep.

By default runs only the **latest annual filing** (10-K / 20-F /
HK-AR) per ticker × metric. The latest annual's segment table
typically carries 2–3 years of restated comparatives, which is the
authoritative view for those periods under the `filing_date DESC`
selector. Each filing takes ~2 LLM calls (1 Agent A + 1 batched
Agent B).

Opt-in flags widen scope when you actually want older or
quarterly restatements:

    --include-quarterlies    add the latest 10-Q / 6-K per ticker
    --full-history           every filing on disk (expensive)

Usage:
    python scripts/sweep_llm_restatements.py \\
        --ticker MSFT --metric cloud_segment_revenue --validate-only

    python scripts/sweep_llm_restatements.py \\
        --ticker MSFT --metric cloud_segment_revenue --yes

    python scripts/sweep_llm_restatements.py --all-known-restaters
"""
from __future__ import annotations

import argparse
import sys
import time
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
ANNUAL_FORMS = ("10-K", "20-F", "HK-AR")
QUARTERLY_FORMS = ("10-Q", "6-K", "HK-IR")
# Wall-clock budget per LLM call in seconds; used for preflight
# estimation only. Tune as measured.
SEC_PER_LLM_CALL = 25


def _filings_for(
    db: Database, ticker: str, *,
    include_quarterlies: bool, full_history: bool,
) -> list[dict]:
    with db.connect() as conn:
        if full_history:
            rows = conn.execute(
                """SELECT id, form_type, filing_date, period_of_report,
                          fiscal_year, raw_path
                   FROM source_documents
                   WHERE ticker=? AND raw_path LIKE 'data/_sources/%'
                   ORDER BY filing_date ASC""",
                (ticker,),
            ).fetchall()
            return [dict(r) for r in rows]
        # Latest annual
        ann = conn.execute(
            """SELECT id, form_type, filing_date, period_of_report,
                      fiscal_year, raw_path
               FROM source_documents
               WHERE ticker=? AND raw_path LIKE 'data/_sources/%'
                 AND form_type IN ('10-K','20-F','HK-AR')
               ORDER BY filing_date DESC LIMIT 1""",
            (ticker,),
        ).fetchone()
        out = [dict(ann)] if ann else []
        if include_quarterlies:
            qtr = conn.execute(
                """SELECT id, form_type, filing_date, period_of_report,
                          fiscal_year, raw_path
                   FROM source_documents
                   WHERE ticker=? AND raw_path LIKE 'data/_sources/%'
                     AND form_type IN ('10-Q','6-K','HK-IR')
                   ORDER BY filing_date DESC LIMIT 1""",
                (ticker,),
            ).fetchone()
            if qtr:
                out.append(dict(qtr))
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--metric", default=None)
    ap.add_argument("--metrics", default=None,
                    help="comma list; overrides --metric")
    ap.add_argument("--all-known-restaters", action="store_true",
                    help="Ticker scope = MSFT/ORCL/BABA/BIDU/GDS.")
    ap.add_argument("--include-quarterlies", action="store_true",
                    help="Also pull the single latest 10-Q/6-K per ticker.")
    ap.add_argument("--full-history", action="store_true",
                    help="Walk every filing on disk (expensive).")
    ap.add_argument("--validate-only", action="store_true",
                    help="Print Agent A output; do not write to DB.")
    ap.add_argument("--yes", action="store_true",
                    help="Skip the cost-preflight confirmation.")
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

    db = Database()

    # ---- Preflight: count filings × metrics, estimate wall-clock ----
    plan: list[tuple[str, dict, str]] = []
    for ticker in tickers:
        filings = _filings_for(
            db, ticker,
            include_quarterlies=args.include_quarterlies,
            full_history=args.full_history,
        )
        for f in filings:
            for metric in metrics:
                plan.append((ticker, f, metric))

    n_calls = 2 * len(plan)     # 1 Agent A + 1 batched Agent B per filing
    est_secs = n_calls * SEC_PER_LLM_CALL
    if args.full_history:
        scope_label = "full-history"
    elif args.include_quarterlies:
        scope_label = "latest annual + latest quarterly"
    else:
        scope_label = "latest annual"
    print("=" * 60)
    print(
        f"SCOPE: {len(tickers)} ticker(s) × {len(metrics)} metric(s). "
        f"{scope_label}."
    )
    print(
        f"  {len(plan)} filing × metric calls = ~{n_calls} LLM invocations, "
        f"est {est_secs // 60}m{est_secs % 60}s wall-clock."
    )
    print("=" * 60)

    if not plan:
        print("nothing to do.")
        return 0

    if not args.validate_only and not args.yes:
        try:
            reply = input("Proceed with commit run? [y/N]: ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            print("aborted.")
            return 0

    try:
        backend = CLIBackend.auto()
    except Exception as e:
        print(f"LLM backend unavailable: {e}", file=sys.stderr)
        return 2

    extractor = LLMHeadlessExtractor()
    total_primary = 0
    total_restated = 0
    t_all = time.time()
    for ticker, f, metric in plan:
        t0 = time.time()
        print(
            f"  [{f['filing_date']}] {ticker} {f['form_type']:5s} "
            f"por={f['period_of_report']} / metric={metric}",
            flush=True,
        )
        try:
            candidates = extractor.extract(
                ticker=ticker, metric_key=metric,
                period=f["period_of_report"],
                form_type=f["form_type"],
                backend=backend, db=db,
            )
        except Exception as e:  # noqa: BLE001
            print(f"    error: {e}", flush=True)
            continue
        dt = time.time() - t0
        if not candidates:
            print(f"    ({dt:.0f}s) no verified candidates", flush=True)
            continue
        n_prim = sum(
            1 for c in candidates if c.extracting_model == "llm-dual-agent"
        )
        n_rest = sum(
            1 for c in candidates
            if c.extracting_model == "llm-dual-agent-restated@0.1.0"
        )
        total_primary += n_prim
        total_restated += n_rest
        print(
            f"    ({dt:.0f}s) {n_prim} primary + {n_rest} restated",
            flush=True,
        )
        for c in candidates:
            print(
                f"      • {c.extracting_model}: "
                f"value={c.value}  {c.value_text[:80]}",
                flush=True,
            )
        if args.validate_only:
            continue
        result_dicts = [c.to_writer_dict() for c in candidates]
        summary = write_extractions(result_dicts, db=db)
        if summary.get("errors"):
            for err in summary["errors"]:
                print(f"    writer error: {err}", flush=True)

    print()
    print(
        f"=== TOTAL — primary: {total_primary} restated: {total_restated} "
        f"across {len(plan)} filing×metric calls "
        f"(elapsed {time.time() - t_all:.0f}s){' validate-only' if args.validate_only else ''} ==="
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
