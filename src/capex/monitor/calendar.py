"""Earnings calendar sync via Alpha Vantage.

Pulls upcoming earnings dates for our tracked companies and stores them
in the fiscal_calendar table. The monitor uses these dates to know
exactly when to start polling SEC EDGAR for new filings.

Alpha Vantage free tier: 5 calls/min, 500/day. We need ~1 call/week.
Register at https://www.alphavantage.co/support/#api-key

Usage:
    capex calendar sync             # pull next 3 months
    capex calendar show             # show upcoming dates
    capex calendar show --week      # this week only
"""
from __future__ import annotations

import csv
import io
import os
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..db import Database
from ..extract.coverage import get_all_tickers, get_company_treatment

ALPHA_VANTAGE_URL = (
    "https://www.alphavantage.co/query"
    "?function=EARNINGS_CALENDAR&horizon=3month&apikey={api_key}"
)

# Map filing cadence to expected form types
FORM_TYPE_MAP = {
    "10-K": "10-K",
    "10-Q": "10-Q",
    "20-F": "20-F",
    "HK-AR": "HK-AR",
}


def sync_earnings_calendar(
    api_key: str | None = None,
    horizon: str = "3month",
    *,
    db: Database | None = None,
) -> dict[str, Any]:
    """Pull upcoming earnings dates from Alpha Vantage.

    Fetches CSV, filters to our tracked tickers, upserts into
    fiscal_calendar table.

    Returns: {synced: int, skipped: int, errors: list}
    """
    api_key = api_key or os.environ.get("ALPHA_VANTAGE_API_KEY", "demo")
    db = db or Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    our_tickers = set(get_all_tickers())

    url = ALPHA_VANTAGE_URL.format(api_key=api_key)
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")

    reader = csv.DictReader(io.StringIO(text))
    synced = 0
    skipped = 0
    errors = []

    for row in reader:
        symbol = row.get("symbol", "").strip()
        if symbol not in our_tickers:
            continue

        report_date = row.get("reportDate", "").strip()
        fiscal_end = row.get("fiscalDateEnding", "").strip()

        if not report_date or not fiscal_end:
            skipped += 1
            continue

        # Determine expected form type from company config
        company = get_company_treatment(symbol)
        form_type = None
        if company:
            cadence = company.filing_cadence
            # Is this a quarter-end or year-end?
            fye = int(fiscal_end[5:7])
            if company.filing_cadence.get("annual") and fye == _fye_month(company):
                form_type = cadence.get("annual")
            else:
                form_type = cadence.get("quarterly") or cadence.get("annual")

        try:
            with db.mutating() as conn:
                conn.execute(
                    """
                    INSERT INTO fiscal_calendar
                        (ticker, report_date, fiscal_date_ending, form_type,
                         status, source, updated_at)
                    VALUES (?, ?, ?, ?, 'upcoming', 'alpha_vantage', ?)
                    ON CONFLICT(ticker, fiscal_date_ending) DO UPDATE SET
                        report_date = excluded.report_date,
                        form_type = excluded.form_type,
                        source = excluded.source,
                        updated_at = excluded.updated_at
                    """,
                    (symbol, report_date, fiscal_end, form_type, now),
                )
                synced += 1
        except Exception as e:
            errors.append(f"{symbol}: {e}")

    return {"synced": synced, "skipped": skipped, "errors": errors}


def _fye_month(company) -> int:
    """Get the fiscal year end month number from coverage config."""
    # Parse from coverage_start or filing_cadence
    # Fallback: read from DB
    db = Database()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT fiscal_year_end_month FROM companies WHERE ticker = ?",
            (company.ticker,),
        ).fetchone()
    return row["fiscal_year_end_month"] if row else 12


def get_todays_earnings(*, db: Database | None = None) -> list[dict[str, Any]]:
    """Return companies with earnings scheduled for today."""
    db = db or Database()
    today = date.today().isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT ticker, report_date, fiscal_date_ending, form_type, status
            FROM fiscal_calendar
            WHERE report_date = ? AND status = 'upcoming'
            ORDER BY ticker
            """,
            (today,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_upcoming_earnings(
    days: int = 7,
    *,
    db: Database | None = None,
) -> list[dict[str, Any]]:
    """Return companies with earnings in the next N days."""
    db = db or Database()
    today = date.today()
    end = (today + timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT ticker, report_date, fiscal_date_ending, form_type, status
            FROM fiscal_calendar
            WHERE report_date >= ? AND report_date <= ?
            ORDER BY report_date, ticker
            """,
            (today.isoformat(), end),
        ).fetchall()
    return [dict(r) for r in rows]


def update_status(
    ticker: str,
    fiscal_date_ending: str,
    status: str,
    *,
    db: Database | None = None,
) -> None:
    """Update the status of a fiscal calendar entry."""
    db = db or Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.mutating() as conn:
        conn.execute(
            """
            UPDATE fiscal_calendar SET status = ?, updated_at = ?
            WHERE ticker = ? AND fiscal_date_ending = ?
            """,
            (status, now, ticker, fiscal_date_ending),
        )


def add_manual_entry(
    ticker: str,
    report_date: str,
    fiscal_date_ending: str,
    form_type: str | None = None,
    *,
    db: Database | None = None,
) -> None:
    """Manually add an earnings date (for companies not in Alpha Vantage)."""
    db = db or Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.mutating() as conn:
        conn.execute(
            """
            INSERT INTO fiscal_calendar
                (ticker, report_date, fiscal_date_ending, form_type,
                 status, source, updated_at)
            VALUES (?, ?, ?, ?, 'upcoming', 'manual', ?)
            ON CONFLICT(ticker, fiscal_date_ending) DO UPDATE SET
                report_date = excluded.report_date,
                updated_at = excluded.updated_at
            """,
            (ticker, report_date, fiscal_date_ending, form_type, now),
        )
