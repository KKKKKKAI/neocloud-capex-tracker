"""Filing watcher — polls SEC EDGAR / HKEX for new filings.

Called on earnings days by the cron runner. Polls until the filing
appears, then downloads and triggers extraction.
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..db import Database
from ..fetch import get_user_agent
from ..fetch.dispatcher import fetch_and_record
from ..extract.coverage import get_company_treatment, get_dataset_treatment
from .calendar import update_status


def poll_sec_latest(
    ticker: str,
    form_type: str,
    *,
    db: Database | None = None,
) -> dict[str, Any] | None:
    """Check SEC EDGAR for the latest filing of a given type.

    Returns {period, filed, accession, doc} or None if nothing new.
    """
    db = db or Database()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT edgar_cik FROM companies WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    if not row or not row["edgar_cik"]:
        return None

    cik = row["edgar_cik"]
    padded = cik.lstrip("0").zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{padded}.json"
    ua = get_user_agent()
    req = urllib.request.Request(url, headers={"User-Agent": ua})

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    accns = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])

    for i, f in enumerate(forms):
        if f == form_type or f == f"{form_type}/A":
            return {
                "period": report_dates[i] if i < len(report_dates) else "",
                "filed": dates[i] if i < len(dates) else "",
                "accession": accns[i] if i < len(accns) else "",
                "doc": docs[i] if i < len(docs) else "",
            }
    return None


def already_in_db(
    ticker: str,
    form_type: str,
    period: str,
    *,
    db: Database | None = None,
) -> bool:
    """Check if we already have this filing in source_documents."""
    db = db or Database()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM source_documents "
            "WHERE ticker = ? AND form_type = ? AND period_of_report = ?",
            (ticker, form_type, period),
        ).fetchone()
    return row is not None


def watch_and_extract(
    ticker: str,
    form_type: str,
    *,
    backend: Any,
    db: Database | None = None,
    metric_keys: list[str] | None = None,
    max_polls: int = 48,
    interval: int = 1800,
) -> dict[str, Any]:
    """Poll for a filing, download when found, run extraction.

    Args:
        ticker: company ticker
        form_type: expected filing type (10-Q, 10-K, 20-F, 6-K)
        backend: CLIBackend for LLM calls
        db: database instance
        metric_keys: metrics to extract (default: all configured)
        max_polls: maximum polling attempts (default: 48 = 24 hours)
        interval: seconds between polls (default: 1800 = 30 min)

    Returns:
        {status, ticker, period, metrics_extracted, issues}
    """
    db = db or Database()

    if metric_keys is None:
        metric_keys = [
            "capital_expenditures", "revenue", "operating_cash_flow",
            "depreciation_amortization", "property_plant_equipment_net",
            "cloud_segment_revenue",
        ]

    for attempt in range(1, max_polls + 1):
        latest = poll_sec_latest(ticker, form_type, db=db)

        if latest and latest.get("period") and not already_in_db(
            ticker, form_type, latest["period"], db=db
        ):
            # Found new filing!
            print(f"  [{ticker}] New {form_type} detected: period={latest['period']}, "
                  f"filed={latest['filed']}")

            # 1. Download
            try:
                fetch_and_record(ticker, form_type, db=db)
            except Exception as e:
                return {
                    "status": "fetch_failed",
                    "ticker": ticker,
                    "error": str(e),
                }

            # 2. LLM dual-agent extraction for each metric
            from ..extract.router import extract_metric

            extracted = []
            issues = []
            for metric_key in metric_keys:
                try:
                    r = extract_metric(
                        ticker, metric_key,
                        period=latest["period"],
                        write=True, backend=backend, db=db,
                    )
                    if r.status == "success":
                        extracted.append(metric_key)
                    else:
                        issues.append(f"{metric_key}: {r.status}")
                except Exception as e:
                    issues.append(f"{metric_key}: {type(e).__name__}: {e}")

            # 3. Update calendar status
            try:
                update_status(ticker, latest["period"], "extracted", db=db)
            except Exception:
                pass

            return {
                "status": "success",
                "ticker": ticker,
                "period": latest["period"],
                "filed": latest["filed"],
                "metrics_extracted": extracted,
                "issues": issues,
            }

        if attempt < max_polls:
            print(f"  [{ticker}] Poll {attempt}/{max_polls}: no new {form_type} yet. "
                  f"Next check in {interval}s...")
            time.sleep(interval)

    return {"status": "timeout", "ticker": ticker, "form_type": form_type}
