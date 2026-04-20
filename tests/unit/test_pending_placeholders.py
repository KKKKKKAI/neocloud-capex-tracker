"""Tests for the trailing-quarter carry-forward placeholder algorithm."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from capex.exporters.interactive_chart import (
    PLACEHOLDER_GREY,
    _apply_pending_placeholders,
)


def _qs(*labels):
    return sorted(labels)


# ---- Algorithm ----------------------------------------------------

def test_no_placeholders_when_all_tickers_report_latest_quarter():
    quarters = _qs("2025Q3", "2025Q4")
    by_quarter = {
        "AMZN": {"2025Q3": 100, "2025Q4": 110},
        "MSFT": {"2025Q3": 200, "2025Q4": 210},
    }
    pm, incomplete = _apply_pending_placeholders(by_quarter, quarters)
    assert pm == {}
    assert incomplete == set()
    # by_quarter unchanged
    assert by_quarter["AMZN"] == {"2025Q3": 100, "2025Q4": 110}


def test_fills_missing_latest_quarter_for_pending_ticker():
    quarters = _qs("2025Q3", "2025Q4", "2026Q1")
    by_quarter = {
        "AMZN": {"2025Q3": 100, "2025Q4": 110, "2026Q1": 120},
        "BABA": {"2025Q3": 200, "2025Q4": 210},  # 2026Q1 pending
    }
    pm, incomplete = _apply_pending_placeholders(by_quarter, quarters)
    # BABA's 2026Q1 should be filled with 2025Q4 value (210)
    assert by_quarter["BABA"]["2026Q1"] == 210
    assert pm == {("BABA", "2026Q1"): "2025Q4"}
    assert incomplete == {"2026Q1"}
    # AMZN untouched — they did report
    assert ("AMZN", "2026Q1") not in pm


def test_walk_stops_at_first_complete_quarter():
    quarters = _qs("2025Q2", "2025Q3", "2025Q4", "2026Q1")
    by_quarter = {
        "AMZN": {q: 100 + i for i, q in enumerate(quarters)},  # all reported
        "BABA": {"2025Q2": 200, "2025Q3": 210, "2025Q4": 220},  # missing 2026Q1
    }
    pm, incomplete = _apply_pending_placeholders(by_quarter, quarters)
    # Only 2026Q1 is incomplete; earlier quarters had full coverage, so the
    # walk stops — historical data must NOT be touched.
    assert incomplete == {"2026Q1"}
    assert len(pm) == 1


def test_walks_through_multiple_trailing_incomplete_quarters():
    quarters = _qs("2025Q3", "2025Q4", "2026Q1")
    by_quarter = {
        "AMZN": {q: 100 for q in quarters},          # reported all
        "BABA": {"2025Q3": 200},                       # missing Q4 + Q1
    }
    pm, incomplete = _apply_pending_placeholders(by_quarter, quarters)
    assert ("BABA", "2025Q4") in pm
    assert ("BABA", "2026Q1") in pm
    assert by_quarter["BABA"]["2025Q4"] == 200        # carried from 2025Q3
    assert by_quarter["BABA"]["2026Q1"] == 200        # carried from prior
    assert incomplete == {"2025Q4", "2026Q1"}


def test_skips_ticker_with_no_prior_data():
    # NBIS starts coverage 2024; before that, no data.
    quarters = _qs("2023Q4", "2024Q1", "2024Q2")
    by_quarter = {
        "AMZN": {q: 100 for q in quarters},
        "NBIS": {"2024Q1": 50, "2024Q2": 55},          # legitimate start
    }
    pm, incomplete = _apply_pending_placeholders(by_quarter, quarters)
    # Nothing pending (both tickers report latest)
    assert pm == {}
    assert incomplete == set()


def test_ticker_never_reported_is_never_placeholdered():
    # Tencent reports annual only for cloud — has no quarterly data.
    quarters = _qs("2025Q3", "2025Q4", "2026Q1")
    by_quarter = {
        "AMZN": {"2025Q3": 100, "2025Q4": 100, "2026Q1": 100},
        "0700": {},  # never reported quarterly
    }
    pm, incomplete = _apply_pending_placeholders(
        by_quarter, quarters, exclude_tickers=set(),
    )
    # 0700 is not in "expected" because has no data; walk finds nothing missing
    assert pm == {}
    assert incomplete == set()


def test_respects_exclude_tickers():
    quarters = _qs("2025Q3", "2025Q4", "2026Q1")
    by_quarter = {
        "AMZN": {"2025Q3": 100, "2025Q4": 100, "2026Q1": 100},
        "0700": {"2025Q3": 50, "2025Q4": 55},  # would be pending
    }
    pm, incomplete = _apply_pending_placeholders(
        by_quarter, quarters, exclude_tickers={"0700"},
    )
    # 0700 excluded → not in expected → no placeholders
    assert pm == {}
    assert incomplete == set()


def test_empty_inputs_return_empty():
    pm, incomplete = _apply_pending_placeholders({}, [])
    assert pm == {}
    assert incomplete == set()


def test_gap_cap_skips_long_lag_tickers():
    """NBIS scenario: 1 quarter of cloud data at 2025Q1, chart latest 2026Q1.
    With max_gap=2, the walk must treat NBIS as a non-quarterly-reporter
    for 2026Q1 rather than filling 4 consecutive grey bars."""
    quarters = _qs("2024Q4", "2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1")
    by_quarter = {
        "AMZN": {q: 100 for q in quarters},      # all reported
        "NBIS": {"2025Q1": 50},                    # stopped at 2025Q1
    }
    pm, incomplete = _apply_pending_placeholders(by_quarter, quarters)
    # NBIS should NOT be placeholder'd anywhere because gap > 2
    assert not any(t == "NBIS" for (t, _) in pm)
    # No placeholders at all → no incomplete quarters
    assert incomplete == set()


def test_gap_cap_honours_custom_value():
    # Use a real ticker in STACK_ORDER (BABA) with a bi-annual-like cadence.
    quarters = _qs("2024Q4", "2025Q1", "2025Q2", "2025Q3", "2025Q4")
    by_quarter = {
        "AMZN": {q: 100 for q in quarters},
        "BABA": {"2024Q4": 50, "2025Q2": 55},    # latest = 2025Q2
    }
    # max_gap=2: BABA latest 2025Q2 vs chart latest 2025Q4 → gap=2 ≤ 2
    # → BABA is eligible; fill every trailing quarter for them.
    pm, _inc = _apply_pending_placeholders(by_quarter, quarters)
    assert ("BABA", "2025Q3") in pm
    assert ("BABA", "2025Q4") in pm

    # max_gap=1: BABA gap to chart latest = 2 > 1 → BABA skipped entirely.
    by_quarter = {
        "AMZN": {q: 100 for q in quarters},
        "BABA": {"2024Q4": 50, "2025Q2": 55},
    }
    pm2, _ = _apply_pending_placeholders(by_quarter, quarters, max_gap=1)
    assert ("BABA", "2025Q3") not in pm2
    assert ("BABA", "2025Q4") not in pm2


# ---- HTML integration --------------------------------------------

def _render_html_for_metric(metric_key):
    """Render an interactive chart HTML for the given metric."""
    import sqlite3

    from capex.exporters.interactive_chart import (
        METRIC_CONFIGS,
        _build_html,
        _load_annual,
        _load_quarterly,
    )
    db_path = Path(__file__).resolve().parents[2] / "data" / "db" / "capex.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cfg = METRIC_CONFIGS[metric_key]
    annual = _load_annual(conn, metric_key, cfg["exclude_tickers"])
    quarterly = _load_quarterly(conn, metric_key, cfg["exclude_tickers"])
    conn.close()
    return _build_html(annual, quarterly, metric_key, cfg)


def test_html_embeds_incomplete_quarters_global():
    html = _render_html_for_metric("revenue")
    # The global must exist in output; value may be [] or list of strings.
    m = re.search(r"window\.__INCOMPLETE_QUARTERS\s*=\s*(\[[^\]]*\])", html)
    assert m, "missing window.__INCOMPLETE_QUARTERS in output"
    parsed = json.loads(m.group(1))
    assert isinstance(parsed, list)


def test_html_quarterly_traces_have_per_bar_marker_color_arrays():
    html = _render_html_for_metric("cloud_segment_revenue")
    # Grab the quarterly traces JSON block from `var quarterlyTraces = [...];`
    m = re.search(r"var quarterlyTraces\s*=\s*(\[.*?\]);", html, re.DOTALL)
    assert m, "quarterly traces assignment not found in HTML"
    traces = json.loads(m.group(1))
    assert traces, "expected at least one quarterly trace"
    for trace in traces:
        # marker.color is now an array (one per bar); previously a single str.
        color = trace["marker"]["color"]
        assert isinstance(color, list), (
            f"trace {trace['name']} marker.color should be a list"
        )
        assert len(color) == len(trace["x"]) == len(trace["y"])
        # customdata aligned with x/y and tagged with placeholder flag + prior
        assert "customdata" in trace
        assert len(trace["customdata"]) == len(trace["x"])


def test_placeholder_grey_appears_only_when_quarter_incomplete(monkeypatch):
    """Inject a synthetic incomplete scenario via monkeypatch, confirm
    at least one bar in one trace is the placeholder grey."""
    import sqlite3

    from capex.exporters.interactive_chart import (
        METRIC_CONFIGS,
        _build_html,
        _load_annual,
    )
    db_path = Path(__file__).resolve().parents[2] / "data" / "db" / "capex.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cfg = METRIC_CONFIGS["revenue"]
    annual = _load_annual(conn, "revenue", cfg["exclude_tickers"])
    # Seed a synthetic quarterly dict where AMZN has one extra quarter
    # that BABA doesn't.
    quarterly = {
        "quarters": ["2025Q3", "2025Q4", "2026Q1"],
        "by_quarter": {
            "AMZN": {"2025Q3": 100e3, "2025Q4": 110e3, "2026Q1": 120e3},
            "BABA": {"2025Q3": 200e3, "2025Q4": 210e3},
        },
    }
    conn.close()
    html = _build_html(annual, quarterly, "revenue", cfg)
    # BABA's 2026Q1 should be carried forward; its colour in that slot
    # is the placeholder grey.
    assert PLACEHOLDER_GREY in html
    # And incomplete_quarters must include 2026Q1.
    m = re.search(r"window\.__INCOMPLETE_QUARTERS\s*=\s*(\[[^\]]*\])", html)
    assert m
    assert "2026Q1" in json.loads(m.group(1))


def test_pending_caption_only_renders_when_incomplete_non_empty():
    # Cloud chart likely has no placeholders right now (data up to 2025Q4)
    html_clean = _render_html_for_metric("cloud_segment_revenue")
    if not re.search(r"window\.__INCOMPLETE_QUARTERS\s*=\s*\[\s*\]", html_clean):
        # There are incomplete quarters — then caption should render
        assert 'class="note pending-note"' in html_clean
    else:
        assert 'class="note pending-note"' not in html_clean
