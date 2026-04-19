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
from dataclasses import dataclass
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


def get_recent_earnings(
    days: int = 30,
    *,
    db: Database | None = None,
) -> list[dict[str, Any]]:
    """Return fiscal_calendar entries whose report_date is in the past N days."""
    db = db or Database()
    today = date.today()
    start = (today - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT ticker, report_date, fiscal_date_ending, form_type,
                   status, updated_at
            FROM fiscal_calendar
            WHERE report_date >= ? AND report_date < ?
            ORDER BY report_date DESC, ticker
            """,
            (start, today.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


@dataclass
class CalendarEvent:
    ticker: str
    company_name: str
    report_date: str            # YYYY-MM-DD
    fiscal_date_ending: str     # YYYY-MM-DD
    form_type: str | None
    status: str                 # upcoming|detected|fetched|extracted|failed
    source_url: str | None      # from source_documents if filing landed
    days_from_today: int        # negative = past, 0 = today, positive = upcoming
    fiscal_year: int            # derived from fiscal_date_ending + FYE month
    period_label: str           # "Q1"/"Q2"/"Q3"/"Q4"/"FY"
    updated_at: str             # from fiscal_calendar or source_documents.fetched_at


def _derive_fy_and_period(fiscal_date_ending: str, fye_month: int) -> tuple[int, str]:
    """Compute (fiscal_year, period_label) from a period-end date + FYE month.

    FY convention: if the period-end month is <= FYE month, period belongs
    to the fiscal year labelled by the calendar year of the end date
    (e.g. MSFT FYE=6, 2026-03-31 → FY2026 Q3). Otherwise period belongs to
    fy+1 (e.g. MSFT 2025-09-30 → FY2026 Q1).
    """
    fy = date.fromisoformat(fiscal_date_ending)
    if fy.month <= fye_month:
        fiscal_year = fy.year
    else:
        fiscal_year = fy.year + 1
    if fy.month == fye_month:
        return fiscal_year, "FY"
    # Quarter within fiscal year (1..4)
    q = (((fy.month - fye_month - 1) % 12) // 3) + 1
    return fiscal_year, f"Q{q}"


def query_for_viewer(
    conn,
    upcoming_days: int = 90,
    past_days: int = 30,
    ticker_filter: str | None = None,
) -> list[CalendarEvent]:
    """Return unified list of earnings events for the viewer.

    Combines:
    - Upcoming events from fiscal_calendar (report_date in [today, today+upcoming_days])
    - Past events from source_documents (filing_date in [today-past_days, today))
      — merged with any matching fiscal_calendar row for status.

    Events are deduped by (ticker, fiscal_date_ending) — the upcoming
    calendar row wins if both exist (keeps announced dates visible until
    filing lands). Results sorted by report_date ascending.
    """
    today = date.today()
    today_iso = today.isoformat()
    upcoming_end = (today + timedelta(days=upcoming_days)).isoformat()
    past_start = (today - timedelta(days=past_days)).isoformat()

    # Company lookup: ticker → (name, fye_month)
    companies = {
        r["ticker"]: (r["name"], r["fiscal_year_end_month"])
        for r in conn.execute(
            "SELECT ticker, name, fiscal_year_end_month FROM companies"
        ).fetchall()
    }

    # Source-doc lookup: (ticker, period_of_report) → (form_type, source_url, filing_date)
    src_rows = conn.execute(
        """
        SELECT ticker, form_type, period_of_report, source_url, filing_date,
               fetched_at
        FROM source_documents
        """
    ).fetchall()
    src_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for r in src_rows:
        key = (r["ticker"], r["period_of_report"])
        # Keep earliest filing per period (first canonical filing)
        existing = src_by_key.get(key)
        if existing is None or r["filing_date"] < existing["filing_date"]:
            src_by_key[key] = dict(r)

    events: dict[tuple[str, str], CalendarEvent] = {}

    # --- Upcoming: from fiscal_calendar ---
    cal_rows = conn.execute(
        """
        SELECT ticker, report_date, fiscal_date_ending, form_type, status,
               updated_at
        FROM fiscal_calendar
        WHERE report_date >= ? AND report_date <= ?
        ORDER BY report_date, ticker
        """,
        (today_iso, upcoming_end),
    ).fetchall()
    for r in cal_rows:
        tk = r["ticker"]
        if ticker_filter and tk != ticker_filter:
            continue
        cname, fye = companies.get(tk, (tk, 12))
        fy, period = _derive_fy_and_period(r["fiscal_date_ending"], fye)
        src = src_by_key.get((tk, r["fiscal_date_ending"]))
        dt = (date.fromisoformat(r["report_date"]) - today).days
        events[(tk, r["fiscal_date_ending"])] = CalendarEvent(
            ticker=tk,
            company_name=cname,
            report_date=r["report_date"],
            fiscal_date_ending=r["fiscal_date_ending"],
            form_type=r["form_type"],
            status=r["status"],
            source_url=src["source_url"] if src else None,
            days_from_today=dt,
            fiscal_year=fy,
            period_label=period,
            updated_at=r["updated_at"],
        )

    # --- Past: from source_documents in the window ---
    past_src_rows = conn.execute(
        """
        SELECT ticker, form_type, period_of_report, source_url, filing_date,
               fetched_at
        FROM source_documents
        WHERE filing_date >= ? AND filing_date < ?
        ORDER BY filing_date DESC
        """,
        (past_start, today_iso),
    ).fetchall()
    # Status merge: prefer fiscal_calendar row if exists
    cal_status_rows = conn.execute(
        "SELECT ticker, fiscal_date_ending, status, updated_at FROM fiscal_calendar"
    ).fetchall()
    cal_status: dict[tuple[str, str], tuple[str, str]] = {
        (r["ticker"], r["fiscal_date_ending"]): (r["status"], r["updated_at"])
        for r in cal_status_rows
    }
    for r in past_src_rows:
        tk = r["ticker"]
        if ticker_filter and tk != ticker_filter:
            continue
        fde = r["period_of_report"]
        key = (tk, fde)
        if key in events:
            # Already has an upcoming entry (shouldn't happen — upcoming is future)
            continue
        cname, fye = companies.get(tk, (tk, 12))
        fy, period = _derive_fy_and_period(fde, fye)
        status, upd = cal_status.get(key, ("extracted", r["fetched_at"]))
        dt = (date.fromisoformat(r["filing_date"]) - today).days
        events[key] = CalendarEvent(
            ticker=tk,
            company_name=cname,
            report_date=r["filing_date"],
            fiscal_date_ending=fde,
            form_type=r["form_type"],
            status=status,
            source_url=r["source_url"],
            days_from_today=dt,
            fiscal_year=fy,
            period_label=period,
            updated_at=upd,
        )

    return sorted(events.values(), key=lambda e: (e.report_date, e.ticker))


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
