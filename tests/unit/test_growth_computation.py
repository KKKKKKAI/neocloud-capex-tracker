"""Unit tests for the chart growth helper.

Covers the four branches of `prior_index_for` + the missing-comparator
edges of `compute_growth`. Must stay in lock-step with the JS
implementation inside `_HTML_TEMPLATE`.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from capex.exporters._growth import compute_growth, prior_index_for

QUARTERLY_LABELS = [
    "2019Q1", "2019Q2", "2019Q3", "2019Q4",
    "2020Q1", "2020Q2", "2020Q3", "2020Q4",
    "2021Q1",
]
ANNUAL_LABELS = ["FY2019", "FY2020", "FY2021", "FY2022", "FY2023"]


def test_prior_index_qoq_quarterly():
    # QoQ: always i - 1, regardless of calendar quarter
    for i in range(1, len(QUARTERLY_LABELS)):
        assert prior_index_for(i, QUARTERLY_LABELS, "qoq", "quarterly") == i - 1
    assert prior_index_for(0, QUARTERLY_LABELS, "qoq", "quarterly") == -1


def test_prior_index_yoy_quarterly_same_quarter():
    # YoY quarterly: same-quarter-1-year-prior by label parse
    assert prior_index_for(4, QUARTERLY_LABELS, "yoy", "quarterly") == 0  # 2020Q1 -> 2019Q1
    assert prior_index_for(7, QUARTERLY_LABELS, "yoy", "quarterly") == 3  # 2020Q4 -> 2019Q4
    assert prior_index_for(8, QUARTERLY_LABELS, "yoy", "quarterly") == 4  # 2021Q1 -> 2020Q1
    # First year has no prior
    assert prior_index_for(0, QUARTERLY_LABELS, "yoy", "quarterly") == -1
    assert prior_index_for(3, QUARTERLY_LABELS, "yoy", "quarterly") == -1


def test_prior_index_yoy_annual():
    # Annual YoY: always i - 1
    for i in range(1, len(ANNUAL_LABELS)):
        assert prior_index_for(i, ANNUAL_LABELS, "yoy", "annual") == i - 1
    assert prior_index_for(0, ANNUAL_LABELS, "yoy", "annual") == -1


def test_prior_index_yoy_missing_label_returns_minus_one():
    # Gap in the label list (2020Q2 missing) → same-quarter lookup returns -1
    labels = ["2019Q1", "2019Q2", "2020Q1", "2020Q3"]
    assert prior_index_for(3, labels, "yoy", "quarterly") == -1  # 2020Q3 has no 2019Q3


def test_compute_growth_quarterly_yoy_simple():
    # Construct a series where YoY is exactly 50% for every quarter
    series = [100.0, 110.0, 120.0, 130.0, 150.0, 165.0, 180.0, 195.0, 225.0]
    out = compute_growth(series, QUARTERLY_LABELS, "yoy", "quarterly")
    # First year entries have no prior-year comparator
    assert out[:4] == [None, None, None, None]
    # Following year should be (1.5x - 1) * 100 == 50%
    for i, v in enumerate(out[4:8]):
        assert v is not None
        assert math.isclose(v, 50.0, rel_tol=1e-9), f"idx {4+i} got {v}"
    # 2021Q1 -> 2020Q1: 225/150 = 1.5x = 50%
    assert math.isclose(out[8], 50.0, rel_tol=1e-9)


def test_compute_growth_qoq_sequential():
    series = [100.0, 110.0, 121.0]
    labels = ["2020Q1", "2020Q2", "2020Q3"]
    out = compute_growth(series, labels, "qoq", "quarterly")
    assert out[0] is None
    assert math.isclose(out[1], 10.0, rel_tol=1e-9)
    assert math.isclose(out[2], 10.0, rel_tol=1e-9)


def test_compute_growth_missing_values():
    # Current or prior None / zero / negative → None
    series = [100.0, None, 0.0, 120.0]
    labels = ["2019Q1", "2020Q1", "2021Q1", "2022Q1"]
    out = compute_growth(series, labels, "yoy", "quarterly")
    assert out == [None, None, None, None]  # None/0 kills everything downstream


def test_compute_growth_annual():
    series = [100.0, 120.0, 150.0, 180.0]
    labels = ["FY2020", "FY2021", "FY2022", "FY2023"]
    out = compute_growth(series, labels, "yoy", "annual")
    assert out[0] is None
    assert math.isclose(out[1], 20.0)   # 120/100 - 1
    assert math.isclose(out[2], 25.0)   # 150/120 - 1
    assert math.isclose(out[3], 20.0)   # 180/150 - 1


def test_compute_growth_length_mismatch_raises():
    try:
        compute_growth([1.0, 2.0], ["a", "b", "c"], "qoq", "quarterly")
    except ValueError:
        return
    raise AssertionError("expected ValueError on length mismatch")


def test_compute_growth_unknown_mode_raises():
    try:
        prior_index_for(1, ANNUAL_LABELS, "ttm", "annual")
    except ValueError:
        return
    raise AssertionError("expected ValueError on unknown mode")


if __name__ == "__main__":
    tests = [
        test_prior_index_qoq_quarterly,
        test_prior_index_yoy_quarterly_same_quarter,
        test_prior_index_yoy_annual,
        test_prior_index_yoy_missing_label_returns_minus_one,
        test_compute_growth_quarterly_yoy_simple,
        test_compute_growth_qoq_sequential,
        test_compute_growth_missing_values,
        test_compute_growth_annual,
        test_compute_growth_length_mismatch_raises,
        test_compute_growth_unknown_mode_raises,
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
