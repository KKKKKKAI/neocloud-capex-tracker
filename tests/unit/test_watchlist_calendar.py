"""Watchlist defaults, expected forms, calendar sync and requeue."""
from __future__ import annotations

import shutil
import sqlite3

import pytest

from capex import paths, settings
from capex.db.schema import Database, migrate
from capex.monitor import calendar
from capex.monitor.watchlist import expected_form, get_entry, sync_watchlist, watched_tickers


def _calendar(db, ticker, fde):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM fiscal_calendar WHERE ticker = ? AND fiscal_date_ending = ?",
            (ticker, fde),
        ).fetchone()
    return dict(row) if row else None


# ---- watchlist ------------------------------------------------------------------

def test_watchlist_defaults(capex_db):
    msft = get_entry("MSFT", capex_db)
    assert (msft["watch"], msft["quarterly_form"], msft["annual_form"]) == (1, "10-Q", "10-K")
    assert get_entry("BIDU", capex_db)["quarterly_form"] == "6-K"   # 20-F filer
    assert get_entry("BIDU", capex_db)["annual_form"] == "20-F"
    assert get_entry("IREN", capex_db)["quarterly_form"] == "10-Q"  # corrected seed
    assert get_entry("0700", capex_db)["watch"] == 0                # HKEX: manual for now
    assert "0700" not in watched_tickers(capex_db)


def test_sync_never_overwrites_runtime_edits(capex_db):
    with capex_db.mutating() as conn:
        conn.execute("UPDATE watchlist SET watch = 0 WHERE ticker = 'MSFT'")
    assert sync_watchlist(capex_db) == 0
    assert get_entry("MSFT", capex_db)["watch"] == 0


@pytest.mark.parametrize("ticker, fde, form", [
    ("MSFT", "2026-06-30", "10-K"),   # FYE June
    ("MSFT", "2026-09-30", "10-Q"),
    ("ORCL", "2026-08-31", "10-Q"),   # FYE May
    ("IREN", "2026-06-30", "10-K"),
    ("IREN", "2026-03-31", "10-Q"),
    ("BIDU", "2026-12-31", "6-K"),    # 6-K filers: every quarter incl. FY end
    ("BABA", "2026-06-30", "6-K"),
])
def test_expected_form(capex_db, ticker, fde, form):
    assert expected_form(ticker, fde, capex_db) == form


# ---- calendar sync ------------------------------------------------------------------

CSV = (
    "symbol,name,reportDate,fiscalDateEnding,estimate,currency\n"
    "MSFT,Microsoft,2026-10-28,2026-09-30,3.1,USD\n"
    "BIDU,Baidu,2026-11-18,2026-09-30,,USD\n"
    "ZZZZ,Not covered,2026-10-01,2026-09-30,,USD\n"
    "IREN,IREN,2026-11-06,2026-09-30,,USD\n"
)


@pytest.fixture
def av(monkeypatch):
    replies = {"text": CSV}
    monkeypatch.setattr(calendar, "fetch_calendar_csv", lambda key, horizon="3month": replies["text"])
    return replies


def test_sync_inserts_covered_tickers_with_expected_forms(capex_db, av):
    result = calendar.sync_earnings_calendar(api_key="real-key", db=capex_db)
    assert result == {"synced": 3, "skipped": 0, "errors": []}
    assert _calendar(capex_db, "MSFT", "2026-09-30")["form_type"] == "10-Q"
    assert _calendar(capex_db, "BIDU", "2026-09-30")["form_type"] == "6-K"
    assert _calendar(capex_db, "IREN", "2026-09-30")["form_type"] == "10-Q"
    assert _calendar(capex_db, "ZZZZ", "2026-09-30") is None


def test_sync_leaves_manual_and_in_flight_rows_alone(capex_db, av):
    calendar.add_manual_entry("MSFT", "2026-10-29", "2026-09-30", "10-Q", db=capex_db)
    with capex_db.mutating() as conn:
        conn.execute(
            "INSERT INTO fiscal_calendar (ticker, report_date, fiscal_date_ending, form_type, "
            "status, source, updated_at) VALUES ('IREN', '2026-11-01', '2026-09-30', '10-Q', "
            "'extracted', 'alpha_vantage', 'x')"
        )
    calendar.sync_earnings_calendar(api_key="real-key", db=capex_db)
    assert _calendar(capex_db, "MSFT", "2026-09-30")["report_date"] == "2026-10-29"
    iren = _calendar(capex_db, "IREN", "2026-09-30")
    assert (iren["report_date"], iren["status"]) == ("2026-11-01", "extracted")


@pytest.mark.parametrize("key", ["", "demo", "your_key_here"])
def test_placeholder_keys_are_refused(capex_db, av, key):
    with pytest.raises(calendar.CalendarError, match="missing or a placeholder"):
        calendar.sync_earnings_calendar(api_key=key, db=capex_db)


def test_demo_key_allowed_when_configured(capex_db, av):
    settings.set("calendar.allow_demo_key", True, db=capex_db)
    assert calendar.sync_earnings_calendar(api_key="demo", db=capex_db)["synced"] == 3


@pytest.mark.parametrize("body", [
    '{"Information": "The demo API key is for demo purposes only."}',
    '{"Error Message": "Invalid API call."}',
    "<html>maintenance</html>",
])
def test_provider_error_bodies_raise(capex_db, av, body):
    av["text"] = body
    with pytest.raises(calendar.CalendarError):
        calendar.sync_earnings_calendar(api_key="real-key", db=capex_db)


# ---- requeue --------------------------------------------------------------------------

def test_requeue_resets_and_refreshes_forms(capex_db):
    with capex_db.mutating() as conn:
        conn.executemany(
            "INSERT INTO fiscal_calendar (ticker, report_date, fiscal_date_ending, form_type, "
            "status, source, updated_at, attempts, last_error) "
            "VALUES (?, ?, ?, ?, ?, 'alpha_vantage', 'x', 3, 'boom')",
            [("IREN", "2026-08-27", "2026-06-30", "20-F", "failed"),
             ("ORCL", "2026-09-09", "2026-08-31", "10-Q", "stale"),
             ("MSFT", "2026-07-29", "2026-06-30", "10-K", "extracted")],
        )
    rows = calendar.requeue(since="2026-05-01", refresh_forms=True, db=capex_db)
    assert {(r["ticker"], r["form_type"]) for r in rows} == {("IREN", "10-K"), ("ORCL", "10-Q")}
    iren = _calendar(capex_db, "IREN", "2026-06-30")
    assert (iren["status"], iren["attempts"], iren["last_error"], iren["form_type"]) == (
        "upcoming", 0, None, "10-K")
    assert _calendar(capex_db, "MSFT", "2026-06-30")["status"] == "extracted"


# ---- migration 0012 on the real DB ------------------------------------------------------

def test_migration_0012_keeps_every_calendar_row(tmp_path):
    real = paths.CODE_ROOT / "data" / "db" / "capex.db"
    if not real.exists():
        pytest.skip("no local production DB copy")
    copy = tmp_path / "capex.db"
    shutil.copy2(real, copy)
    before = _calendar_rows(copy)
    migrate(Database(path=copy, dump_path=tmp_path / "dump.sql"))
    after = _calendar_rows(copy)
    assert after == before
    conn = sqlite3.connect(copy)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fiscal_calendar)")}
    assert {"attempts", "last_error", "next_attempt_at", "filing_event_id"} <= cols
    assert conn.execute("SELECT COUNT(*) FROM filing_events").fetchone()[0] == 0
    conn.close()


def _calendar_rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT id, ticker, report_date, fiscal_date_ending, form_type, status, source "
            "FROM fiscal_calendar ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
