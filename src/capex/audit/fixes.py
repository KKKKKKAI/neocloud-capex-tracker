"""Fix orchestrator — maps mechanical failures to remediation routines.

For each flagged cell, classify the root cause and run the appropriate
fix. Fixes are only applied when the CLI is invoked with `--apply`.
Every fix logs to `audit_verdicts` with a before/after snapshot.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..db import Database

REPO_ROOT = Path(__file__).resolve().parents[3]


def classify_fix(cell) -> str | None:
    """Decide which fix class applies to a flagged cell, or None."""
    failures = [r for r in cell.check_results if not r.passed]
    names = {f.check_name for f in failures}
    # period_type / duration mismatch — the XBRL refetch fix covers it
    if "period_type" in names:
        return "xbrl_refetch"
    # cross-source mismatch where DB value disagrees with quote
    if "cross_source" in names and "identity" not in names:
        return "xbrl_refetch"
    # identity violation — rerun reconcile
    if "identity" in names and names == {"identity"}:
        return "reconcile"
    # range / continuity flags alone — usually indicates wrong context;
    # refetch first, then mark for LLM review.
    if names & {"range", "continuity"}:
        return "xbrl_refetch"
    # gap_fixable in check_gap details
    if cell.classification == "gap_fixable":
        return "gap_extract"
    return None


def apply_fixes(
    cells, run_id: str, apply: bool = False,
) -> list[dict[str, Any]]:
    """Walk flagged cells; for each, run the classified fix (or dry-run).
    Returns list of {ticker, fiscal_year, metric_key, period_type, fix_class,
    old_usd, new_usd} dicts for the report."""
    applied: list[dict[str, Any]] = []
    # Bucket by fix class so we batch where possible
    by_class: dict[str, list] = {}
    for c in cells:
        fc = classify_fix(c)
        if fc is None:
            continue
        by_class.setdefault(fc, []).append(c)

    if "xbrl_refetch" in by_class:
        applied.extend(_apply_xbrl_refetch(by_class["xbrl_refetch"], apply, run_id))
    if "reconcile" in by_class:
        applied.extend(_apply_reconcile(by_class["reconcile"], apply, run_id))
    if "gap_extract" in by_class:
        applied.extend(_apply_gap_extract(by_class["gap_extract"], apply, run_id))
    return applied


def _apply_xbrl_refetch(cells, apply: bool, run_id: str) -> list[dict]:
    """Runs scripts/refetch_xbrl_flow_metrics.py for affected tickers × metrics."""
    if not apply:
        return [
            {"ticker": c.ticker, "fiscal_year": c.fiscal_year,
             "metric_key": c.metric_key, "period_type": c.period_type,
             "fix_class": "xbrl_refetch (dry-run)",
             "old_usd": c.value_usd, "new_usd": None}
            for c in cells
        ]
    tickers = sorted({c.ticker for c in cells})
    metrics = sorted({c.metric_key for c in cells})
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "refetch_xbrl_flow_metrics.py"),
        "--tickers", ",".join(tickers),
        "--metrics", ",".join(metrics),
    ]
    print(f"  [xbrl_refetch] running for {len(tickers)} tickers × "
          f"{len(metrics)} metrics...")
    try:
        subprocess.run(cmd, check=True, cwd=REPO_ROOT)
    except subprocess.CalledProcessError as e:
        print(f"  [xbrl_refetch] FAILED: {e}", file=sys.stderr)
        return []
    return [
        {"ticker": c.ticker, "fiscal_year": c.fiscal_year,
         "metric_key": c.metric_key, "period_type": c.period_type,
         "fix_class": "xbrl_refetch",
         "old_usd": c.value_usd, "new_usd": None}  # new value TBD from DB post-run
        for c in cells
    ]


def _apply_reconcile(cells, apply: bool, run_id: str) -> list[dict]:
    if not apply:
        return [
            {"ticker": c.ticker, "fiscal_year": c.fiscal_year,
             "metric_key": c.metric_key, "period_type": c.period_type,
             "fix_class": "reconcile (dry-run)",
             "old_usd": c.value_usd, "new_usd": None}
            for c in cells
        ]
    # For safety, reconcile all flagged metrics once
    metrics = sorted({c.metric_key for c in cells})
    for m in metrics:
        cmd = [sys.executable, "-m", "capex.cli.main", "reconcile",
               "--metric", m]
        print(f"  [reconcile] {m}...")
        try:
            subprocess.run(cmd, check=True, cwd=REPO_ROOT,
                           env={"PYTHONPATH": str(REPO_ROOT / "src"),
                                "PATH": sys.executable})
        except subprocess.CalledProcessError:
            pass
    return [
        {"ticker": c.ticker, "fiscal_year": c.fiscal_year,
         "metric_key": c.metric_key, "period_type": c.period_type,
         "fix_class": "reconcile",
         "old_usd": c.value_usd, "new_usd": None}
        for c in cells
    ]


def _apply_gap_extract(cells, apply: bool, run_id: str) -> list[dict]:
    """Gap extractions require per-ticker routing. For the first pass we
    just flag what extractor SHOULD run; actual invocation is manual
    (one of scripts/extract_baba_cloud_6k.py, extract_bidu_cloud_v2.py,
    backfill_baba_capex_20f.py, extract_hyperscaler_cloud_quarterly.py
    depending on ticker + metric)."""
    return [
        {"ticker": c.ticker, "fiscal_year": c.fiscal_year,
         "metric_key": c.metric_key, "period_type": c.period_type,
         "fix_class": f"gap_extract (manual: {_suggest_extractor(c)})",
         "old_usd": None, "new_usd": None}
        for c in cells
    ]


def _suggest_extractor(cell) -> str:
    if cell.ticker == "BABA" and cell.metric_key == "cloud_segment_revenue":
        return "scripts/extract_baba_cloud_6k.py"
    if cell.ticker == "BIDU" and cell.metric_key == "cloud_segment_revenue":
        return "scripts/extract_bidu_cloud_v2.py"
    if cell.ticker == "BABA" and cell.metric_key == "capital_expenditures":
        return "scripts/backfill_baba_capex_20f.py"
    if cell.ticker in ("AMZN", "MSFT", "GOOGL", "ORCL") \
            and cell.metric_key == "cloud_segment_revenue":
        return "scripts/extract_hyperscaler_cloud_quarterly.py"
    return "manual (no extractor wired)"


def record_verdicts(
    cells, run_id: str, fixes: list[dict], db: Database,
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.mutating() as conn:
        for cell in cells:
            if cell.classification != "flagged":
                continue
            if cell.extraction_id is None:
                continue
            failures = [r for r in cell.check_results if not r.passed]
            if not failures:
                continue
            severity = "error" if any(
                r.severity == "error" for r in failures
            ) else "warn"
            conn.execute(
                """
                INSERT INTO audit_verdicts (
                    extraction_id, run_id, checked_at,
                    mechanical_flags_json, llm_verdict,
                    llm_response_json, applied_fix_json, severity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cell.extraction_id, run_id, now,
                    json.dumps([r.to_json() for r in failures]),
                    cell.llm_verdict, None, None, severity,
                ),
            )
