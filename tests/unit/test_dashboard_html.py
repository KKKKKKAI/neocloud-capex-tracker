"""Sanity checks for the dashboard landing page (docs/index.html)."""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def _make_empty_db(tmp_path: Path) -> Path:
    """Minimal DB so the stats line renders without explosion."""
    db = tmp_path / "capex.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE companies (
            ticker TEXT PRIMARY KEY, name TEXT DEFAULT '',
            fiscal_year_end_month INTEGER NOT NULL DEFAULT 12
        );
        CREATE TABLE source_documents (
            id INTEGER PRIMARY KEY, ticker TEXT, filing_date TEXT
        );
        CREATE TABLE extractions (
            id INTEGER PRIMARY KEY, metric_key TEXT
        );
        INSERT INTO companies (ticker, fiscal_year_end_month) VALUES
            ('AMZN', 12), ('MSFT', 6);
        INSERT INTO source_documents (ticker, filing_date) VALUES
            ('AMZN', '2025-02-01'), ('MSFT', '2025-07-30');
        INSERT INTO extractions (metric_key) VALUES
            ('revenue'), ('revenue'), ('capital_expenditures');
        """
    )
    conn.commit()
    conn.close()
    return db


def test_dashboard_has_seven_nav_pills_with_home_active(tmp_path):
    from capex.exporters.dashboard_html import generate_dashboard_html
    db = _make_empty_db(tmp_path)
    out = tmp_path / "index.html"
    generate_dashboard_html(output=out, db_path=db)
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
    assert 'class="nav-pill active" href="index.html"' in txt


def test_dashboard_has_six_cards(tmp_path):
    from capex.exporters.dashboard_html import generate_dashboard_html
    db = _make_empty_db(tmp_path)
    out = tmp_path / "index.html"
    generate_dashboard_html(output=out, db_path=db)
    txt = out.read_text(encoding="utf-8")
    assert txt.count('class="card chart-card"') == 4
    assert txt.count('class="card preview-card"') == 2


def test_dashboard_links_to_all_sub_pages(tmp_path):
    from capex.exporters.dashboard_html import generate_dashboard_html
    db = _make_empty_db(tmp_path)
    out = tmp_path / "index.html"
    generate_dashboard_html(output=out, db_path=db)
    txt = out.read_text(encoding="utf-8")
    for href in (
        "cloud.html", "revenue.html", "capex.html",
        "operating_cash_flow.html", "calendar.html", "treatments.html",
    ):
        assert f'href="{href}"' in txt, f"missing link: {href}"


def test_dashboard_references_thumbnails_via_relative_path(tmp_path):
    """Thumbnail paths must stay inside the GitHub-Pages root (docs/)."""
    from capex.exporters.dashboard_html import generate_dashboard_html
    db = _make_empty_db(tmp_path)
    out = tmp_path / "index.html"
    generate_dashboard_html(output=out, db_path=db)
    txt = out.read_text(encoding="utf-8")
    for name in (
        "cloud_revenue_annual.png", "revenue_annual.png",
        "capex_annual.png", "operating_cash_flow_annual.png",
    ):
        # Must be `charts/<name>` — not `../charts/` which escapes docs/.
        assert f'src="charts/{name}"' in txt
        assert f"../charts/{name}" not in txt


def test_dashboard_mirrors_existing_thumbnails_into_docs_charts(tmp_path):
    """If a PNG exists in CHARTS_DIR, it's copied into docs/charts/."""
    from capex.exporters import dashboard_html as dh

    # Stage a fake charts/ dir so we don't touch the repo's real one.
    src_charts = tmp_path / "src_charts"
    src_charts.mkdir()
    png_name = dh.CHART_CARDS[0]["png"]
    (src_charts / png_name).write_bytes(b"\x89PNG\r\n\x1a\nfake")

    # Monkeypatch via direct swap of CHARTS_DIR.
    original = dh.CHARTS_DIR
    dh.CHARTS_DIR = src_charts
    try:
        db = _make_empty_db(tmp_path)
        out = tmp_path / "docs" / "index.html"
        dh.generate_dashboard_html(output=out, db_path=db)
        assert (out.parent / "charts" / png_name).exists()
    finally:
        dh.CHARTS_DIR = original


def test_dashboard_stats_line_reflects_db(tmp_path):
    from capex.exporters.dashboard_html import generate_dashboard_html
    db = _make_empty_db(tmp_path)
    out = tmp_path / "index.html"
    generate_dashboard_html(output=out, db_path=db)
    txt = out.read_text(encoding="utf-8")
    assert "2 companies" in txt
    assert "3 data points" in txt
    assert "2025-07-30" in txt
