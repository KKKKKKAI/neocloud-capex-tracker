"""QoQ + YoY computation against an in-memory DB."""
from __future__ import annotations

from pathlib import Path

import pytest

from capex.db.schema import Database, migrate
from capex.notify.performance import (
    _delta_pct,
    _label,
    _prior_quarter,
    get_performance,
)


@pytest.fixture
def seeded_db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "test.db")
    migrate(db)
    with db.mutating() as conn:
        conn.execute(
            "INSERT INTO metric_definitions (key, label, aliases, unit_default, description) "
            "VALUES ('revenue','Revenue','[]','USD_millions','x')"
        )
        conn.execute(
            "INSERT INTO companies (ticker, name, preferred_source, edgar_cik, "
            "fiscal_year_end_month, reporting_currency, synced_at) "
            "VALUES ('MSFT','Microsoft','sec_edgar','0000789019',6,'USD','2026-04-29T00:00:00+00:00')"
        )
        # 4 quarters of revenue: FY2025 Q3, Q4, FY2026 Q1, Q2, Q3
        # MSFT FYE Jun → Q1 = Jul-Sep, Q2 = Oct-Dec, Q3 = Jan-Mar, Q4 = Apr-Jun
        for fy, period_type, period_end, value in [
            (2025, "Q3", "2025-03-31", 70066),
            (2025, "Q4", "2025-06-30", 76441),
            (2026, "Q1", "2025-09-30", 75000),
            (2026, "Q2", "2025-12-31", 81273),
            (2026, "Q3", "2026-03-31", 82886),
        ]:
            cur = conn.execute(
                "INSERT INTO source_documents (ticker, form_type, filing_date, "
                "period_of_report, fiscal_year, period_token, sha256, raw_path, "
                "source, source_url, accession_number, fetched_at, fetcher_version, "
                "protocol_version) VALUES ('MSFT','10-Q', ?, ?, ?, ?, ?, ?, "
                "'sec_edgar','http://x','acc', '2026-04-29T00:00:00+00:00', "
                "'test-1.0', '0.1.0')",
                (period_end, period_end, fy, period_type, f"sha-{fy}-{period_type}",
                 f"path/{fy}-{period_type}"),
            )
            sd_id = cur.lastrowid
            conn.execute(
                "INSERT INTO extractions (source_document_id, metric_key, value, "
                "value_text, unit, quote, locator_section, extraction_type, "
                "extracting_model, protocol_version, extracted_at, period_type, "
                "basis_period_months) VALUES (?, 'revenue', ?, '$x', "
                "'USD_millions', 'q', 'l', 'direct', 'xbrl-verified', "
                "'0.1.0-draft', '2026-04-29T00:00:00+00:00', ?, 3)",
                (sd_id, float(value), period_type),
            )
    return db


def test_get_performance_returns_current_qoq_yoy(seeded_db: Database):
    p = get_performance("MSFT", "revenue", "2026-03-31", db=seeded_db)
    assert p is not None
    assert p.metric_key == "revenue"
    assert p.current.value == 82886
    assert p.current.period_type == "Q3"
    assert p.current.fiscal_year == 2026
    assert p.current.period_label == "Q3 FY2026"
    # QoQ: Q3 FY2026 → Q2 FY2026 = $81,273M
    assert p.prior_qtr is not None
    assert p.prior_qtr.value == 81273
    assert p.prior_qtr.period_label == "Q2 FY2026"
    assert abs(p.qoq_pct - ((82886 - 81273) / 81273 * 100)) < 0.01
    # YoY: Q3 FY2026 → Q3 FY2025 = $70,066M
    assert p.prior_year is not None
    assert p.prior_year.value == 70066
    assert p.prior_year.period_label == "Q3 FY2025"
    assert abs(p.yoy_pct - ((82886 - 70066) / 70066 * 100)) < 0.01


def test_get_performance_q1_qoq_walks_back_a_year(seeded_db: Database):
    """Q1 FY2026 (period_of_report=2025-09-30) → QoQ = Q4 FY2025 = $76,441M"""
    p = get_performance("MSFT", "revenue", "2025-09-30", db=seeded_db)
    assert p is not None
    assert p.current.period_label == "Q1 FY2026"
    assert p.prior_qtr is not None
    assert p.prior_qtr.value == 76441
    assert p.prior_qtr.period_label == "Q4 FY2025"


def test_get_performance_returns_none_when_no_match(seeded_db: Database):
    p = get_performance("MSFT", "revenue", "1999-01-01", db=seeded_db)
    assert p is None


def test_delta_pct_handles_edge_cases():
    assert _delta_pct(110, 100) == 10.0
    assert _delta_pct(90, 100) == -10.0
    assert _delta_pct(None, 100) is None
    assert _delta_pct(100, None) is None
    assert _delta_pct(100, 0) is None
    # Negative prior: divide by absolute value so sign of the delta is intuitive
    assert _delta_pct(50, -100) == 150.0


def test_prior_quarter_steps_calendar_quarters():
    assert _prior_quarter("Q4", 2026) == ("Q3", 2026)
    assert _prior_quarter("Q1", 2026) == ("Q4", 2025)
    assert _prior_quarter("FY", 2026) is None  # FY has no prior quarter


def test_label_format():
    assert _label("Q3", 2026) == "Q3 FY2026"
    assert _label("FY", 2025) == "FY2025"
