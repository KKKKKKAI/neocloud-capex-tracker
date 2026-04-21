"""Tests for the multi-period LLM extraction flow.

Covers:
- `parse_agent_a_response` returns the `periods` schema for both
  the new multi-period JSON and the legacy single-value JSON.
- `verify_period` pairs one period's expected value with Agent B's
  blind verdict.
- `ensure_restated_source_doc` creates a virtual source_documents
  row with the restating filing's citation fields but the restated
  period's `fiscal_year`.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def test_parse_multi_period_response():
    from capex.verification.dual_agent import parse_agent_a_response
    raw = json.dumps({
        "found": True,
        "periods": [
            {"role": "primary", "label": "FY2025",
             "period_of_report": "2025-06-30", "basis_period_months": 12,
             "value": 106265, "unit": "USD_millions",
             "excerpts": [{"text": "…", "location": "Segment", "role": "primary_value"}],
             "reasoning": "current yr"},
            {"role": "comparative", "label": "FY2024 (restated)",
             "period_of_report": "2024-06-30", "basis_period_months": 12,
             "value": 87464, "unit": "USD_millions",
             "excerpts": [{"text": "…", "location": "Segment", "role": "primary_value"}],
             "reasoning": "prior yr"},
        ],
        "reasoning": "read from segment table",
    })
    parsed = parse_agent_a_response(raw)
    assert parsed["found"] is True
    assert len(parsed["periods"]) == 2
    assert parsed["periods"][0]["role"] == "primary"
    assert parsed["periods"][1]["value"] == 87464


def test_parse_legacy_single_value_response_normalized_to_multi_period():
    """If a caller still produces the old single-value schema, the
    parser should normalize it to the multi-period form with one
    primary entry."""
    from capex.verification.dual_agent import parse_agent_a_response
    raw = json.dumps({
        "found": True,
        "value": 42,
        "unit": "USD_millions",
        "excerpts": [],
        "reasoning": "x",
    })
    parsed = parse_agent_a_response(raw)
    assert parsed["found"] is True
    assert len(parsed["periods"]) == 1
    assert parsed["periods"][0]["role"] == "primary"
    assert parsed["periods"][0]["value"] == 42


def test_parse_not_found_response():
    from capex.verification.dual_agent import parse_agent_a_response
    raw = json.dumps({"found": False, "periods": [],
                      "reasoning": "no such thing"})
    parsed = parse_agent_a_response(raw)
    assert parsed["found"] is False
    assert parsed["periods"] == []


def test_verify_period_matches():
    from capex.verification.dual_agent import verify_period
    period_a = {
        "value": 106265, "unit": "USD_millions",
        "excerpts": [{"text": "…", "role": "primary_value"}],
        "reasoning": "x",
    }
    result_b = {"determinable": True, "value": 106265, "reasoning": "y"}
    v = verify_period(period_a, result_b)
    assert v.verified is True
    assert v.value_a == 106265
    assert v.value_b == 106265


def test_verify_period_mismatch():
    from capex.verification.dual_agent import verify_period
    period_a = {"value": 100, "excerpts": [], "reasoning": ""}
    result_b = {"determinable": True, "value": 200, "reasoning": ""}
    v = verify_period(period_a, result_b)
    assert v.verified is False
    assert v.needs_review is True


def test_verify_period_b_insufficient():
    from capex.verification.dual_agent import verify_period
    period_a = {"value": 100, "excerpts": [], "reasoning": ""}
    result_b = {"determinable": False, "reasoning": "ambiguous"}
    v = verify_period(period_a, result_b)
    assert v.verified is False
    assert v.value_b is None


# ---- Batched Agent B -------------------------------------------

def test_verify_periods_batch_pairs_verdicts_by_index():
    from capex.verification.dual_agent import verify_periods_batch
    periods_a = [
        {"value": 106265, "excerpts": [], "reasoning": "FY25"},
        {"value": 87464, "excerpts": [], "reasoning": "FY24 restated"},
        {"value": 87907, "excerpts": [], "reasoning": "FY23 restated"},
    ]
    agent_b = {
        "verdicts": [
            {"determinable": True, "value": 106265},
            {"determinable": True, "value": 87464},
            {"determinable": True, "value": 87907},
        ]
    }
    results = verify_periods_batch(periods_a, agent_b)
    assert len(results) == 3
    assert all(r.verified for r in results)
    assert [r.value_b for r in results] == [106265, 87464, 87907]


def test_verify_periods_batch_fewer_verdicts_than_periods():
    """If Agent B returns a short list, the unmatched periods get
    a `not_found`-like verdict instead of crashing."""
    from capex.verification.dual_agent import verify_periods_batch
    periods_a = [
        {"value": 100, "excerpts": [], "reasoning": ""},
        {"value": 200, "excerpts": [], "reasoning": ""},
    ]
    agent_b = {"verdicts": [{"determinable": True, "value": 100}]}
    results = verify_periods_batch(periods_a, agent_b)
    assert len(results) == 2
    assert results[0].verified is True
    assert results[1].verified is False
    assert results[1].needs_review is True


def test_parse_agent_b_response_modern_shape():
    from capex.verification.dual_agent import parse_agent_b_response
    raw = json.dumps({
        "verdicts": [
            {"period": "FY25", "determinable": True, "value": 1},
            {"period": "FY24", "determinable": False, "value": None,
             "concerns": "ambiguous"},
        ]
    })
    parsed = parse_agent_b_response(raw)
    assert len(parsed["verdicts"]) == 2
    assert parsed["verdicts"][0]["value"] == 1


def test_parse_agent_b_response_legacy_shape_normalized():
    """Legacy Agent B output (single value, no verdicts list) gets
    wrapped in a one-element verdicts array so callers can uniformly
    index."""
    from capex.verification.dual_agent import parse_agent_b_response
    raw = json.dumps({
        "determinable": True, "value": 106265, "reasoning": "yes",
    })
    parsed = parse_agent_b_response(raw)
    assert len(parsed["verdicts"]) == 1
    assert parsed["verdicts"][0]["value"] == 106265


def test_build_agent_b_prompt_multi_lists_periods():
    from capex.verification.dual_agent import build_agent_b_prompt
    periods = [
        {"role": "primary", "label": "FY2025",
         "period_of_report": "2025-06-30", "basis_period_months": 12,
         "excerpts": [{"text": "hdr...FY25...106265", "role": "primary_value",
                       "location": "Segment"}]},
        {"role": "comparative", "label": "FY2024 (restated)",
         "period_of_report": "2024-06-30", "basis_period_months": 12,
         "excerpts": [{"text": "hdr...FY24...87464", "role": "primary_value",
                       "location": "Segment"}]},
    ]
    prompt = build_agent_b_prompt(
        company_name="MSFT", form_type="10-K",
        periods=periods, metric_description="Intelligent Cloud revenue",
    )
    # Both period labels appear; each period's excerpt is included
    assert "FY2025" in prompt
    assert "FY2024 (restated)" in prompt
    assert "106265" in prompt
    assert "87464" in prompt
    # The output format note tells Agent B to return a verdicts array
    assert '"verdicts"' in prompt


# ---- virtual source_doc -----------------------------------------

def _make_min_db(tmp_path: Path) -> Path:
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
            ticker TEXT NOT NULL REFERENCES companies(ticker),
            form_type TEXT NOT NULL
                CHECK(form_type IN ('10-K','10-Q','20-F','6-K','HK-AR','HK-IR')),
            filing_date TEXT NOT NULL,
            period_of_report TEXT NOT NULL, fiscal_year INTEGER NOT NULL,
            period_token TEXT NOT NULL
                CHECK(period_token IN ('AR','Q1','Q2','Q3','Q4','H1','H2')),
            sha256 TEXT NOT NULL UNIQUE, raw_path TEXT NOT NULL,
            canonical_path TEXT,
            source TEXT NOT NULL
                CHECK(source IN ('sec_edgar','hkex','xbrl_api')),
            source_url TEXT NOT NULL, accession_number TEXT,
            fetched_at TEXT NOT NULL, fetcher_version TEXT NOT NULL,
            protocol_version TEXT NOT NULL,
            UNIQUE(ticker, form_type, period_of_report)
        );
        """
    )
    conn.execute(
        "INSERT INTO companies (ticker, fiscal_year_end_month) VALUES (?, ?)",
        ("MSFT", 6),
    )
    # The restating 10-K
    conn.execute(
        "INSERT INTO source_documents "
        "(ticker, form_type, filing_date, period_of_report, fiscal_year,"
        " period_token, sha256, raw_path, source, source_url,"
        " accession_number, fetched_at, fetcher_version, protocol_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("MSFT", "10-K", "2025-07-30", "2025-06-30", 2025, "AR",
         "restating-sha", "data/_sources/MSFT/_raw/msft-20250630.htm",
         "sec_edgar", "https://sec.gov/msft-20250630.htm",
         "0001-restating", "2025-07-30T00:00:00Z", "test", "0.1.0-draft"),
    )
    conn.commit()
    conn.close()
    return db


def test_ensure_restated_source_doc_creates_row(tmp_path):
    db = _make_min_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    from capex.extract.virtual_source_docs import ensure_restated_source_doc
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_id = ensure_restated_source_doc(conn, "MSFT", 2024, 1, now)
    conn.commit()
    row = conn.execute(
        "SELECT * FROM source_documents WHERE id = ?", (new_id,),
    ).fetchone()
    conn.close()
    assert row["fiscal_year"] == 2024
    assert row["period_of_report"] == "2024-06-30"
    assert row["form_type"] == "6-K"
    assert row["raw_path"].startswith("restated-virtual://")
    # Citation fields copied from the restating filing
    assert row["source_url"] == "https://sec.gov/msft-20250630.htm"
    assert row["accession_number"] == "0001-restating"
    assert row["filing_date"] == "2025-07-30"


def test_ensure_restated_source_doc_idempotent(tmp_path):
    db = _make_min_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    from capex.extract.virtual_source_docs import ensure_restated_source_doc
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    a = ensure_restated_source_doc(conn, "MSFT", 2024, 1, now)
    b = ensure_restated_source_doc(conn, "MSFT", 2024, 1, now)
    conn.commit()
    conn.close()
    assert a == b
