#!/usr/bin/env python3
"""Rollback every restatement-tagged row ahead of the LLM-based rework.

Prior versions of the pipeline populated restatement rows via two
separate paths we're deprecating (XBRL companyfacts dedup + regex
segment-table scan). This script wipes those rows and their virtual
source_documents + evidence / validation rows, then logs a summary
to `audit_log` so the history is preserved.

Deletes:
  - extractions rows where extracting_model LIKE 'restated-%'
  - extraction_evidence rows referencing those extraction ids
  - validation_results rows referencing those extraction ids
  - source_documents rows whose raw_path starts with 'restated-virtual://'
    (created as the "virtual" row for a restated comparative) and are
    no longer referenced after the extraction rows above are gone.

Usage:
    python scripts/rollback_restatement_rows.py           # dry-run
    python scripts/rollback_restatement_rows.py --apply   # commit
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Commit deletions (default: dry-run)")
    args = ap.parse_args()

    db = Database()
    with db.connect() as conn:
        restated_rows = conn.execute(
            """
            SELECT e.id, e.metric_key, e.value_usd, e.extracting_model,
                   e.period_type, sd.ticker, sd.fiscal_year,
                   sd.period_of_report, sd.id AS sd_id, sd.raw_path
            FROM extractions e
            JOIN source_documents sd ON e.source_document_id = sd.id
            WHERE e.extracting_model LIKE 'restated-%'
            ORDER BY sd.ticker, sd.fiscal_year, e.metric_key
            """,
        ).fetchall()
        virtual_sds = conn.execute(
            "SELECT id, ticker, fiscal_year, period_of_report, raw_path "
            "FROM source_documents "
            "WHERE raw_path LIKE 'restated-virtual://%'",
        ).fetchall()

    print(f"Found {len(restated_rows)} restated-* extraction rows")
    print(f"Found {len(virtual_sds)} virtual source_documents "
          "(raw_path LIKE 'restated-virtual://%')")
    print()
    if restated_rows:
        print("--- First 10 extraction rows that would be removed ---")
        for r in restated_rows[:10]:
            d = dict(r)
            print(f"  ext_id={d['id']:4d}  {d['ticker']:6s} FY{d['fiscal_year']} "
                  f"{d['metric_key']:30s} {d['period_type']:4s}  "
                  f"${d['value_usd'] or 0:>9,.0f}M  model={d['extracting_model']}")
        if len(restated_rows) > 10:
            print(f"  ... and {len(restated_rows) - 10} more")
        print()

    if not args.apply:
        print("DRY RUN — re-run with --apply to commit.")
        return 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ext_ids = [r["id"] for r in restated_rows]
    vsd_ids = [r["id"] for r in virtual_sds]
    deleted = {"extractions": 0, "extraction_evidence": 0,
               "validation_results": 0, "audit_verdicts": 0,
               "source_documents": 0}
    with db.mutating() as conn:
        if ext_ids:
            placeholders = ",".join("?" * len(ext_ids))
            cur = conn.execute(
                f"DELETE FROM extraction_evidence "
                f"WHERE extraction_id IN ({placeholders})",
                ext_ids,
            )
            deleted["extraction_evidence"] = cur.rowcount or 0
            cur = conn.execute(
                f"DELETE FROM validation_results "
                f"WHERE extraction_id IN ({placeholders})",
                ext_ids,
            )
            deleted["validation_results"] = cur.rowcount or 0
            # audit_verdicts has FK to extractions(id)
            cur = conn.execute(
                f"DELETE FROM audit_verdicts "
                f"WHERE extraction_id IN ({placeholders})",
                ext_ids,
            )
            deleted["audit_verdicts"] = cur.rowcount or 0
            cur = conn.execute(
                f"DELETE FROM extractions WHERE id IN ({placeholders})",
                ext_ids,
            )
            deleted["extractions"] = cur.rowcount or 0
        # Delete virtual source_documents that are now orphaned (no
        # extractions reference them).
        if vsd_ids:
            placeholders = ",".join("?" * len(vsd_ids))
            orphan_ids = [
                r[0] for r in conn.execute(
                    f"SELECT id FROM source_documents "
                    f"WHERE id IN ({placeholders}) "
                    f"AND id NOT IN (SELECT DISTINCT source_document_id "
                    f"                FROM extractions)",
                    vsd_ids,
                )
            ]
            if orphan_ids:
                p2 = ",".join("?" * len(orphan_ids))
                cur = conn.execute(
                    f"DELETE FROM source_documents WHERE id IN ({p2})",
                    orphan_ids,
                )
                deleted["source_documents"] = cur.rowcount or 0
        # Preserve provenance in audit_log.
        conn.execute(
            "INSERT INTO audit_log "
            "(ts, actor, action, target_table, target_id, payload) "
            "VALUES (?, 'rollback-restatement@0.1.0', "
            "'restatement_rollback', 'extractions', NULL, ?)",
            (now, json.dumps({
                "rationale": (
                    "Wipe legacy restatement rows ahead of LLM dual-agent "
                    "redesign. Restatements will be re-populated via the "
                    "multi-period Agent A / Agent B flow."
                ),
                "deleted_extraction_ids": ext_ids,
                "deleted_virtual_source_document_ids": vsd_ids,
                "deleted_counts": deleted,
            }, sort_keys=True)),
        )
    print(f"Deleted: {deleted}")
    print("Audit-log entry recorded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
