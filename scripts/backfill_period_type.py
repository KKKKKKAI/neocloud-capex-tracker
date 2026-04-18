#!/usr/bin/env python3
"""Backfill period_type on existing extraction rows.

Uses the same inference as extract/reconcile.py: form_type +
period_token + FYE month + period_of_report month → period_type.

Run once after applying migration 0007 so that audit and reconcile
have an accurate picture of what's already stored.

Usage:
    python scripts/backfill_period_type.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from capex.db import Database
from capex.extract.reconcile import _infer_period_type, _basis_months_for


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    db = Database(path=Path(args.db)) if args.db else Database()

    updates: list[tuple[str, int | None, int]] = []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT e.id, e.period_type, sd.form_type, sd.period_token, "
            "sd.period_of_report, c.fiscal_year_end_month "
            "FROM extractions e "
            "JOIN source_documents sd ON e.source_document_id = sd.id "
            "JOIN companies c ON sd.ticker = c.ticker "
            "WHERE (e.period_type IS NULL OR e.period_type = '')"
        ).fetchall()

    for r in rows:
        row = {
            "period_type": r[1],
            "form_type": r[2],
            "period_token": r[3],
            "period_of_report": r[4],
            "fiscal_year_end_month": r[5],
        }
        inferred = _infer_period_type(row)
        if not inferred:
            continue
        updates.append((inferred, _basis_months_for(inferred), r[0]))

    print(f"rows eligible for update: {len(updates)} / total scanned: {len(rows)}")
    if args.dry_run:
        for ptype, _basis, _eid in updates[:5]:
            print("  sample:", ptype)
        print("dry-run; not committing")
        return 0

    with db.mutating() as conn:
        conn.executemany(
            "UPDATE extractions SET period_type = ?, basis_period_months = ? "
            "WHERE id = ?",
            updates,
        )
    print(f"backfilled {len(updates)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
