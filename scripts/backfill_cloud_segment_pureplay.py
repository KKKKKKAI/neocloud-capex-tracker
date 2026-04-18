#!/usr/bin/env python3
"""Backfill cloud_segment_revenue from revenue for pure-play neoclouds.

For companies with whole_company treatment in coverage.yaml (GDS, CRWV,
APLD, IREN, NBIS), cloud_segment_revenue IS total revenue. Rather than
re-extracting from filings, clone existing revenue rows with
metric_key='cloud_segment_revenue' and extracting_model='whole-company-copy'.

Idempotent: skips rows that already exist for the same
(source_document_id, metric_key, extracting_model, period_type).

Usage:
    python scripts/backfill_cloud_segment_pureplay.py [--dry-run]
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

# Pure-play tickers where cloud_segment_revenue == revenue
PURE_PLAY_TICKERS = ["GDS", "CRWV", "APLD", "IREN", "NBIS"]

ACTOR = "whole-company-copy@0.1.0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT e.id, e.source_document_id, e.value, e.value_text, e.unit,
                   e.quote, e.locator_page, e.locator_section,
                   e.extraction_type, e.confidence, e.value_usd,
                   e.fx_rate, e.fx_rate_date, e.reporting_currency,
                   e.period_type, e.basis_period_months,
                   e.reporting_convention, sd.ticker
            FROM extractions e
            JOIN source_documents sd ON e.source_document_id = sd.id
            WHERE e.metric_key = 'revenue'
              AND sd.ticker IN ({placeholders})
              AND e.extracting_model NOT LIKE 'reconcile%'
              AND e.value_usd IS NOT NULL
            """.format(placeholders=",".join("?" * len(PURE_PLAY_TICKERS))),
            PURE_PLAY_TICKERS,
        ).fetchall()

    candidates = [dict(r) for r in rows]
    print(f"found {len(candidates)} revenue rows across pure-plays")

    if args.dry_run:
        for c in candidates[:5]:
            print(
                f"  would copy: {c['ticker']} period_type={c['period_type']!r} "
                f"value_usd={c['value_usd']}"
            )
        print("dry-run; not committing")
        return 0

    inserted = 0
    skipped = 0
    with db.mutating() as conn:
        for c in candidates:
            existing = conn.execute(
                """
                SELECT id FROM extractions
                WHERE source_document_id = ? AND metric_key = ?
                  AND extracting_model = ? AND period_type = ?
                """,
                (c["source_document_id"], "cloud_segment_revenue", ACTOR, c["period_type"] or ""),
            ).fetchone()
            if existing:
                skipped += 1
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
                    c["source_document_id"],
                    "cloud_segment_revenue",
                    c["value"],
                    c["value_text"],
                    c["unit"],
                    f"whole-company treatment: total revenue = cloud revenue ({c['ticker']})",
                    c["locator_page"],
                    c["locator_section"] or "whole_company_treatment",
                    "inferred",
                    c["confidence"],
                    ACTOR,
                    "0.1.0-draft",
                    now,
                    c["value_usd"],
                    c["fx_rate"],
                    c["fx_rate_date"],
                    c["reporting_currency"],
                    c["period_type"] or "",
                    c["basis_period_months"],
                    c["reporting_convention"],
                ),
            )
            row_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO audit_log (ts, actor, action, target_table, target_id, payload)
                VALUES (?, ?, 'cloud_segment_whole_company_copy', 'extractions', ?, ?)
                """,
                (
                    now, ACTOR, row_id,
                    json.dumps({
                        "source_revenue_id": c["id"],
                        "ticker": c["ticker"],
                        "period_type": c["period_type"],
                    }, sort_keys=True),
                ),
            )
            inserted += 1

    print(f"inserted {inserted} cloud_segment_revenue rows, skipped {skipped} existing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
