#!/usr/bin/env python3
"""Backfill BABA annual capex FY2021-FY2024 from 20-F cash flow statements.

BABA does NOT tag capex in XBRL (only OCF). Values are in the
Consolidated Statements of Cash Flows under "Purchase of property and
equipment". Each 20-F's line shows the prior-2 + prior-1 + current FY
values in CNY millions plus a USD convenience conversion.

Most-recent-wins: for each fiscal year, use the value from the most
recent 20-F that reported it (values occasionally get restated in
later filings).

Usage:
    python scripts/backfill_baba_capex_20f.py [--dry-run]
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
from capex.fx.rates import normalize_to_usd
from capex.read.text import extract_text

ACTOR = "baba-capex-20f@0.1.0"

# Hand-curated (fiscal_year → value in CNY millions) extracted from the
# Purchase-of-PP&E line in each 20-F's cash flow statement. Most recent
# 20-F wins for any given year. Values are NEGATIVE in the filing but
# we store as a positive capex figure.
# Sourced via the probe in the parent conversation — see
# docs/_notes/baba_capex_provenance.md for detail (not committed).
CAPEX_BY_FY_CNY_M = {
    # Already in DB with different methodology — skip these to avoid
    # overwriting:
    # 2019: 32336,
    # 2020: 24662,
    # 2025: 73038,
    2021: 36160,   # from FY21/FY22/FY23 20-Fs
    2022: 42028,   # from FY22/FY23/FY24 20-Fs
    2023: 30373,   # from FY23/FY24 20-Fs
    2024: 27579,   # from FY24 20-F
}


def _find_source_doc(conn, fy: int) -> int | None:
    """Find the BABA 20-F row for fiscal_year=fy."""
    r = conn.execute(
        "SELECT id FROM source_documents WHERE ticker='BABA' "
        "AND form_type='20-F' AND fiscal_year = ?",
        (fy,),
    ).fetchone()
    return r[0] if r else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    inserted = 0
    skipped = 0
    with db.mutating() as conn:
        for fy, value_cny_m in sorted(CAPEX_BY_FY_CNY_M.items()):
            sd_id = _find_source_doc(conn, fy)
            if not sd_id:
                print(f"  FY{fy}: no 20-F in source_documents, skipping")
                skipped += 1
                continue
            # Check if capex row already exists
            existing = conn.execute(
                """SELECT id, value, value_usd, extracting_model FROM extractions
                WHERE source_document_id=? AND metric_key='capital_expenditures'
                  AND period_type='FY'""",
                (sd_id,),
            ).fetchone()
            # BABA FYE March: fiscal year 2024 ends 2024-03-31
            period_of_report = f"{fy:04d}-03-31"
            value_usd, fx_rate, fx_date = normalize_to_usd(
                value_cny_m, "CNY", period_of_report, db=db,
            )
            quote = (
                f"Purchase of property and equipment (excluding land use "
                f"rights and construction in progress relating to office "
                f"campuses): CNY {value_cny_m:,}M (FY{fy})"
            )
            if args.dry_run:
                if existing:
                    print(f"  FY{fy}: EXISTS (model={existing['extracting_model']}, "
                          f"usd=${existing['value_usd']:,.0f}M) — would "
                          f"{'SKIP' if 'xbrl' in (existing['extracting_model'] or '') else 'REPLACE'}")
                else:
                    print(f"  FY{fy}: would INSERT CNY{value_cny_m:,.0f}M → "
                          f"${value_usd:,.0f}M USD")
                continue
            if existing:
                # Prefer xbrl-verified; otherwise update.
                model = existing["extracting_model"] or ""
                if "xbrl" in model:
                    skipped += 1
                    continue
                conn.execute(
                    """UPDATE extractions SET value=?, value_usd=?, fx_rate=?,
                    fx_rate_date=?, quote=?, locator_section=?,
                    extracting_model=?, extracted_at=? WHERE id=?""",
                    (value_cny_m, value_usd, fx_rate, fx_date, quote,
                     "Item 18 - Consolidated Statements of Cash Flows — "
                     "Purchase of property and equipment",
                     ACTOR, now, existing["id"]),
                )
                conn.execute(
                    "INSERT INTO audit_log (ts, actor, action, target_table, target_id, payload) "
                    "VALUES (?, ?, 'baba_capex_20f_updated', 'extractions', ?, ?)",
                    (now, ACTOR, existing["id"], json.dumps({
                        "fiscal_year": fy, "value_cny_m": value_cny_m,
                        "value_usd": value_usd,
                    }, sort_keys=True)),
                )
                inserted += 1
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
                    sd_id, "capital_expenditures", value_cny_m,
                    f"CNY {value_cny_m:,.0f}M", "USD_millions",
                    quote, None,
                    "Item 18 - Consolidated Statements of Cash Flows — "
                    "Purchase of property and equipment",
                    "direct", 0.95, ACTOR, "0.1.0-draft", now,
                    value_usd, fx_rate, fx_date, "CNY",
                    "FY", 12, "ytd_cumulative",
                ),
            )
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, target_table, target_id, payload) "
                "VALUES (?, ?, 'baba_capex_20f_inserted', 'extractions', ?, ?)",
                (now, ACTOR, cur.lastrowid, json.dumps({
                    "fiscal_year": fy, "value_cny_m": value_cny_m,
                    "value_usd": value_usd,
                }, sort_keys=True)),
            )
            inserted += 1
    print(f"inserted/updated={inserted}, skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
