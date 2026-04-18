"""Unit tests for the quarterly reporting convention validator.

Builds a tiny in-memory SQLite with a few companies + convention rows,
then checks representative filings against their declared conventions.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from capex.extract.convention_validator import validate_convention


def _setup_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE companies (ticker TEXT PRIMARY KEY);
        CREATE TABLE company_quarterly_convention (
            ticker TEXT PRIMARY KEY REFERENCES companies(ticker),
            default_convention TEXT NOT NULL,
            per_metric_json TEXT,
            header_signatures_json TEXT,
            synced_at TEXT NOT NULL
        );
        INSERT INTO companies VALUES ('MSFT'), ('BABA'), ('0700');
        """
    )
    rows = [
        (
            "MSFT", "three_month_column", "{}",
            '{"expect_any_of": ["Three Months Ended"]}',
            "2026-04-18T00:00:00",
        ),
        (
            "BABA", "standalone_quarterly", "{}",
            '{"expect_any_of": ["quarter ended", "Three months ended"], '
            '"must_not_match": ["Nine months ended"]}',
            "2026-04-18T00:00:00",
        ),
        (
            "0700", "semi_annual", "{}",
            '{"expect_any_of": ["Six months ended", "interim"]}',
            "2026-04-18T00:00:00",
        ),
    ]
    c.executemany(
        "INSERT INTO company_quarterly_convention VALUES (?,?,?,?,?)",
        rows,
    )
    return c


def test_three_month_column_happy_path():
    c = _setup_conn()
    text = "Three Months Ended September 30, 2025. Nine Months Ended September 30, 2025."
    r = validate_convention(ticker="MSFT", filing_text=text, conn=c)
    assert r.ok, r.warnings


def test_standalone_quarterly_flags_ytd_phrasing():
    c = _setup_conn()
    text = "Nine months ended December 31, 2025 revenue was 100 billion."
    r = validate_convention(ticker="BABA", filing_text=text, conn=c)
    assert not r.ok
    assert any("standalone_quarterly" in w for w in r.warnings)


def test_standalone_quarterly_happy_path():
    c = _setup_conn()
    text = "In the quarter ended June 30, 2016, revenue was RMB32,154 million."
    r = validate_convention(ticker="BABA", filing_text=text, conn=c)
    assert r.ok, r.warnings


def test_semi_annual_happy_path():
    c = _setup_conn()
    text = "Interim consolidated results for the six months ended June 30, 2024."
    r = validate_convention(ticker="0700", filing_text=text, conn=c)
    assert r.ok, r.warnings


def test_unknown_ticker():
    c = _setup_conn()
    r = validate_convention(ticker="NEWCO", filing_text="whatever", conn=c)
    assert not r.ok
    assert any("no quarterly_convention declared" in w for w in r.warnings)


if __name__ == "__main__":
    tests = [
        test_three_month_column_happy_path,
        test_standalone_quarterly_flags_ytd_phrasing,
        test_standalone_quarterly_happy_path,
        test_semi_annual_happy_path,
        test_unknown_ticker,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK   {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
