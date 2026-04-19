#!/usr/bin/env python3
"""Backfill `extraction_evidence` rows for XBRL-sourced extractions.

XBRL values arrive without a filing-text quote — the original extractor
stores only `quote="XBRL: <concept>"` as a placeholder. This script
locates the actual value in the filing HTML (if archived locally under
`data/_sources/<TICKER>/_raw/`) and writes a real quote to
`extraction_evidence.excerpt_text`. Once populated, the Excel citation
formatter surfaces `Quote: "..."` automatically via
`citations._get_verification_badge`.

Strategy:
  1. Iterate every extraction row with `extracting_model='xbrl-verified'`
     or `xbrl-companyfacts'` that has a non-empty period_type.
  2. Find the canonical filing HTML by (ticker, form_type, period_of_report).
  3. Search the text for the value formatted a few ways:
       - $X,XXX.X  / X,XXX.X / X,XXX / X.X billion
  4. For the first match that's in a sentence-shaped context (<200 chars,
     contains a revenue/capex/cash-flow keyword within 150 chars), grab
     the surrounding sentence and store it as evidence.
  5. Skip rows that already have an evidence row.

Usage:
    python scripts/backfill_xbrl_quotes.py [--dry-run] [--limit N]
                                           [--tickers AMZN,MSFT,...]
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from capex.db import Database
from capex.read.text import extract_text

XBRL_MODELS = ("xbrl-verified", "xbrl-companyfacts")

# Metric → nearby-context keywords that help confirm a match is the
# right number. Values tend to appear in many places in a filing;
# these keywords bias toward the canonical line item.
METRIC_KEYWORDS: dict[str, list[str]] = {
    "revenue": [
        "total revenues", "total revenue", "net sales", "revenues",
        "sales", "net revenues",
    ],
    "capital_expenditures": [
        "purchases of property and equipment",
        "purchase of property and equipment",
        "payments for property and equipment",
        "additions to property and equipment",
        "capital expenditures",
    ],
    "operating_cash_flow": [
        "net cash provided by operating activities",
        "net cash from operating activities",
        "net cash provided by (used in) operating activities",
        "cash flow from operations",
    ],
    "depreciation_amortization": [
        "depreciation and amortization",
        "depreciation, amortization",
    ],
    "property_plant_equipment_net": [
        "property and equipment, net",
        "property, plant and equipment, net",
    ],
    "cloud_segment_revenue": [
        "aws", "intelligent cloud", "google cloud",
        "cloud services and license support",
    ],
}


def _find_filing_path(ticker: str, form_type: str, period: str) -> Path | None:
    """Look up the locally-archived filing HTML/PDF."""
    base = REPO_ROOT / "data" / "_sources" / ticker / "_raw"
    if not base.exists():
        return None
    # Canonical format: [yyyy.mm.dd][TICKER][TOKEN][FORM].htm.
    # Match on period + form_type (the filing date in the filename is
    # the FILING date, not period_of_report, so exact filename match
    # isn't possible — scan all and pick one with matching period token).
    # Cheap heuristic: AR ends on FY end (usually Dec but varies).
    candidates = sorted(base.glob(f"*{ticker}*{form_type}*.htm"))
    if not candidates:
        # Some files use the bare filing name (baba-20250331.htm style).
        candidates = sorted(base.glob("*.htm"))
    if not candidates:
        return None
    # Return candidate whose sidecar (.fetch.json) matches period_of_report.
    import json as _json
    for c in candidates:
        side = c.with_suffix(c.suffix + ".fetch.json")
        if not side.exists():
            continue
        try:
            meta = _json.loads(side.read_text())
        except Exception:
            continue
        if meta.get("period_of_report") == period and meta.get("form_type") == form_type:
            return c
    # Fallback: if exactly one candidate matches form_type, return it.
    return None


_VALUE_PATTERNS = [
    # $12,345.6 million / $12,345 / 12,345.6 million / 12,345 million
    r"\$?\s*{int}(?:\.\d+)?\s*(?:million|billion|M|B|bn|billion)?",
]


def _candidates_for_value(val_millions: float) -> list[re.Pattern]:
    """Return regex patterns that could match the given value in text."""
    val_int = int(round(val_millions))
    # Only match the millions-level integer formatted with thousand
    # separators. Avoid fuzzy matches on raw number fragments.
    s_mn = f"{val_int:,}"
    patterns = [
        re.compile(rf"\$?\s*{re.escape(s_mn)}(?:\.\d+)?\s*(?:million|M)?\b", re.IGNORECASE),
    ]
    # If value is ≥1000M, also try billion representation
    if val_millions >= 1000:
        val_b = val_millions / 1000.0
        s_b1 = f"{val_b:.1f}"
        s_b2 = f"{val_b:.2f}"
        patterns.append(re.compile(rf"\$?\s*{re.escape(s_b1)}\s*(?:billion|B|bn)\b", re.IGNORECASE))
        patterns.append(re.compile(rf"\$?\s*{re.escape(s_b2)}\s*(?:billion|B|bn)\b", re.IGNORECASE))
    return patterns


def _extract_quote(text: str, val_millions: float, metric_key: str) -> str | None:
    """Find the sentence containing `val_millions` near a metric keyword."""
    if val_millions <= 0:
        return None
    patterns = _candidates_for_value(val_millions)
    keywords = METRIC_KEYWORDS.get(metric_key, [])
    for pat in patterns:
        for m in pat.finditer(text):
            # Get the surrounding 250 chars and check for a keyword
            start = max(0, m.start() - 180)
            end = min(len(text), m.end() + 120)
            ctx = text[start:end]
            if not keywords or any(k.lower() in ctx.lower() for k in keywords):
                # Trim to the enclosing sentence
                before = ctx[: m.start() - start]
                after = ctx[m.end() - start:]
                sent_start = max(
                    before.rfind(". "), before.rfind(".\n"),
                    before.rfind("!"), before.rfind("?"), 0,
                )
                sent_end = min(
                    after.find(". ") + m.end() - start,
                    len(ctx),
                )
                if sent_end <= m.end() - start:
                    sent_end = min(m.end() - start + 80, len(ctx))
                quote = ctx[sent_start:sent_end].strip(" .\n")
                if len(quote) > 300:
                    quote = quote[:297] + "..."
                return quote
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--tickers", default=None)
    args = ap.parse_args()

    db = Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT e.id AS ext_id, e.metric_key, e.value, e.value_usd,
                   e.period_type, e.extracting_model,
                   sd.ticker, sd.form_type, sd.period_of_report
            FROM extractions e
            JOIN source_documents sd ON e.source_document_id = sd.id
            WHERE e.extracting_model IN ({p})
              AND e.period_type IN ('Q1','Q2','Q3','Q4','H1','9M','FY')
              AND NOT EXISTS (
                SELECT 1 FROM extraction_evidence ev
                WHERE ev.extraction_id = e.id
                  AND ev.excerpt_role = 'primary_value'
              )
            ORDER BY sd.ticker, sd.period_of_report DESC
            """.format(p=",".join("?" * len(XBRL_MODELS))),
            XBRL_MODELS,
        ).fetchall()

    if args.tickers:
        allowed = set(t.strip() for t in args.tickers.split(","))
        rows = [r for r in rows if r["ticker"] in allowed]
    if args.limit:
        rows = list(rows)[: args.limit]
    print(f"candidate rows: {len(rows)}")

    # Cache filing text to avoid re-parsing the same file N times.
    text_cache: dict[Path, str] = {}

    matched = 0
    no_filing = 0
    no_match = 0
    with db.mutating() as conn:
        for r in rows:
            filing = _find_filing_path(r["ticker"], r["form_type"], r["period_of_report"])
            if not filing:
                no_filing += 1
                continue
            if filing not in text_cache:
                try:
                    text_cache[filing] = extract_text(filing)
                except Exception:
                    text_cache[filing] = ""
            text = text_cache[filing]
            if not text:
                no_filing += 1
                continue
            # Prefer value_usd for the numeric match (it's in millions)
            # since the filing displays USD millions.
            val = abs(r["value_usd"]) if r["value_usd"] else abs(r["value"] or 0)
            quote = _extract_quote(text, val, r["metric_key"])
            if not quote:
                no_match += 1
                continue
            if args.dry_run:
                print(f"  {r['ticker']} {r['period_of_report']} {r['metric_key']}: "
                      f"{quote[:150]}")
                matched += 1
                continue
            conn.execute(
                """
                INSERT INTO extraction_evidence (
                    extraction_id, excerpt_text, excerpt_location,
                    excerpt_role, created_at
                ) VALUES (?, ?, ?, 'primary_value', ?)
                """,
                (r["ext_id"], quote, str(filing.name), now),
            )
            matched += 1

    print(f"matched={matched}, no_filing={no_filing}, no_match={no_match}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
