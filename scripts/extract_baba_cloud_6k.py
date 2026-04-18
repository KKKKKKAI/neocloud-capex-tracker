#!/usr/bin/env python3
"""Extract Alibaba Cloud Intelligence Group quarterly revenue from 6-Ks.

Fetches each BABA 6-K press release HTML from the SEC source_url,
regex-matches the cloud-segment revenue line (`Cloud Intelligence
Group` / `Cloud Computing` / `Cloud` depending on vintage), and writes
the value to `extractions` as `cloud_segment_revenue` with
`period_type='Q1'/'Q2'/'Q3'/'Q4'`.

Rate-limited to 2 req/s to respect SEC EDGAR fair-use guidelines.

Usage:
    python scripts/extract_baba_cloud_6k.py [--dry-run]
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

ACTOR = "baba-cloud-6k@0.1.0"
HEADERS = {"User-Agent": "capex-research research@example.com"}
RATE_DELAY = 0.5  # seconds between requests

# Patterns tried in order, newest naming first. Each captures RMB value
# in millions.
PATTERNS = [
    # Modern (2024+): "revenue from Cloud Intelligence Group was RMB<N> million"
    r"(?is)Cloud\s+Intelligence\s+Group[^.]{0,250}?RMB\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*million",
    # 2017-2023: "revenue from Cloud was RMB<N> million" or
    #            "Cloud Computing ... was RMB<N> million"
    r"(?is)(?:Cloud\s+Computing|Cloud)\s*(?:and\s+Internet\s+services?\s*)?[^.]{0,180}?(?:[Rr]evenue[^.]{0,80})?(?:was|were|totaled)?\s*RMB\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*million",
]

# Stricter pattern: require "Cloud" line that's clearly about revenue
# (avoid matching "cloud cost" etc.)
REVENUE_CONTEXT_RE = re.compile(
    r"(?is)(cloud\s+intelligence\s+group|cloud\s+computing|cloud)[^.\n]{0,300}?"
    r"(?:revenue|revenues)[^.\n]{0,100}?(?:was|were|of|totaled)?\s*RMB\s*"
    r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*million"
)


def _q_token_from_period(period_of_report: str) -> str:
    """Infer period_type from period_of_report month. BABA FYE = March.

    BABA fiscal quarters:
      Q1 fiscal = Apr-Jun (period 06-30) — we store as calendar Q2 value
      Q2 fiscal = Jul-Sep (period 09-30)
      Q3 fiscal = Oct-Dec (period 12-31)
      Q4 fiscal = Jan-Mar (period 03-31)

    BABA 6-K press releases are standalone 3-month values (per coverage.yaml
    quarterly_convention.default = standalone_quarterly). We map to the
    period_type that equals the CALENDAR quarter of period_of_report so
    the chart labels correctly.
    """
    month = int(period_of_report[5:7])
    if month in (1, 2, 3):
        return "Q1"
    if month in (4, 5, 6):
        return "Q2"
    if month in (7, 8, 9):
        return "Q3"
    return "Q4"


def _fetch(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  fetch error for {url}: {e}", file=sys.stderr)
        return None


def _strip(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"&[a-z#0-9]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _extract_cloud_rmb(text: str) -> tuple[float, str] | None:
    m = REVENUE_CONTEXT_RE.search(text)
    if m:
        val = float(m.group(2).replace(",", ""))
        return (val, m.group(0)[:240])
    # Loose fallback
    for pat in PATTERNS:
        m = re.search(pat, text)
        if m:
            val = float(m.group(1).replace(",", ""))
            return (val, m.group(0)[:240])
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="Only process first N filings")
    args = ap.parse_args()

    db = Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Load all BABA 6-K source_documents ordered by period
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, period_of_report, fiscal_year, source_url
            FROM source_documents
            WHERE ticker='BABA' AND form_type='6-K' AND source_url LIKE 'https://%'
            ORDER BY period_of_report
            """
        ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"processing {len(rows)} BABA 6-K filings")

    pending: list[tuple[dict, float, str, str]] = []
    fail_count = 0
    for r in rows:
        time.sleep(RATE_DELAY)
        html = _fetch(r["source_url"])
        if not html:
            fail_count += 1
            continue
        text = _strip(html)
        hit = _extract_cloud_rmb(text)
        period = r["period_of_report"]
        if not hit:
            print(f"  {period}: NO MATCH")
            fail_count += 1
            continue
        val_rmb, quote = hit
        ptype = _q_token_from_period(period)
        pending.append((
            {"id": r["id"], "period_of_report": period, "fiscal_year": r["fiscal_year"],
             "source_url": r["source_url"]},
            val_rmb, quote, ptype,
        ))
        print(f"  {period} [{ptype}]: RMB {val_rmb:,.0f}M")

    print(f"\nextracted {len(pending)} values, failed {fail_count}")
    if args.dry_run:
        print("dry-run; not committing")
        return 0

    inserted = 0
    skipped = 0
    with db.mutating() as conn:
        for meta, val_rmb, quote, ptype in pending:
            value_usd, fx_rate, fx_date = normalize_to_usd(
                val_rmb, "CNY", meta["period_of_report"],
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
                    meta["id"], "cloud_segment_revenue", val_rmb,
                    f"RMB {val_rmb:,.0f}M", "USD_millions",
                    quote, None, "6-K Press Release — Cloud Intelligence Group",
                    "direct", None, ACTOR, "0.1.0-draft", now,
                    value_usd, fx_rate, fx_date, "CNY",
                    ptype, 3, "standalone_quarterly",
                ),
            )
            conn.execute(
                """INSERT INTO audit_log (ts, actor, action, target_table, target_id, payload)
                VALUES (?, ?, 'baba_cloud_6k_extracted', 'extractions', ?, ?)""",
                (now, ACTOR, cur.lastrowid, json.dumps({
                    "period_of_report": meta["period_of_report"],
                    "fiscal_year": meta["fiscal_year"],
                    "period_type": ptype,
                    "value_rmb_m": val_rmb,
                    "value_usd_m": value_usd,
                }, sort_keys=True)),
            )
            inserted += 1
    print(f"inserted {inserted}, skipped {skipped} existing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
