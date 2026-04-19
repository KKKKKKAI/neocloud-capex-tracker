#!/usr/bin/env python3
"""Scale BIDU quarterly non-online-marketing proxy to true AI Cloud.

Principle (per task #14):
  quarterly AI Cloud ≈ quarterly non-online-mkt × (annual AI Cloud / annual non-online-mkt)

We already have:
  * quarterly_non_online_mkt_rmb   — from extract_bidu_cloud_6k.py 6-K extraction
  * annual_ai_cloud_rmb            — from the 20-F (LLM-extracted, claude-code model)

We don't have annual_non_online_mkt explicitly, so we estimate it:
  annual_non_online_mkt ≈ (Q1 + Q2 + Q3)_non_online × 4/3

That makes the per-quarter scaling factor:
  scale_fy = (annual_ai_cloud_rmb) / ((Q1+Q2+Q3)_non_online × 4/3)
          = (3 × annual_ai_cloud_rmb) / (4 × sum_Q1Q2Q3_non_online_rmb)

The scaled quarterly AI Cloud values are written as *new* rows with
extracting_model='bidu-cloud-scaled@0.2.0', replacing the raw proxy
rows from 'bidu-cloud-6k-proxy@0.1.0' (which are deleted). Reconcile
then derives Q4 from annual − (Q1+Q2+Q3).

Usage:
    python scripts/scale_bidu_cloud_quarterly.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from capex.db import Database
from capex.fx.rates import normalize_to_usd

PROXY_MODEL = "bidu-cloud-6k-proxy@0.1.0"
SCALED_MODEL = "bidu-cloud-scaled@0.2.0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Step 1: load existing proxy rows + annual AI Cloud per FY
    with db.connect() as conn:
        proxy_rows = conn.execute(
            """
            SELECT e.id, e.value, e.period_type, e.quote, e.locator_section,
                   sd.id AS sd_id, sd.period_of_report, sd.fiscal_year
            FROM extractions e
            JOIN source_documents sd ON e.source_document_id = sd.id
            WHERE sd.ticker='BIDU' AND e.metric_key='cloud_segment_revenue'
              AND e.extracting_model = ?
            ORDER BY sd.fiscal_year, sd.period_of_report
            """,
            (PROXY_MODEL,),
        ).fetchall()
        annual_rows = conn.execute(
            """
            SELECT sd.fiscal_year, e.value AS annual_ai_cloud_rmb, e.id AS annual_ext_id
            FROM extractions e
            JOIN source_documents sd ON e.source_document_id = sd.id
            WHERE sd.ticker='BIDU' AND e.metric_key='cloud_segment_revenue'
              AND sd.period_token='AR' AND e.period_type='FY'
              AND e.reporting_currency='CNY'
            """
        ).fetchall()
    if not proxy_rows:
        print(f"no rows found for model {PROXY_MODEL!r}; "
              "run extract_bidu_cloud_6k.py first", file=sys.stderr)
        return 1
    annual_by_fy = {r["fiscal_year"]: r for r in annual_rows}

    # Step 2: group proxy rows by fiscal_year
    by_fy: dict[int, list] = {}
    for r in proxy_rows:
        by_fy.setdefault(r["fiscal_year"], []).append(dict(r))

    # Step 3: compute scale per FY and build output rows
    scaled_pending: list[tuple[dict, float, float, str]] = []
    skipped = []
    for fy in sorted(by_fy):
        quarters = by_fy[fy]
        ann = annual_by_fy.get(fy)
        if not ann:
            print(f"  FY{fy}: no annual AI Cloud; skipping", file=sys.stderr)
            skipped.extend(quarters)
            continue
        annual_ai = float(ann["annual_ai_cloud_rmb"])
        sum_q123 = sum(float(q["value"]) for q in quarters)
        if sum_q123 <= 0:
            skipped.extend(quarters)
            continue
        # Estimate annual non-online as (Q1+Q2+Q3) × 4/3
        annual_non_online_est = sum_q123 * 4.0 / 3.0
        scale = annual_ai / annual_non_online_est
        print(
            f"  FY{fy}: annual_ai_cloud=RMB{annual_ai:,.0f}M "
            f"Q123_non_online_sum=RMB{sum_q123:,.0f}M "
            f"annual_non_online_est=RMB{annual_non_online_est:,.0f}M "
            f"scale={scale:.3f}"
        )
        for q in quarters:
            scaled_rmb = float(q["value"]) * scale
            scaled_pending.append((q, scaled_rmb, scale, ann["annual_ext_id"]))

    if not scaled_pending:
        print("nothing to scale")
        return 0

    if args.dry_run:
        for q, scaled, scale, _annual_ext in scaled_pending[:8]:
            print(f"    {q['period_of_report']} {q['period_type']}: "
                  f"RMB{q['value']:,.0f}M × {scale:.3f} = RMB{scaled:,.0f}M")
        print(f"would scale {len(scaled_pending)} rows; dry-run")
        return 0

    inserted = 0
    deleted = 0
    with db.mutating() as conn:
        for q, scaled_rmb, scale, annual_ext_id in scaled_pending:
            value_usd, fx_rate, fx_date = normalize_to_usd(
                scaled_rmb, "CNY", q["period_of_report"],
            )
            locator = (
                "6-K Non-online marketing × annual AI-Cloud share "
                "(annual_non_online estimated as (Q1+Q2+Q3)×4/3)"
            )
            quote = (
                f"[SCALED] proxy_non_online_rmb_m={q['value']:.0f}, "
                f"scale_factor={scale:.4f}, "
                f"anchor=annual_ai_cloud_extraction#{annual_ext_id}. "
                "Derived estimate of BIDU AI Cloud quarterly revenue "
                "anchored to the 20-F annual AI Cloud figure."
            )
            # Delete old proxy row + insert scaled replacement for same
            # (source_doc, period_type) tuple.
            conn.execute(
                "DELETE FROM extractions WHERE id = ?", (q["id"],),
            )
            deleted += 1
            existing = conn.execute(
                """SELECT id FROM extractions
                WHERE source_document_id=? AND metric_key=?
                  AND extracting_model=? AND period_type=?""",
                (q["sd_id"], "cloud_segment_revenue", SCALED_MODEL, q["period_type"]),
            ).fetchone()
            if existing:
                continue
            cur = conn.execute(
                """
                INSERT INTO extractions (
                    source_document_id, metric_key, value, value_text, unit,
                    quote, locator_page, locator_section, extraction_type,
                    confidence, extracting_model, protocol_version,
                    extracted_at, value_usd, fx_rate, fx_rate_date,
                    reporting_currency, period_type, basis_period_months,
                    reporting_convention
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    q["sd_id"], "cloud_segment_revenue", scaled_rmb,
                    f"RMB {scaled_rmb:,.0f}M (scaled)", "USD_millions",
                    quote[:500], None, locator,
                    "inferred", 0.85, SCALED_MODEL, "0.1.0-draft", now,
                    value_usd, fx_rate, fx_date, "CNY",
                    q["period_type"], 3, "standalone_quarterly",
                ),
            )
            conn.execute(
                """INSERT INTO audit_log (ts, actor, action, target_table, target_id, payload)
                VALUES (?, ?, 'bidu_cloud_scaled', 'extractions', ?, ?)""",
                (now, SCALED_MODEL, cur.lastrowid, json.dumps({
                    "period_of_report": q["period_of_report"],
                    "fiscal_year": q["fiscal_year"],
                    "period_type": q["period_type"],
                    "proxy_rmb_m": q["value"],
                    "scale_factor": scale,
                    "scaled_rmb_m": scaled_rmb,
                    "annual_ext_id": annual_ext_id,
                }, sort_keys=True)),
            )
            inserted += 1
    print(f"inserted {inserted} scaled rows; deleted {deleted} proxy rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
