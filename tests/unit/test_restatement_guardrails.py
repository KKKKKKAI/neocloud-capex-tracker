"""Guardrails for LLM restated-comparative rows.

Covers two specific failure modes that ate MSFT 2023Q4 before the fix:

1. **Fiscal-year mis-derivation**: `int(period_of_report[:4])` gives the
   calendar year, which only happens to equal the fiscal year for
   Dec-FYE filers. MSFT (FYE=Jun) Q2 ending 2024-12-31 is FY2025, not
   FY2024. A virtual source_doc tagged with the wrong fiscal_year
   collides with an unrelated original row in the
   `(ticker, fiscal_year, period_type)` selector bucket.

2. **Zero-valued restated row wins over real original**: if the LLM
   mis-reads an empty comparative cell as 0, the `filing_date DESC`
   tiebreak would promote it over the authentic original. Both the
   writer (skip at extract time) and the selectors (demote 0 below any
   real value) enforce the fallback.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


# ---- Fiscal-year derivation helper --------------------------------

def test_fiscal_year_from_msft_fye6():
    from capex.extract.extractors.llm_headless import _fiscal_year_from
    # MSFT: FYE=June. Period ending in June sits inside the same fiscal
    # year; periods ending later in the calendar roll into FY+1.
    assert _fiscal_year_from("2024-06-30", 6) == 2024
    assert _fiscal_year_from("2024-09-30", 6) == 2025
    assert _fiscal_year_from("2024-12-31", 6) == 2025
    assert _fiscal_year_from("2025-03-31", 6) == 2025
    assert _fiscal_year_from("2025-06-30", 6) == 2025


def test_fiscal_year_from_amzn_fye12():
    # Calendar-year companies: fiscal_year == period's calendar year.
    from capex.extract.extractors.llm_headless import _fiscal_year_from
    assert _fiscal_year_from("2024-03-31", 12) == 2024
    assert _fiscal_year_from("2024-12-31", 12) == 2024


def test_fiscal_year_from_baba_fye3():
    # BABA FYE=March. Periods ending Mar sit in FY{cy}; later in FY{cy+1}.
    from capex.extract.extractors.llm_headless import _fiscal_year_from
    assert _fiscal_year_from("2024-03-31", 3) == 2024
    assert _fiscal_year_from("2024-06-30", 3) == 2025
    assert _fiscal_year_from("2024-12-31", 3) == 2025


def test_fiscal_year_from_invalid_input_returns_none():
    from capex.extract.extractors.llm_headless import _fiscal_year_from
    assert _fiscal_year_from("", 12) is None
    assert _fiscal_year_from("not-a-date", 12) is None
    assert _fiscal_year_from("2024-13-01", 12) is None
    assert _fiscal_year_from("2024-01-01", 13) is None


# ---- Selector: zero-valued row loses to a real original ----------

def _build_db_with_zero_and_real(tmp_path: Path) -> Path:
    """Minimal schema to exercise the chart selector's ORDER BY."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE companies (
            ticker TEXT PRIMARY KEY, name TEXT DEFAULT '',
            fiscal_year_end_month INTEGER NOT NULL,
            reporting_currency TEXT DEFAULT 'USD'
        );
        CREATE TABLE source_documents (
            id INTEGER PRIMARY KEY, ticker TEXT,
            form_type TEXT, filing_date TEXT, period_of_report TEXT,
            fiscal_year INTEGER, period_token TEXT, raw_path TEXT
        );
        CREATE TABLE extractions (
            id INTEGER PRIMARY KEY,
            source_document_id INTEGER,
            metric_key TEXT, value REAL, value_usd REAL,
            period_type TEXT, extraction_type TEXT DEFAULT 'direct',
            extracted_at TEXT
        );
        INSERT INTO companies (ticker, fiscal_year_end_month)
            VALUES ('TEST', 6);
        -- original filing (filed 2024-01-30)
        INSERT INTO source_documents VALUES
            (1, 'TEST', '10-Q', '2024-01-30', '2023-12-31', 2024, 'Q2',
             'data/_sources/TEST/original.htm');
        -- a later restating 10-Q filed 2026-01-28 whose LLM mis-read
        -- the prior-year comparative column as 0
        INSERT INTO source_documents VALUES
            (2, 'TEST', '6-K', '2026-01-28', '2023-12-31', 2024, 'Q2',
             'restated-virtual://TEST/2023-12-31/abc');
        INSERT INTO extractions VALUES
            (10, 1, 'cloud_segment_revenue', 25880, 25880, 'Q2',
             'direct', '2024-01-30T00:00:00Z');
        INSERT INTO extractions VALUES
            (11, 2, 'cloud_segment_revenue', 0, 0, 'Q2',
             'direct', '2026-01-28T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()
    return db


def test_quarterly_selector_prefers_nonzero_original_over_zero_restated(tmp_path):
    """With filing_date DESC alone, the zero would win. The new CASE
    clause demotes 0/NULL values so the original stays authoritative."""
    from capex.exporters.interactive_chart import _load_quarterly
    db = _build_db_with_zero_and_real(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    q = _load_quarterly(conn, "cloud_segment_revenue", set())
    conn.close()
    # The 25880 (original) must survive; if the zero had won,
    # `by_quarter[TEST]` would be empty because abs(0) <= 0 gets dropped.
    assert q["by_quarter"].get("TEST", {}).get("2023Q4") == 25880


def test_annual_selector_prefers_nonzero_original_over_zero_restated(tmp_path):
    """Same guardrail, FY view."""
    import sqlite3
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE companies (
            ticker TEXT PRIMARY KEY, name TEXT DEFAULT '',
            fiscal_year_end_month INTEGER NOT NULL,
            reporting_currency TEXT DEFAULT 'USD'
        );
        CREATE TABLE source_documents (
            id INTEGER PRIMARY KEY, ticker TEXT,
            form_type TEXT, filing_date TEXT, period_of_report TEXT,
            fiscal_year INTEGER, period_token TEXT, raw_path TEXT
        );
        CREATE TABLE extractions (
            id INTEGER PRIMARY KEY, source_document_id INTEGER,
            metric_key TEXT, value REAL, value_usd REAL,
            period_type TEXT, extraction_type TEXT DEFAULT 'direct',
            extracted_at TEXT
        );
        INSERT INTO companies (ticker, fiscal_year_end_month) VALUES ('TEST', 12);
        INSERT INTO source_documents VALUES
            (1, 'TEST', '10-K', '2024-02-01', '2023-12-31', 2023, 'AR', 'data/orig.htm'),
            (2, 'TEST', '10-K', '2026-02-01', '2023-12-31', 2023, 'AR', 'restated-virtual://TEST/x');
        INSERT INTO extractions VALUES
            (10, 1, 'revenue', 100, 100, 'FY', 'direct', '2024-02-01T00:00:00Z'),
            (11, 2, 'revenue', 0, 0, 'FY', 'direct', '2026-02-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    from capex.exporters.interactive_chart import _load_annual
    a = _load_annual(conn, "revenue", set())
    conn.close()
    assert a["by_year"].get(2023, {}).get("TEST") == 100


# ---- Reconcile: a zero row doesn't overwrite a real one -----------

def test_reconcile_group_rows_ignores_zero_when_real_value_exists():
    from capex.extract.reconcile import _group_rows
    rows = [
        {
            "ticker": "MSFT", "fiscal_year": 2024, "metric_key": "revenue",
            "period_of_report": "2023-12-31", "period_type": "Q2",
            "period_token": "Q2", "form_type": "10-Q",
            "filing_date": "2024-01-30", "id": 1,
            "source_document_id": 1, "value": 25880, "value_usd": 25880,
            "basis_period_months": 3,
        },
        # Later filing re-states the same period as 0 — must NOT overwrite.
        {
            "ticker": "MSFT", "fiscal_year": 2024, "metric_key": "revenue",
            "period_of_report": "2023-12-31", "period_type": "Q2",
            "period_token": "Q2", "form_type": "6-K",
            "filing_date": "2026-01-28", "id": 2,
            "source_document_id": 2, "value": 0, "value_usd": 0,
            "basis_period_months": 3,
        },
    ]
    grouped = _group_rows(rows)
    key = ("MSFT", 2024, "revenue")
    assert grouped[key]["Q2"]["value"] == 25880
