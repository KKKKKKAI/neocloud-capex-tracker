"""Tests for the per-company treatments viewer (data + HTML + CLI)."""
from __future__ import annotations

import io
import re
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# The data layer reads coverage.yaml + human_notes.yaml via module-level
# caches. Tests that seed their own YAML fixtures should use
# `_reload_for_test` helpers rather than mutating the caches directly.

from capex.audit import human_notes as hn_mod
from capex.audit.treatments_query import (
    CompanyTreatmentView,
    DatasetRule,
    HumanNoteView,
    query_treatments,
)


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "capex.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE companies (
            ticker TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            preferred_source TEXT NOT NULL DEFAULT 'sec',
            edgar_cik TEXT,
            hkex_stock_code TEXT,
            fiscal_year_end_month INTEGER NOT NULL,
            synced_at TEXT NOT NULL DEFAULT '',
            reporting_currency TEXT NOT NULL DEFAULT 'USD'
        );
        CREATE TABLE audit_review_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            audit_run_id TEXT NOT NULL,
            cell_key TEXT NOT NULL,
            human_input TEXT NOT NULL,
            formalized_note_id TEXT,
            formalization_json TEXT,
            reviewer TEXT,
            reviewed_at TEXT NOT NULL,
            UNIQUE(audit_run_id, cell_key)
        );
        """
    )
    conn.executemany(
        "INSERT INTO companies (ticker, name, fiscal_year_end_month) "
        "VALUES (?, ?, ?)",
        [("BABA", "Alibaba", 3), ("MSFT", "Microsoft", 6), ("AMZN", "Amazon", 12)],
    )
    conn.commit()
    conn.close()
    return db_path


# ---- query_treatments ---------------------------------------------

def test_query_treatments_returns_one_view_per_company(tmp_path):
    db = _make_db(tmp_path)
    from capex.db import Database
    views = query_treatments(db=Database(db))
    tickers = {v.ticker for v in views}
    # Real coverage.yaml has 13 companies, but only 3 exist in our
    # seeded DB (the FYE lookup falls back to 12 for missing rows).
    assert len(views) == 13
    assert tickers >= {"BABA", "MSFT", "AMZN"}


def test_query_treatments_baba_has_fiscal_conv_and_cloud_rule(tmp_path):
    db = _make_db(tmp_path)
    from capex.db import Database
    views = query_treatments(db=Database(db))
    baba = next(v for v in views if v.ticker == "BABA")
    assert baba.fiscal_year_end_month == 3
    assert baba.reporting_currency == "CNY"
    assert baba.quarterly_convention.get("default") == "standalone_quarterly"
    cloud = [
        r for r in baba.dataset_rules
        if r.dataset == "cloud_segment_revenue"
    ]
    assert cloud and cloud[0].treatment == "named_segment"
    assert "Cloud Intelligence Group" in cloud[0].segment_names


def test_query_treatments_ticker_filter(tmp_path):
    db = _make_db(tmp_path)
    from capex.db import Database
    views = query_treatments(db=Database(db), ticker_filter="BABA")
    assert len(views) == 1
    assert views[0].ticker == "BABA"


def test_query_treatments_metric_filter_narrows_rules(tmp_path):
    db = _make_db(tmp_path)
    from capex.db import Database
    views = query_treatments(
        db=Database(db),
        ticker_filter="BABA",
        metric_filter="cloud_segment_revenue",
    )
    assert len(views) == 1
    baba = views[0]
    # All returned dataset_rules must include the filtered metric
    assert baba.dataset_rules
    for r in baba.dataset_rules:
        assert "cloud_segment_revenue" in r.metric_keys


def test_query_treatments_merges_reviewer_feedback(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    from capex.db import Database

    # Seed a fake human note via monkey-patch
    fake_note = hn_mod.HumanNote(
        id="HN-TEST-001",
        scope=hn_mod.HumanNoteScope(
            ticker="BABA",
            metric_keys=["cloud_segment_revenue"],
            period_range="FY2023+",
        ),
        guidance="test guidance",
        keywords_to_match=["test"],
        cautions=["be careful"],
        state="active",
        added_at="2026-04-20T00:00:00Z",
        source_audit_run_id="audit-test",
        source_cell_keys=["BABA:cloud_segment_revenue:2023Q1"],
    )
    monkeypatch.setattr(hn_mod, "load_all", lambda path=None: [fake_note])

    # Seed matching audit_review_feedback row
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO audit_review_feedback "
        "(audit_run_id, cell_key, human_input, formalized_note_id, "
        " reviewer, reviewed_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("audit-test", "BABA:cloud_segment_revenue:2023Q1",
         "reviewer typed this", "HN-TEST-001",
         "human_review", "2026-04-20T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    views = query_treatments(db=Database(db), ticker_filter="BABA")
    baba = views[0]
    assert len(baba.human_notes) == 1
    hn = baba.human_notes[0]
    assert hn.id == "HN-TEST-001"
    assert hn.reviewer_input == "reviewer typed this"
    assert hn.reviewer == "human_review"
    assert hn.state == "active"


# ---- HTML output --------------------------------------------------

def test_html_has_seven_nav_pills_with_treatments_active(tmp_path):
    db = _make_db(tmp_path)
    out = tmp_path / "treatments.html"
    from capex.exporters.treatments_html import generate_treatments_html
    generate_treatments_html(output=out, db_path=str(db))
    txt = out.read_text(encoding="utf-8")
    pills = re.findall(
        r'<a class="nav-pill[^"]*" href="([^"]+)">([^<]+)</a>', txt,
    )
    labels = [lbl for _, lbl in pills]
    assert labels == [
        "Home",
        "Cloud / DC Revenue", "Total Revenue", "CapEx",
        "Operating Cash Flow", "Calendar", "Treatments",
    ]
    # Treatments pill is the active one
    assert 'class="nav-pill active" href="treatments.html"' in txt


def test_html_has_one_card_per_company(tmp_path):
    db = _make_db(tmp_path)
    out = tmp_path / "treatments.html"
    from capex.exporters.treatments_html import generate_treatments_html
    generate_treatments_html(output=out, db_path=str(db))
    txt = out.read_text(encoding="utf-8")
    assert txt.count('class="company-card"') == 13
    # BABA content must be present
    assert "Alibaba Group Holding Limited" in txt
    assert "Cloud Intelligence Group" in txt


def test_html_empty_human_notes_shows_friendly_placeholder(tmp_path):
    db = _make_db(tmp_path)
    out = tmp_path / "treatments.html"
    from capex.exporters.treatments_html import generate_treatments_html
    generate_treatments_html(output=out, db_path=str(db))
    txt = out.read_text(encoding="utf-8")
    # Should show "no human notes yet" for every company (human_notes.yaml empty)
    assert "no human notes yet" in txt


def test_html_filter_bar_wires_search_and_dropdowns(tmp_path):
    db = _make_db(tmp_path)
    out = tmp_path / "treatments.html"
    from capex.exporters.treatments_html import generate_treatments_html
    generate_treatments_html(output=out, db_path=str(db))
    txt = out.read_text(encoding="utf-8")
    assert 'id="search"' in txt
    assert 'id="ticker-filter"' in txt
    assert 'id="metric-filter"' in txt
    # Inline JS filter
    assert "applyFilters" in txt


# ---- CLI table ---------------------------------------------------

def test_cli_table_shows_all_columns(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    from capex.audit.treatments_query import query_treatments
    from capex.cli.main import _print_treatments_table
    from capex.db import Database
    views = query_treatments(db=Database(db), ticker_filter="BABA")
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_treatments_table(views)
    out = buf.getvalue()
    for col in ("Ticker", "FYE", "Cur", "Approach", "Rules", "Human"):
        assert col in out
    assert "BABA" in out
    assert "Mar" in out  # FYE month abbreviation
    assert "Cloud Intelligence Group" in out  # segment detail below table


def test_dataset_rule_and_human_note_view_are_dataclasses():
    # Regression guard — field names the HTML/CLI reader depends on
    r = DatasetRule(
        dataset="x", metric_keys=["y"], treatment="z",
    )
    assert r.segment_names == []
    assert r.excluded is False

    v = HumanNoteView(
        id="x", scope={}, guidance="", keywords_to_match=[], cautions=[],
        state="active", added_at="", added_by="", source_audit_run_id="",
        source_cell_keys=[], rationale="",
    )
    assert v.reviewer is None

    c = CompanyTreatmentView(
        ticker="X", full_name="x", category="y", reporting_currency="USD",
        fiscal_year_end_month=12, coverage_start="", filing_cadence={},
        extraction_approach="x", quarterly_convention={}, company_notes="",
    )
    assert c.dataset_rules == []
    assert c.human_notes == []
