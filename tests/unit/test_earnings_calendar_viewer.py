"""Unit tests for the earnings calendar viewer (data + HTML + CLI table)."""
from __future__ import annotations

import io
import sqlite3
import sys
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from capex.cli.main import _print_calendar_table
from capex.exporters.earnings_calendar_html import (
    _countdown_label,
    generate_earnings_calendar_html,
)
from capex.monitor.calendar import (
    CalendarEvent,
    _derive_fy_and_period,
    query_for_viewer,
)


def _make_db(tmp_path) -> Path:
    """Build a minimal schema containing only the tables we query."""
    db_path = tmp_path / "capex.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE companies (
            ticker TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            preferred_source TEXT NOT NULL DEFAULT 'sec',
            edgar_cik TEXT,
            hkex_stock_code TEXT,
            fiscal_year_end_month INTEGER NOT NULL,
            synced_at TEXT NOT NULL DEFAULT '',
            reporting_currency TEXT NOT NULL DEFAULT 'USD'
        );
        CREATE TABLE fiscal_calendar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            report_date TEXT NOT NULL,
            fiscal_date_ending TEXT NOT NULL,
            form_type TEXT,
            status TEXT NOT NULL DEFAULT 'upcoming',
            source TEXT NOT NULL DEFAULT 'alpha_vantage',
            updated_at TEXT NOT NULL,
            UNIQUE(ticker, fiscal_date_ending)
        );
        CREATE TABLE source_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            form_type TEXT NOT NULL,
            filing_date TEXT NOT NULL,
            period_of_report TEXT NOT NULL,
            fiscal_year INTEGER NOT NULL DEFAULT 0,
            period_token TEXT NOT NULL DEFAULT '',
            sha256 TEXT NOT NULL DEFAULT '',
            raw_path TEXT NOT NULL DEFAULT '',
            canonical_path TEXT,
            source TEXT NOT NULL DEFAULT 'sec',
            source_url TEXT NOT NULL DEFAULT '',
            accession_number TEXT,
            fetched_at TEXT NOT NULL DEFAULT '',
            fetcher_version TEXT NOT NULL DEFAULT '',
            protocol_version TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _seed(db_path: Path, today: date) -> None:
    conn = sqlite3.connect(str(db_path))
    upcoming1 = (today + timedelta(days=10)).isoformat()
    upcoming2 = (today + timedelta(days=20)).isoformat()
    past1 = (today - timedelta(days=5)).isoformat()
    fy_dec = "2026-03-31"
    fy_msft = "2026-03-31"
    conn.executemany(
        "INSERT INTO companies (ticker, name, fiscal_year_end_month) VALUES (?, ?, ?)",
        [
            ("MSFT", "Microsoft", 6),
            ("META", "Meta Platforms", 12),
            ("BABA", "Alibaba", 3),
        ],
    )
    conn.executemany(
        "INSERT INTO fiscal_calendar "
        "(ticker, report_date, fiscal_date_ending, form_type, status, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("MSFT", upcoming1, fy_msft, "10-Q", "upcoming", "2026-04-15T19:00:00+00:00"),
            ("META", upcoming1, fy_dec, "10-Q", "upcoming", "2026-04-15T19:00:00+00:00"),
            ("BABA", upcoming2, "2026-03-31", "20-F", "upcoming", "2026-04-15T19:00:00+00:00"),
        ],
    )
    # Past filing: MSFT 10-Q for prior quarter
    conn.execute(
        "INSERT INTO source_documents "
        "(ticker, form_type, filing_date, period_of_report, source_url, fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("MSFT", "10-Q", past1, "2025-12-31",
         "https://www.sec.gov/Archives/msft-20251231.htm",
         past1 + "T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()


# ----------------- FY + period derivation -----------------

def test_derive_fy_and_period_dec_fye():
    assert _derive_fy_and_period("2026-03-31", 12) == (2026, "Q1")
    assert _derive_fy_and_period("2026-06-30", 12) == (2026, "Q2")
    assert _derive_fy_and_period("2026-12-31", 12) == (2026, "FY")


def test_derive_fy_and_period_msft_jun_fye():
    # MSFT FYE=June: Jul=Q1, Oct=Q2, Jan=Q3, Apr=Q4/FY
    assert _derive_fy_and_period("2025-09-30", 6) == (2026, "Q1")
    assert _derive_fy_and_period("2025-12-31", 6) == (2026, "Q2")
    assert _derive_fy_and_period("2026-03-31", 6) == (2026, "Q3")
    assert _derive_fy_and_period("2026-06-30", 6) == (2026, "FY")


def test_derive_fy_and_period_orcl_may_fye():
    assert _derive_fy_and_period("2026-05-31", 5) == (2026, "FY")
    assert _derive_fy_and_period("2026-02-28", 5) == (2026, "Q3")


# ----------------- query_for_viewer -----------------

def test_query_for_viewer_returns_sorted_events(tmp_path):
    db = _make_db(tmp_path)
    today = date.today()
    _seed(db, today)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    events = query_for_viewer(conn, upcoming_days=30, past_days=30)
    conn.close()

    # 3 upcoming + 1 past = 4 events
    assert len(events) == 4
    # Sorted ascending by report_date
    dates = [e.report_date for e in events]
    assert dates == sorted(dates)


def test_query_for_viewer_merges_source_url_for_past_events(tmp_path):
    db = _make_db(tmp_path)
    today = date.today()
    _seed(db, today)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    events = query_for_viewer(conn, upcoming_days=30, past_days=30)
    conn.close()

    past = [e for e in events if e.days_from_today < 0]
    assert len(past) == 1
    assert past[0].ticker == "MSFT"
    assert past[0].source_url.startswith("https://www.sec.gov/")
    assert past[0].status == "extracted"


def test_query_for_viewer_ticker_filter(tmp_path):
    db = _make_db(tmp_path)
    today = date.today()
    _seed(db, today)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    events = query_for_viewer(
        conn, upcoming_days=30, past_days=30, ticker_filter="META",
    )
    conn.close()
    assert len(events) == 1
    assert events[0].ticker == "META"


def test_query_for_viewer_excludes_past_when_zero(tmp_path):
    db = _make_db(tmp_path)
    today = date.today()
    _seed(db, today)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    # past_days=0 means today-0 ... today — window is empty
    events = query_for_viewer(conn, upcoming_days=30, past_days=0)
    conn.close()
    past = [e for e in events if e.days_from_today < 0]
    assert past == []


# ----------------- HTML output -----------------

def test_html_has_all_nav_pills_and_calendar_active(tmp_path):
    db = _make_db(tmp_path)
    _seed(db, date.today())
    out = tmp_path / "calendar.html"
    generate_earnings_calendar_html(output=out, db_path=db)
    txt = out.read_text(encoding="utf-8")
    for label in ("Home", "Cloud / DC Revenue", "Total Revenue", "CapEx",
                  "Operating Cash Flow", "Calendar"):
        assert label in txt, f"missing nav label: {label}"
    # Calendar pill is active
    assert 'class="nav-pill active" href="calendar.html"' in txt


def test_html_renders_event_rows_and_badges(tmp_path):
    db = _make_db(tmp_path)
    _seed(db, date.today())
    out = tmp_path / "calendar.html"
    generate_earnings_calendar_html(output=out, db_path=db)
    txt = out.read_text(encoding="utf-8")
    # Each event renders an <li class="ev-row">
    assert txt.count('class="ev-row"') == 4
    # Status badges for both upcoming and extracted appear
    assert "upcoming" in txt
    assert "extracted" in txt
    # Filing link shows up for the past event
    assert 'class="filing-link"' in txt


def test_html_empty_db_shows_friendly_message(tmp_path):
    db = _make_db(tmp_path)  # no seed — fiscal_calendar + source_documents empty
    out = tmp_path / "calendar.html"
    generate_earnings_calendar_html(output=out, db_path=db)
    txt = out.read_text(encoding="utf-8")
    assert "capex calendar sync" in txt


def test_countdown_labels():
    assert _countdown_label(0) == "today"
    assert _countdown_label(1) == "tomorrow"
    assert _countdown_label(-1) == "yesterday"
    assert _countdown_label(10) == "in 10 days"
    assert _countdown_label(-7) == "7 days ago"


# ----------------- CLI table -----------------

def _event(**overrides) -> CalendarEvent:
    defaults = dict(
        ticker="MSFT", company_name="Microsoft",
        report_date="2026-04-29", fiscal_date_ending="2026-03-31",
        form_type="10-Q", status="upcoming", source_url=None,
        days_from_today=10, fiscal_year=2026, period_label="Q3",
        updated_at="2026-04-15T19:00:00+00:00",
    )
    defaults.update(overrides)
    return CalendarEvent(**defaults)


def test_cli_table_contains_headers_and_row(tmp_path):
    evs = [_event(), _event(ticker="META", period_label="Q1", fiscal_year=2026)]
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_calendar_table(evs)
    out = buf.getvalue()
    for col in ("Ticker", "Report date", "Period end", "Form",
                "Period", "Status", "Offset"):
        assert col in out
    assert "MSFT" in out
    assert "META" in out
    assert "FY26 Q3" in out
    assert "in 10d" in out
    # Summary line at the end
    assert "upcoming" in out


def test_cli_table_past_summary_separator():
    past = _event(days_from_today=-5, status="extracted",
                  ticker="MSFT", report_date="2026-04-14",
                  fiscal_date_ending="2025-12-31", period_label="Q2")
    upcoming = _event()
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_calendar_table([past, upcoming])
    out = buf.getvalue()
    # Both upcoming and last-30d blocks should appear, separated by "|"
    assert "last 30d" in out
    assert "upcoming" in out
