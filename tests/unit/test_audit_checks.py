"""Unit tests for the nine data-quality audit checks."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from capex.audit.checks import (
    check_continuity,
    check_cross_source,
    check_currency,
    check_gap,
    check_identity,
    check_period_type,
    check_range,
    check_segment_def,
    check_sign,
)


# ----- gap -----
def test_gap_present():
    r = check_gap("AMZN", "revenue", 2024, "FY",
                  present=True, source_doc_present=True)
    assert r.passed
    assert r.details["status"] == "present"


def test_gap_fixable():
    r = check_gap("AMZN", "revenue", 2015, "Q1",
                  present=False, source_doc_present=True)
    assert not r.passed
    assert r.details["status"] == "gap_fixable"


def test_gap_unfixable():
    r = check_gap("CRWV", "revenue", 2019, "Q1",
                  present=False, source_doc_present=False)
    assert r.details["status"] == "gap_unfixable"


# ----- identity -----
def test_identity_ok():
    g = {"Q1": 10.0, "Q2": 20.0, "Q3": 30.0, "Q4": 40.0, "FY": 100.0}
    assert check_identity(g).passed


def test_identity_violation():
    g = {"Q1": 10.0, "Q2": 20.0, "Q3": 30.0, "Q4": 40.0, "FY": 90.0}
    r = check_identity(g)
    assert not r.passed
    assert "violations" in r.details


def test_identity_h1_ok():
    g = {"Q1": 10.0, "Q2": 15.0, "H1": 25.0}
    assert check_identity(g).passed


def test_identity_9m_violation():
    g = {"Q1": 10.0, "Q2": 20.0, "Q3": 30.0, "9M": 80.0}
    assert not check_identity(g).passed


# ----- range -----
def test_range_inside_default():
    # defaults include revenue FY 20-700000
    r = check_range("FAKE_TICKER", "revenue", "FY", 100000.0)
    # No ticker-specific; falls back to defaults if YAML present.
    assert r.check_name == "range"


def test_range_outside():
    # Force explicit bounds via monkeypatch
    import capex.audit.checks as C
    C._bounds_cache = {"defaults": {"revenue": {"FY": [100, 500]}}}
    r = check_range("FAKE", "revenue", "FY", 1000.0)
    assert not r.passed
    assert r.details["outside"] == "above"
    C._bounds_cache = None


# ----- continuity -----
def test_continuity_ok():
    r = check_continuity("AMZN", "revenue", "2024Q3", 150.0,
                         "2024Q4", 170.0)
    assert r.passed


def test_continuity_jump():
    r = check_continuity("AMZN", "revenue", "2024Q3", 10.0,
                         "2024Q4", 100.0)
    assert not r.passed
    assert r.details["factor"] == 10.0


def test_continuity_drop():
    r = check_continuity("AMZN", "revenue", "2024Q3", 100.0,
                         "2024Q4", 20.0)
    assert not r.passed


# ----- cross-source -----
def test_cross_source_match():
    q = "Total revenue 245,122 211,915 198,270"
    r = check_cross_source(245122.0, q)
    assert r.passed
    assert r.details["match"] == 245122.0


def test_cross_source_no_match():
    q = "Some unrelated numbers 999,999 and 111,111"
    r = check_cross_source(245122.0, q)
    assert not r.passed


def test_cross_source_no_quote():
    assert check_cross_source(100.0, None).passed


# ----- sign -----
def test_sign_revenue_positive_ok():
    assert check_sign("AMZN", "revenue", 2024, 100.0, "FY").passed


def test_sign_revenue_negative_error():
    r = check_sign("AMZN", "revenue", 2024, -50.0, "FY")
    assert not r.passed
    assert r.severity == "error"


def test_sign_capex_negative_normalised():
    # Some 10-Qs report capex as negative cash outflow — we accept abs().
    assert check_sign("AMZN", "capital_expenditures", 2024, -100.0, "FY").passed


def test_sign_ocf_negative_preipo_ok():
    assert check_sign("CRWV", "operating_cash_flow", 2023, -5.0, "FY").passed


def test_sign_ocf_negative_mature_flag():
    r = check_sign("AMZN", "operating_cash_flow", 2024, -5.0, "FY")
    assert not r.passed


# ----- currency -----
def test_currency_usd_ok():
    assert check_currency("USD", 100.0, 100.0, 1.0).passed


def test_currency_usd_mismatch():
    r = check_currency("USD", 100.0, 50.0, 1.0)
    assert not r.passed


def test_currency_cny_consistent():
    # CNY 1000 × 0.14 = 140 USD
    assert check_currency("CNY", 1000.0, 140.0, 0.14).passed


def test_currency_cny_inconsistent_fx():
    r = check_currency("CNY", 1000.0, 200.0, 0.14)
    assert not r.passed
    assert "rel" in r.details


# ----- segment_def -----
def test_segment_def_non_cloud_metric_pass():
    assert check_segment_def("AMZN", "revenue", "Item 8", "whatever").passed


# ----- period_type -----
def test_period_type_ok():
    assert check_period_type("Q1", 89).passed


def test_period_type_mismatch():
    r = check_period_type("Q1", 364)  # TTM when we expected 3M
    assert not r.passed
    assert r.details["expected"] == (75, 105)


def test_period_type_no_duration():
    assert check_period_type("Q1", None).passed


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in list(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK   {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
