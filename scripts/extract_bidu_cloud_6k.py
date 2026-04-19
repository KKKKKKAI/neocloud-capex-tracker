#!/usr/bin/env python3
"""Extract Baidu AI Cloud quarterly proxy from 6-K earnings press releases.

BIDU does NOT disclose quarterly AI Cloud revenue as a standalone line.
Closest proxy in 6-K press releases is "non-online marketing revenue"
(= AI Cloud + Apollo autonomous driving + smart-device hardware, with
AI Cloud being the dominant component — roughly 85-95 % of that total
based on BIDU's 20-F segment breakdown).

This script fetches each BIDU 6-K, regex-matches the proxy value, and
writes it to `extractions` as `cloud_segment_revenue` with
`extracting_model='bidu-cloud-6k-proxy@0.1.0'` so the non-AI-Cloud
components are transparently acknowledged.

Pattern vintages:
- ≥ 2022: "non-online marketing revenue was RMB<N> billion"
- 2019-2021: "other revenues were RMB<N> billion" (broader — still used
  as a fallback for continuity of the series, tagged differently).
- ≤ 2018: no usable breakout; skip (BIDU's cloud business was tiny).

Usage:
    python scripts/extract_bidu_cloud_6k.py [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from capex.db import Database
from capex.fx.rates import normalize_to_usd

ACTOR = "bidu-cloud-6k-proxy@0.1.0"
HEADERS = {"User-Agent": "capex-research research@example.com"}
RATE_DELAY = 0.5

# Patterns tried in order — first match wins. Each group-1 is the
# numeric value; unit (million / billion) captured in group-2.
PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "non_online_marketing",
        re.compile(
            r"(?i)non[\- ]online\s+marketing\s+(?:services?\s+)?revenue\s+"
            r"(?:was|were|of)\s+RMB\s*(\d[\d,]*(?:\.\d+)?)\s*(million|billion)",
        ),
    ),
    (
        "other_revenues",
        re.compile(
            r"(?i)other\s+(?:services?\s+)?revenues?\s+"
            r"(?:was|were|of)\s+RMB\s*(\d[\d,]*(?:\.\d+)?)\s*(million|billion)",
        ),
    ),
]


def _q_token_from_period(period_of_report: str) -> str:
    """BIDU FYE December. Calendar quarter == fiscal quarter."""
    month = int(period_of_report[5:7])
    return {1: "Q1", 2: "Q1", 3: "Q1", 4: "Q2", 5: "Q2", 6: "Q2",
            7: "Q3", 8: "Q3", 9: "Q3", 10: "Q4", 11: "Q4", 12: "Q4"}[month]


def _fetch(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  fetch error: {e}", file=sys.stderr)
        return None


def _strip(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"&[a-z#0-9]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _match(text: str) -> tuple[str, float, str] | None:
    for name, pat in PATTERNS:
        m = pat.search(text[:100000])
        if not m:
            continue
        raw = m.group(1).replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        unit = m.group(2).lower()
        if unit == "billion":
            val *= 1000.0  # convert billion → million
        return (name, val, m.group(0)[:220])
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    db = Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, period_of_report, fiscal_year, source_url
            FROM source_documents
            WHERE ticker='BIDU' AND form_type='6-K' AND source_url LIKE 'https://%'
            ORDER BY period_of_report
            """
        ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"processing {len(rows)} BIDU 6-K filings")

    pending = []
    fail = 0
    for r in rows:
        time.sleep(RATE_DELAY)
        html = _fetch(r["source_url"])
        if not html:
            fail += 1
            continue
        text = _strip(html)
        hit = _match(text)
        period = r["period_of_report"]
        if not hit:
            print(f"  {period}: NO MATCH")
            fail += 1
            continue
        pat_name, val_rmb_m, quote = hit
        ptype = _q_token_from_period(period)
        pending.append((dict(r), val_rmb_m, quote, ptype, pat_name))
        print(f"  {period} [{ptype}]: RMB {val_rmb_m:,.0f}M ({pat_name})")

    print(f"\nextracted {len(pending)}, failed {fail}")
    if args.dry_run:
        print("dry-run; not committing")
        return 0

    inserted = 0
    skipped = 0
    with db.mutating() as conn:
        for meta, val_rmb_m, quote, ptype, pat_name in pending:
            value_usd, fx_rate, fx_date = normalize_to_usd(
                val_rmb_m, "CNY", meta["period_of_report"],
            )
            existing = conn.execute(
                """SELECT id FROM extractions
                WHERE source_document_id=? AND metric_key=?
                  AND extracting_model=? AND period_type=?""",
                (meta["id"], "cloud_segment_revenue", ACTOR, ptype),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            locator = (
                f"6-K Press Release — Non-online marketing proxy "
                f"(AI Cloud + Apollo + smart-device hardware; pattern={pat_name})"
            )
            full_quote = (
                f"[PROXY: {pat_name}] " + quote +
                " — Note: BIDU does not disclose quarterly AI Cloud standalone; "
                "this figure is Baidu Core non-online marketing revenue, of which "
                "AI Cloud is the dominant component."
            )
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
                    meta["id"], "cloud_segment_revenue", val_rmb_m,
                    f"RMB {val_rmb_m:,.0f}M (proxy)", "USD_millions",
                    full_quote[:500], None, locator,
                    "inferred", 0.8, ACTOR, "0.1.0-draft", now,
                    value_usd, fx_rate, fx_date, "CNY",
                    ptype, 3, "standalone_quarterly",
                ),
            )
            conn.execute(
                """INSERT INTO audit_log (ts, actor, action, target_table, target_id, payload)
                VALUES (?, ?, 'bidu_cloud_6k_proxy_extracted', 'extractions', ?, ?)""",
                (now, ACTOR, cur.lastrowid, json.dumps({
                    "period_of_report": meta["period_of_report"],
                    "fiscal_year": meta["fiscal_year"],
                    "period_type": ptype,
                    "pattern": pat_name,
                    "value_rmb_m": val_rmb_m,
                    "value_usd_m": value_usd,
                }, sort_keys=True)),
            )
            inserted += 1
    print(f"inserted {inserted}, skipped {skipped} existing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
