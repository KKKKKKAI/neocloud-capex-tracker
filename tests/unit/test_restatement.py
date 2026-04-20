"""Tests for selector precedence + minimal restatement-report plumbing.

Restatements themselves are now captured by the LLM dual-agent
extractor (see `tests/unit/test_multi_period_extraction.py`). This
file keeps the cross-cutting tests: selector precedence (newest
`source_documents.filing_date` wins) and the shape of the audit
report's "Restatements" section.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def test_xbrl_fetch_returns_single_entry_per_period(monkeypatch):
    """Post-rework, fetch_concept_timeseries returns one entry per
    (end_date, form) group even when the API surfaces multiple
    contexts — the later-filed variants are discarded here. LLM
    dual-agent captures restatements instead."""
    from capex.xbrl import timeseries as ts

    fake_facts = {
        "facts": {
            "us-gaap": {
                "Revenue": {
                    "units": {
                        "USD": [
                            {"end": "2024-06-30", "val": 105_362_000_000,
                             "form": "10-K", "filed": "2024-07-30",
                             "fy": 2024, "fp": "FY", "accn": "A",
                             "start": "2023-07-01"},
                            {"end": "2024-06-30", "val": 87_464_000_000,
                             "form": "10-K", "filed": "2025-07-30",
                             "fy": 2024, "fp": "FY", "accn": "B",
                             "start": "2023-07-01"},
                        ]
                    }
                }
            }
        }
    }
    monkeypatch.setattr(ts, "_fetch_companyfacts", lambda cik: fake_facts)
    series = ts.fetch_concept_timeseries(
        cik="0000000000", concept="us-gaap:Revenue",
    )
    ends = [e["end"] for e in series]
    assert ends.count("2024-06-30") == 1
    # No is_restatement field surfaces any more
    assert all("is_restatement" not in e for e in series)


# ---- Selector: newest filing wins --------------------------------

def _seed_extractions_db(tmp_path: Path) -> Path:
    """Minimal schema + two source_documents for the same cell."""
    db = tmp_path / "capex.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE companies (
            ticker TEXT PRIMARY KEY, name TEXT DEFAULT '',
            preferred_source TEXT DEFAULT 'sec', edgar_cik TEXT,
            hkex_stock_code TEXT, fiscal_year_end_month INTEGER NOT NULL,
            synced_at TEXT DEFAULT '', reporting_currency TEXT DEFAULT 'USD'
        );
        CREATE TABLE source_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, form_type TEXT NOT NULL,
            filing_date TEXT NOT NULL, period_of_report TEXT NOT NULL,
            fiscal_year INTEGER NOT NULL, period_token TEXT DEFAULT 'AR',
            sha256 TEXT DEFAULT '', raw_path TEXT DEFAULT '',
            canonical_path TEXT, source TEXT DEFAULT 'sec',
            source_url TEXT DEFAULT '', accession_number TEXT,
            fetched_at TEXT DEFAULT '', fetcher_version TEXT DEFAULT '',
            protocol_version TEXT DEFAULT ''
        );
        CREATE TABLE extractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_document_id INTEGER NOT NULL,
            metric_key TEXT NOT NULL, value REAL, value_text TEXT,
            unit TEXT, quote TEXT, locator_page TEXT,
            locator_section TEXT, extraction_type TEXT DEFAULT 'direct',
            confidence REAL, extracting_model TEXT, protocol_version TEXT,
            extracted_at TEXT, value_usd REAL, fx_rate REAL,
            fx_rate_date TEXT, reporting_currency TEXT DEFAULT 'USD',
            period_type TEXT DEFAULT '', basis_period_months INTEGER,
            reporting_convention TEXT
        );
        CREATE TABLE extraction_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            extraction_id INTEGER NOT NULL,
            excerpt_role TEXT NOT NULL,
            excerpt_text TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO companies (ticker, fiscal_year_end_month) VALUES (?, ?)",
        ("MSFT", 6),
    )
    # Original FY2024 10-K (filed 2024-07-30): IC = $105,362M
    conn.execute(
        "INSERT INTO source_documents "
        "(id, ticker, form_type, filing_date, period_of_report, fiscal_year,"
        " source_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "MSFT", "10-K", "2024-07-30", "2024-06-30", 2024,
         "https://sec.gov/original"),
    )
    # Restating FY2025 10-K (filed 2025-07-30) — but carries FY2024 comparative
    # at a restated value of $87,464M.
    conn.execute(
        "INSERT INTO source_documents "
        "(id, ticker, form_type, filing_date, period_of_report, fiscal_year,"
        " source_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (2, "MSFT", "10-K", "2025-07-30", "2024-06-30", 2024,
         "https://sec.gov/restated"),
    )
    # Two extraction rows for the same (ticker, fy, metric, period_type):
    # original from filing 1, restated from filing 2.
    conn.execute(
        "INSERT INTO extractions "
        "(source_document_id, metric_key, value, value_usd, period_type,"
        " extracting_model, extraction_type, extracted_at) "
        "VALUES (1, 'cloud_segment_revenue', 105362, 105362, 'FY',"
        " 'claude-code', 'direct', '2024-08-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO extractions "
        "(source_document_id, metric_key, value, value_usd, period_type,"
        " extracting_model, extraction_type, extracted_at) "
        "VALUES (2, 'cloud_segment_revenue', 87464, 87464, 'FY',"
        " 'restated-segment@0.1.0', 'direct', '2025-08-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    return db


def test_load_annual_selector_prefers_newest_filing_date(tmp_path):
    db = _seed_extractions_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    from capex.exporters.interactive_chart import _load_annual
    result = _load_annual(conn, "cloud_segment_revenue")
    conn.close()
    # FY2024 MSFT value should be the RESTATED $87,464M, not the
    # original $105,362M — selector prefers newer filing_date.
    assert result["by_year"][2024]["MSFT"] == 87464


def test_load_cells_selector_prefers_newest_filing_date(tmp_path):
    db = _seed_extractions_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    from capex.audit.orchestrator import load_cells
    cells = load_cells(conn, [])
    conn.close()
    key = ("MSFT", 2024, "cloud_segment_revenue", "FY")
    assert key in cells
    assert cells[key]["value_usd"] == 87464
    assert cells[key]["extracting_model"] == "restated-segment@0.1.0"


# ---- Detector + applier -----------------------------------------

def test_render_markdown_empty_case():
    from capex.audit.restatement import RestatementSummary, render_markdown
    md = render_markdown(RestatementSummary())
    assert "No restatements detected" in md


def test_render_markdown_has_row_per_finding():
    from capex.audit.restatement import (
        RestatementFinding,
        RestatementSummary,
        render_markdown,
    )
    f = RestatementFinding(
        cell_key="MSFT:cloud_segment_revenue:2024FY",
        ticker="MSFT", metric_key="cloud_segment_revenue",
        fiscal_year=2024, period_type="FY",
        original_value_usd=105_362, latest_value_usd=87_464,
        delta_pct=0.17,
        original_filing_date="2024-07-30",
        latest_filing_date="2025-07-30",
        latest_source_url="https://sec.gov/x",
    )
    md = render_markdown(RestatementSummary(findings=[f]))
    assert "MSFT:cloud_segment_revenue:2024FY" in md
    assert "$87,464M" in md
    assert "17.0%" in md
    assert "2025-07-30" in md
