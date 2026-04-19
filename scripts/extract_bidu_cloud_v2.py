#!/usr/bin/env python3
"""BIDU quarterly cloud v2 — Total − Online marketing − iQIYI formula.

User's principle (paraphrased from BIDU 20-F footnote):
    Baidu reports revenue as "Online marketing services" and "Others".
    "Others" mainly = cloud + iQIYI video membership services.
    iQIYI's standalone revenue is separately disclosed in the segment
    table.
    => cloud ≈ Others − iQIYI
       ≈ (Total revenue − Online marketing) − iQIYI

This script:
  1. Walks EDGAR for every BIDU 6-K earnings press release (including
     Q4 full-year filings, which are filed in Feb and were missing
     from the DB before).
  2. Fetches the Exhibit 99.1 HTML and extracts three numbers per
     quarter — Total revenue, Online marketing revenue, iQIYI revenue.
  3. Computes `cloud = Total − Online − iQIYI` per quarter and writes
     the result to `extractions` as `cloud_segment_revenue` with
     `extracting_model='bidu-cloud-total-minus-online-minus-iqiyi@0.3.0'`
     and `period_type` set by the filing's calendar quarter.
  4. Replaces older `bidu-cloud-6k-proxy@0.1.0` and
     `bidu-cloud-scaled@0.2.0` rows so the chart picks up the new
     values. Adds new source_document rows for Q4 filings that weren't
     previously captured.

Usage:
    python scripts/extract_bidu_cloud_v2.py [--dry-run] [--limit N]
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

ACTOR = "bidu-cloud-total-minus-online-minus-iqiyi@0.3.0"
HEADERS = {"User-Agent": "capex-research research@example.com"}
RATE_DELAY = 0.4

CIK = "0001329099"
OLD_MODELS_TO_DELETE = [
    "bidu-cloud-6k-proxy@0.1.0",
    "bidu-cloud-scaled@0.2.0",
]


# --------------------------------------------------------------------
# Patterns for narrative paragraphs (not tables). Each pattern captures
# (value, unit). Try permissive matching to handle wording variants
# across 2015-2025.
# --------------------------------------------------------------------
NUMBER = r"([\d,]+(?:\.\d+)?)"
UNIT   = r"(million|billion)"
RMB    = r"RMB\s*"

PAT_TOTAL_PRIMARY = re.compile(
    rf"(?i)total\s+revenues?\s+(?:in\s+the\s+\w+\s+quarter[^.]{{0,60}}?)?"
    rf"(?:was|were|of)\s+{RMB}{NUMBER}\s+{UNIT}"
)
PAT_TOTAL_FALLBACK = re.compile(
    rf"(?i)total\s+revenues?\s+for\s+(?:the\s+)?(?:fourth|third|second|first|full|fiscal)?\s*"
    rf"(?:quarter|year)[^.]{{0,120}}?{RMB}{NUMBER}\s+{UNIT}"
)
PAT_ONLINE = re.compile(
    rf"(?i)online\s+marketing\s+(?:services?\s+)?revenue[^.]{{0,60}}?"
    rf"(?:was|were)\s+{RMB}{NUMBER}\s+{UNIT}"
)
PAT_NON_ONLINE = re.compile(
    rf"(?i)non[\- ]online\s+marketing\s+(?:services?\s+)?revenue[^.]{{0,60}}?"
    rf"(?:was|were)\s+{RMB}{NUMBER}\s+{UNIT}"
)
PAT_OTHER = re.compile(
    rf"(?i)other\s+(?:services?\s+)?revenues?[^.]{{0,60}}?"
    rf"(?:was|were)\s+{RMB}{NUMBER}\s+{UNIT}"
)
PAT_IQIYI = re.compile(
    rf"(?i)(?:revenue\s+from\s+iqiyi|iqiyi\s+revenue)[^.]{{0,60}}?"
    rf"(?:was|were)\s+{RMB}{NUMBER}\s+{UNIT}"
)


def _parse_rmb_million(val_str: str, unit: str) -> float:
    val = float(val_str.replace(",", ""))
    return val * 1000.0 if unit.lower() == "billion" else val


def _match(pattern, text: str) -> float | None:
    m = pattern.search(text)
    if not m:
        return None
    try:
        return _parse_rmb_million(m.group(1), m.group(2))
    except ValueError:
        return None


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


def _extract_quarter_values(text: str) -> dict[str, float]:
    """Return whichever of {total, online, non_online, other, iqiyi} matched."""
    out: dict[str, float] = {}
    window = text[:120000]
    for key, pat in [
        ("total", PAT_TOTAL_PRIMARY),
        ("online", PAT_ONLINE),
        ("non_online", PAT_NON_ONLINE),
        ("other", PAT_OTHER),
        ("iqiyi", PAT_IQIYI),
    ]:
        v = _match(pat, window)
        if v is not None:
            out[key] = v
    if "total" not in out:
        v = _match(PAT_TOTAL_FALLBACK, window)
        if v is not None:
            out["total"] = v
    return out


def _fiscal_quarter_from_period(period: str) -> str:
    """BIDU FYE December → calendar quarter = fiscal quarter."""
    m = int(period[5:7])
    return {1: "Q1", 2: "Q1", 3: "Q1", 4: "Q2", 5: "Q2", 6: "Q2",
            7: "Q3", 8: "Q3", 9: "Q3", 10: "Q4", 11: "Q4", 12: "Q4"}[m]


# --------------------------------------------------------------------
# EDGAR walkers — find every BIDU 6-K earnings press release.
# --------------------------------------------------------------------
def _list_all_6k_filings() -> list[dict]:
    """Return [{filing_date, accession, primary_doc}, ...] for all BIDU 6-Ks."""
    url = f"https://data.sec.gov/submissions/CIK{CIK}.json"
    data = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(url, headers=HEADERS), timeout=15
        ).read()
    )
    r = data["filings"]["recent"]
    out = []
    for i in range(len(r["form"])):
        if r["form"][i] != "6-K":
            continue
        out.append({
            "filing_date": r["filingDate"][i],
            "accession": r["accessionNumber"][i],
            "primary_doc": r["primaryDocument"][i],
            "report_date": r.get("reportDate", [None] * len(r["form"]))[i],
        })
    return out


def _accession_ex991(accn: str) -> str | None:
    """Given accession '0001193125-25-028182', return URL to d*ex991.htm."""
    accn_nodash = accn.replace("-", "")
    idx = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accn_nodash}/"
    try:
        html = urllib.request.urlopen(
            urllib.request.Request(idx, headers=HEADERS), timeout=15,
        ).read().decode("utf-8", errors="replace")
    except Exception:
        return None
    for m in re.finditer(r'href="[^"]+/(d\d+d[^"]*ex99[^"]*\.htm)"', html):
        return idx + m.group(1)
    return None


def _period_from_filing_date(filing_date: str) -> str | None:
    """BIDU reporting schedule (empirical):
        Feb filing → prior year Q4 (period Dec 31)
        May filing → current year Q1 (period Mar 31)
        Aug filing → current year Q2 (period Jun 30)
        Nov filing → current year Q3 (period Sep 30)
    Also accept Apr, Jul, Oct as off-by-one variants.
    """
    year = int(filing_date[:4])
    month = int(filing_date[5:7])
    if month in (2, 3):
        return f"{year - 1:04d}-12-31"
    if month in (4, 5):
        return f"{year:04d}-03-31"
    if month in (7, 8):
        return f"{year:04d}-06-30"
    if month in (10, 11):
        return f"{year:04d}-09-30"
    return None


def _guess_period_from_text(text: str, filing_date: str) -> str | None:
    """Infer period; filing-date heuristic wins over text parsing to avoid
    accidentally latching onto prior-year comparison phrasing."""
    return _period_from_filing_date(filing_date)


# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--since", default="2015-01-01")
    args = ap.parse_args()

    db = Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print("discovering BIDU 6-K filings on EDGAR...")
    filings = _list_all_6k_filings()
    print(f"  {len(filings)} BIDU 6-K filings total")

    # Filter: earnings press releases only (Feb, Apr-May, Jul-Aug, Oct-Nov)
    # and within the date window.
    earnings_months = {"02", "04", "05", "07", "08", "10", "11"}
    filings = [f for f in filings
               if f["filing_date"] >= args.since
               and f["filing_date"][5:7] in earnings_months]
    print(f"  {len(filings)} candidate earnings filings after filtering")
    if args.limit:
        filings = filings[: args.limit]

    # Resolve ex99.1 URLs and extract
    records: list[dict] = []
    for f in filings:
        time.sleep(RATE_DELAY)
        url = _accession_ex991(f["accession"])
        if not url:
            print(f"  {f['filing_date']} {f['accession']}: no ex99.1")
            continue
        time.sleep(RATE_DELAY)
        html = _fetch(url)
        if not html or len(html) < 30000:
            sz = len(html) if html else 0
            print(f"  {f['filing_date']}: press release too small ({sz}), skip")
            continue
        text = _strip(html)
        period = _guess_period_from_text(text, f["filing_date"])
        if not period:
            continue
        vals = _extract_quarter_values(text)
        records.append({
            "filing_date": f["filing_date"],
            "accession": f["accession"],
            "source_url": url,
            "period_of_report": period,
            "values": vals,
        })
        # Compute cloud if possible
        total = vals.get("total")
        online = vals.get("online")
        iqiyi = vals.get("iqiyi")
        cloud = None
        if total and online and iqiyi:
            cloud = total - online - iqiyi
        elif vals.get("non_online"):
            cloud = vals["non_online"]  # proxy-only fallback (no iQIYI adjustment needed)
        print(f"  {f['filing_date']} period={period}: "
              f"total={total} online={online} non_online={vals.get('non_online')} "
              f"iqiyi={iqiyi} => cloud={cloud}")

    # Derived values per quarter
    pending_writes = []
    for rec in records:
        v = rec["values"]
        total, online, iqiyi = v.get("total"), v.get("online"), v.get("iqiyi")
        if total and online and iqiyi:
            cloud = total - online - iqiyi
            formula = "Total − Online marketing − iQIYI"
        elif v.get("non_online"):
            cloud = v["non_online"]
            formula = "Non-online marketing (fallback; Baidu Core non-ad, iQIYI already excluded)"
        else:
            continue
        pending_writes.append({
            **rec,
            "cloud_rmb_m": cloud,
            "formula": formula,
        })
    print(f"\nresolvable to cloud value: {len(pending_writes)} / {len(records)}")

    if args.dry_run:
        return 0

    # Find or create source_documents + write extractions
    inserted = 0
    created_source_docs = 0
    with db.mutating() as conn:
        # Delete old proxy/scaled rows
        deleted = conn.execute(
            "DELETE FROM extractions WHERE metric_key='cloud_segment_revenue' "
            f"AND extracting_model IN ({','.join('?'*len(OLD_MODELS_TO_DELETE))})",
            OLD_MODELS_TO_DELETE,
        ).rowcount
        print(f"  deleted {deleted} old proxy/scaled rows")

        for w in pending_writes:
            sd_row = conn.execute(
                "SELECT id FROM source_documents "
                "WHERE ticker='BIDU' AND form_type='6-K' AND period_of_report=?",
                (w["period_of_report"],),
            ).fetchone()
            if not sd_row:
                # Create synthetic source_document for the new Q4 6-K
                fy = int(w["period_of_report"][:4])
                token = _fiscal_quarter_from_period(w["period_of_report"])
                conn.execute(
                    """
                    INSERT INTO source_documents (
                        ticker, form_type, filing_date, period_of_report,
                        fiscal_year, period_token, sha256, raw_path,
                        source, source_url, accession_number, fetched_at,
                        fetcher_version, protocol_version
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "BIDU", "6-K", w["filing_date"], w["period_of_report"],
                        fy, token,
                        f"bidu-v2-{w['accession']}-{w['period_of_report']}",
                        f"6k://bidu-v2/{w['period_of_report']}",
                        "sec_edgar", w["source_url"], w["accession"],
                        now, "bidu-cloud-v2@0.3.0", "0.1.0-draft",
                    ),
                )
                sd_id = conn.execute(
                    "SELECT id FROM source_documents "
                    "WHERE ticker='BIDU' AND form_type='6-K' "
                    "AND period_of_report=?", (w["period_of_report"],),
                ).fetchone()["id"]
                created_source_docs += 1
            else:
                sd_id = sd_row["id"]

            ptype = _fiscal_quarter_from_period(w["period_of_report"])
            value_usd, fx_rate, fx_date = normalize_to_usd(
                w["cloud_rmb_m"], "CNY", w["period_of_report"],
            )
            existing = conn.execute(
                "SELECT id FROM extractions "
                "WHERE source_document_id=? AND metric_key=? "
                "AND extracting_model=? AND period_type=?",
                (sd_id, "cloud_segment_revenue", ACTOR, ptype),
            ).fetchone()
            if existing:
                continue
            v = w["values"]
            quote = (
                f"[{w['formula']}] "
                f"Total={v.get('total')}M, Online={v.get('online')}M, "
                f"iQIYI={v.get('iqiyi')}M, "
                f"non_online={v.get('non_online')}M "
                f"=> cloud={w['cloud_rmb_m']:.0f}M RMB"
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
                    sd_id, "cloud_segment_revenue", w["cloud_rmb_m"],
                    f"RMB {w['cloud_rmb_m']:,.0f}M", "USD_millions",
                    quote[:500], None,
                    "6-K earnings press release (Exhibit 99.1): Total − Online − iQIYI",
                    "inferred", 0.88, ACTOR, "0.1.0-draft", now,
                    value_usd, fx_rate, fx_date, "CNY",
                    ptype, 3, "standalone_quarterly",
                ),
            )
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, target_table, target_id, payload) "
                "VALUES (?, ?, 'bidu_cloud_v2_extracted', 'extractions', ?, ?)",
                (now, ACTOR, cur.lastrowid, json.dumps({
                    "period_of_report": w["period_of_report"],
                    "accession": w["accession"],
                    "values_rmb_m": v,
                    "cloud_rmb_m": w["cloud_rmb_m"],
                    "formula": w["formula"],
                }, sort_keys=True)),
            )
            inserted += 1

    print(f"  inserted={inserted}, replaced=old-proxy={deleted}, "
          f"new_source_docs={created_source_docs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
