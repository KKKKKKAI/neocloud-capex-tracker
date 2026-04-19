#!/usr/bin/env python3
"""Re-fetch XBRL values for flow metrics (capex, OCF, D&A) with corrected
duration-preference dedup.

Previously `xbrl.timeseries.fetch_concept_timeseries` did first-come-wins
dedup on (end_date, form), which for multi-context concepts like AMZN's
PaymentsToAcquirePropertyPlantAndEquipment picked a trailing-twelve-months
(364-day) value at 10-Q end dates instead of the standalone 3-month
value. The fix in `xbrl/timeseries.py` now picks the entry closest to
the preferred duration (90d for 10-Q/6-K, 365d for 10-K/20-F).

This script walks every extraction row with `extracting_model IN
('xbrl-verified','xbrl-companyfacts')` for metric_key IN (capex, OCF,
D&A) and UPDATES value/value_usd with the newly-fetched correct values.
Rows that don't change are skipped.

Usage:
    python scripts/refetch_xbrl_flow_metrics.py [--dry-run]
                                                [--tickers AMZN,MSFT,...]
                                                [--metrics capital_expenditures,...]
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
from capex.extract.extractors.xbrl import CONCEPT_MAP
from capex.fx.rates import normalize_to_usd
from capex.xbrl.timeseries import fetch_concept_timeseries

FLOW_METRICS = ("capital_expenditures", "operating_cash_flow",
                "depreciation_amortization", "revenue")
XBRL_MODELS = ("xbrl-verified", "xbrl-companyfacts")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tickers", default=None,
                    help="comma list; default = every ticker with edgar_cik")
    ap.add_argument("--metrics", default=",".join(FLOW_METRICS))
    args = ap.parse_args()

    metrics = [m.strip() for m in args.metrics.split(",") if m.strip()]
    db = Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with db.connect() as conn:
        if args.tickers:
            tickers = [t.strip() for t in args.tickers.split(",")]
        else:
            tickers = [
                r[0] for r in conn.execute(
                    "SELECT ticker FROM companies "
                    "WHERE edgar_cik IS NOT NULL AND edgar_cik != '' "
                    "ORDER BY ticker"
                )
            ]
        company_info = {
            r["ticker"]: dict(r) for r in conn.execute(
                "SELECT ticker, edgar_cik, reporting_currency FROM companies"
            )
        }

    updated = 0
    unchanged = 0
    for ticker in tickers:
        info = company_info.get(ticker)
        if not info or not info["edgar_cik"]:
            continue
        cik = info["edgar_cik"]
        reporting_ccy = info["reporting_currency"] or "USD"
        for metric_key in metrics:
            concepts = CONCEPT_MAP.get(metric_key, [])
            # Some filers switch concepts mid-life (e.g. AMZN moved to
            # PaymentsToAcquireProductiveAssets in 2017). Accumulate
            # entries from ALL concepts and let each end_date get its
            # value from whichever concept actually reported.
            by_end: dict[str, dict] = {}
            for concept in concepts:
                try:
                    series = fetch_concept_timeseries(
                        cik=cik, concept=concept, start_date="2015-01-01",
                    )
                except Exception:
                    continue
                for p in series:
                    by_end.setdefault(p["end"], p)
            if not by_end:
                continue

            # Walk existing XBRL rows for this company × metric
            with db.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT e.id, e.value, e.value_usd, e.extracting_model,
                           e.period_type,
                           sd.period_of_report, sd.period_token,
                           sd.form_type, sd.fiscal_year
                    FROM extractions e
                    JOIN source_documents sd ON e.source_document_id = sd.id
                    WHERE sd.ticker = ? AND e.metric_key = ?
                      AND e.extracting_model IN ({placeholders})
                    """.format(placeholders=",".join("?" * len(XBRL_MODELS))),
                    (ticker, metric_key, *XBRL_MODELS),
                ).fetchall()

            def _duration_days(entry: dict) -> int | None:
                s, t = entry.get("start"), entry.get("end")
                if not s or not t:
                    return None
                try:
                    from datetime import date as _d
                    return (_d.fromisoformat(t) - _d.fromisoformat(s)).days
                except ValueError:
                    return None

            def _target_period_type(
                form: str, token: str, duration: int | None,
            ) -> tuple[str, int]:
                """Pick period_type from the XBRL context duration, not
                the source_document's period_token. That way a 10-Q that
                only reports YTD values (GOOGL/META) gets stored as H1/9M
                rather than falsely labelled 3-month Q2/Q3."""
                form = (form or "").strip()
                token = (token or "").strip()
                if form in ("10-K", "20-F") and token == "AR":
                    return ("FY", 12)
                if form != "10-Q":
                    return ("", 0)
                if duration is None:
                    return ({"Q1": "Q1", "Q2": "Q2", "Q3": "Q3"}.get(token, ""), 3)
                # 10-Q: choose from context duration buckets
                if duration <= 100:
                    # 3-month standalone
                    return ({"Q1": "Q1", "Q2": "Q2", "Q3": "Q3"}.get(token, ""), 3)
                if 150 <= duration <= 200:
                    return ("H1", 6)
                if 250 <= duration <= 290:
                    return ("9M", 9)
                if duration >= 350:
                    # TTM — don't store; reconcile handles this path
                    return ("", 0)
                # Awkward duration — fall back to token-based mapping
                return ({"Q1": "Q1", "Q2": "Q2", "Q3": "Q3"}.get(token, ""), 3)

            with db.mutating() as conn:
                for r in rows:
                    target = by_end.get(r["period_of_report"])
                    if not target:
                        continue
                    new_val_native = round(target["val"] / 1e6, 2)
                    dur = _duration_days(target)
                    target_ptype, basis = _target_period_type(
                        r["form_type"], r["period_token"], dur,
                    )
                    if not target_ptype:
                        continue
                    value_matches = (
                        r["value"] is not None
                        and abs(float(r["value"]) - new_val_native) < 0.5
                    )
                    ptype_matches = (r.get("period_type") if hasattr(r, 'get')
                                     else r["period_type"]) == target_ptype
                    # r is a sqlite3.Row — use bracket access
                    ptype_matches = (r["period_type"] == target_ptype)
                    if value_matches and ptype_matches:
                        unchanged += 1
                        continue
                    new_usd, fx_rate, fx_date = normalize_to_usd(
                        new_val_native, reporting_ccy,
                        r["period_of_report"], db=db,
                    )
                    old_usd = r["value_usd"]
                    old_ptype = r["period_type"]
                    if args.dry_run:
                        print(
                            f"  {ticker} {metric_key} {r['period_of_report']} "
                            f"{r['period_token']} (ptype {old_ptype}→{target_ptype}): "
                            f"${old_usd:,.0f}M → ${new_usd:,.0f}M"
                        )
                        updated += 1
                        continue
                    # Check for unique-constraint collision before updating
                    # ptype: another row with the same (source_doc, metric,
                    # model, target_ptype) would block the update.
                    collision = conn.execute(
                        "SELECT id FROM extractions "
                        "WHERE source_document_id=? AND metric_key=? "
                        "AND extracting_model=? AND period_type=? "
                        "AND id != ?",
                        (
                            # need source_document_id — fetch from another
                            # row in this set or separate lookup
                            # Use a fresh SELECT here
                            None,  # placeholder — we'll patch below
                            metric_key,
                            r["extracting_model"],
                            target_ptype,
                            r["id"],
                        ),
                    )
                    # Properly do the collision check
                    sdid_row = conn.execute(
                        "SELECT source_document_id FROM extractions WHERE id = ?",
                        (r["id"],),
                    ).fetchone()
                    sdid = sdid_row[0] if sdid_row else None
                    if sdid is None:
                        continue
                    collision = conn.execute(
                        "SELECT id FROM extractions "
                        "WHERE source_document_id=? AND metric_key=? "
                        "AND extracting_model=? AND period_type=? "
                        "AND id != ?",
                        (sdid, metric_key, r["extracting_model"],
                         target_ptype, r["id"]),
                    ).fetchone()
                    if collision:
                        # Delete the colliding row (it's the pre-existing
                        # duplicate that we would have replaced).
                        conn.execute(
                            "DELETE FROM extractions WHERE id = ?",
                            (collision[0],),
                        )
                    conn.execute(
                        "UPDATE extractions SET value=?, value_usd=?, "
                        "fx_rate=?, fx_rate_date=?, period_type=?, "
                        "basis_period_months=?, extracted_at=? WHERE id=?",
                        (
                            new_val_native, new_usd, fx_rate, fx_date,
                            target_ptype, basis, now, r["id"],
                        ),
                    )
                    conn.execute(
                        "INSERT INTO audit_log (ts, actor, action, "
                        "target_table, target_id, payload) VALUES (?, "
                        "'xbrl-refetch-duration-fix@0.1.0', "
                        "'xbrl_flow_value_corrected', 'extractions', ?, ?)",
                        (now, r["id"], json.dumps({
                            "ticker": ticker,
                            "metric_key": metric_key,
                            "period_of_report": r["period_of_report"],
                            "period_token": r["period_token"],
                            "form_type": r["form_type"],
                            "old_native": r["value"],
                            "new_native": new_val_native,
                            "old_usd": old_usd,
                            "new_usd": new_usd,
                        }, sort_keys=True)),
                    )
                    updated += 1
    print(f"updated={updated}, unchanged={unchanged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
