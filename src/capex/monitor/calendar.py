"""Earnings calendar sync via Alpha Vantage.

SCOPE — read this first:
    Alpha Vantage is used SOLELY to discover forward-looking earnings
    *dates* (e.g. "GOOGL will report on 2026-04-29"). It NEVER touches
    financial data, never extracts a value, never writes to extractions
    or extraction_evidence. Its only output is rows in fiscal_calendar
    with status='upcoming'.

    All actual financial data extraction is our own LLM dual-agent
    framework reading filings from SEC EDGAR / HKEXnews — see
    src/capex/extract/extractors/llm_headless_filing.py and
    src/capex/extract/extractors/llm_headless.py.

Pulls upcoming earnings dates for our tracked companies and stores them
in the fiscal_calendar table. The monitor uses these dates to know
exactly when to start polling SEC EDGAR for new filings.

Alpha Vantage free tier: a small daily quota (check alphavantage.co);
we need one call per day at most.
Register at https://www.alphavantage.co/support/#api-key

Usage:
    capex calendar sync             # pull next 3 months
    capex calendar show             # show upcoming dates
    capex calendar show --week      # this week only
"""
from __future__ import annotations

import csv
import io
import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .. import settings
from ..db import Database
from ..extract.coverage import get_all_tickers
from .clock import utc_iso
from .watchlist import expected_form, sync_watchlist

ALPHA_VANTAGE_URL = (
    "https://www.alphavantage.co/query"
    "?function=EARNINGS_CALENDAR&horizon=3month&apikey={api_key}"
)

class CalendarError(RuntimeError):
    """The calendar can't be synced (bad key, provider error, bad reply)."""


# Keys that mean "not configured". Alpha Vantage answers "demo" with an
# error body, which used to parse as zero rows and exit green.
PLACEHOLDER_KEYS = frozenset({"", "demo", "your_key_here", "changeme", "none"})


def fetch_calendar_csv(api_key: str, horizon: str = "3month") -> str:
    url = ALPHA_VANTAGE_URL.format(api_key=api_key).replace(
        "horizon=3month", f"horizon={horizon}"
    )
    with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as resp:
        return resp.read().decode("utf-8")


def parse_calendar_csv(text: str) -> list[dict[str, str]]:
    """Rows of Alpha Vantage's EARNINGS_CALENDAR CSV.

    Raises CalendarError when the reply is an error body (JSON such as
    {"Information": "..."}) or lacks the expected CSV header.
    """
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            message = str(next(iter(json.loads(stripped).values())))
        except (ValueError, StopIteration, AttributeError):
            message = stripped[:200]
        raise CalendarError(f"Alpha Vantage returned an error instead of CSV: {message[:200]}")
    reader = csv.DictReader(io.StringIO(text))
    header = set(reader.fieldnames or [])
    if not {"symbol", "reportDate", "fiscalDateEnding"} <= header:
        raise CalendarError("unexpected Alpha Vantage reply (no earnings-calendar CSV header)")
    return list(reader)


def sync_earnings_calendar(
    api_key: str | None = None,
    horizon: str = "3month",
    *,
    db: Database | None = None,
    allow_demo_key: bool | None = None,
) -> dict[str, Any]:
    """Pull upcoming earnings dates from Alpha Vantage into fiscal_calendar.

    Only covered tickers are kept. The expected form comes from the
    watchlist (see watchlist.expected_form). Existing rows are updated
    only while still 'upcoming' and not entered by hand, so a sync never
    rewrites a row the watcher is processing or a manual correction.

    Returns: {synced: int, skipped: int, errors: list}
    Raises CalendarError for a missing/placeholder key or a provider error.
    """
    db = db or Database()
    if api_key is None:
        api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
    if allow_demo_key is None:
        allow_demo_key = settings.get("calendar.allow_demo_key", db)
    if api_key.strip().lower() in PLACEHOLDER_KEYS and not allow_demo_key:
        raise CalendarError(
            "ALPHA_VANTAGE_API_KEY is missing or a placeholder; set it (on the "
            "server: SSM parameter /capex/ALPHA_VANTAGE_API_KEY)"
        )

    rows = parse_calendar_csv(fetch_calendar_csv(api_key, horizon))
    our_tickers = set(get_all_tickers())
    sync_watchlist(db)
    now = utc_iso()
    synced = 0
    skipped = 0
    errors: list[str] = []

    with db.mutating() as conn:
        for row in rows:
            symbol = row.get("symbol", "").strip()
            if symbol not in our_tickers:
                continue
            report_date = row.get("reportDate", "").strip()
            fiscal_end = row.get("fiscalDateEnding", "").strip()
            if not report_date or not fiscal_end:
                skipped += 1
                continue
            try:
                form_type = expected_form(symbol, fiscal_end, db)
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
                    WHERE fiscal_calendar.status = 'upcoming'
                      AND fiscal_calendar.source != 'manual'
                    """,
                    (symbol, report_date, fiscal_end, form_type, now),
                )
                synced += 1
            except Exception as e:  # one bad row must not sink the sync
                errors.append(f"{symbol}: {e}")

    return {"synced": synced, "skipped": skipped, "errors": errors}


def requeue(
    *,
    statuses: tuple[str, ...] = ("failed", "stale"),
    since: str | None = None,
    tickers: set[str] | None = None,
    refresh_forms: bool = False,
    db: Database | None = None,
) -> list[dict[str, Any]]:
    """Put calendar rows back in the watcher's queue ('upcoming', 0 attempts).

    `refresh_forms` recomputes each row's expected form from the watchlist
    (e.g. after correcting a company's filing cadence). Returns the rows
    changed, with their new form.
    """
    db = db or Database()
    sql = (
        "SELECT id, ticker, report_date, fiscal_date_ending, form_type, status "
        f"FROM fiscal_calendar WHERE status IN ({','.join('?' * len(statuses))})"
    )
    params: list[Any] = list(statuses)
    if since:
        sql += " AND report_date >= ?"
        params.append(since)
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY report_date, ticker", params)]
    rows = [r for r in rows if tickers is None or r["ticker"] in tickers]
    if not rows:
        return []
    now = utc_iso()
    with db.mutating() as conn:
        for r in rows:
            if refresh_forms:
                r["form_type"] = expected_form(r["ticker"], r["fiscal_date_ending"], db)
            conn.execute(
                "UPDATE fiscal_calendar SET status = 'upcoming', attempts = 0, "
                "last_error = NULL, next_attempt_at = NULL, form_type = ?, "
                "updated_at = ? WHERE id = ?",
                (r["form_type"], now, r["id"]),
            )
    return rows


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


if __name__ == "__main__":
    import sys

    try:
        result = sync_earnings_calendar()
    except CalendarError as e:
        print(f"calendar sync failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2))
    sys.exit(0 if not result.get("errors") else 1)
