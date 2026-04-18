"""Calendar-quarter labeling for interactive chart + Excel quarterly columns.

The chart x-axis and Excel quarterly columns must use *calendar* quarters
derived from period_of_report, not fiscal-quarter indices. Otherwise
non-Dec-FYE companies (MSFT=Jun, BABA=Mar, ORCL=May, APLD=May, IREN=Jun)
get mis-aligned labels that sort out of chronological order.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from capex.exporters.excel import (
    _calendar_quarter as ex_calendar_quarter,
)
from capex.exporters.excel import (
    _period_sort_key,
)
from capex.exporters.excel import (
    _qlabel as ex_qlabel,
)
from capex.exporters.interactive_chart import (
    _calendar_quarter as ic_calendar_quarter,
)
from capex.exporters.interactive_chart import (
    _qlabel as ic_qlabel,
)
from capex.exporters.interactive_chart import (
    _qsort_key,
)


def test_calendar_quarter_mapping():
    """period_of_report month -> calendar quarter."""
    cases = [
        ("2025-03-31", (2025, 1)),
        ("2025-06-30", (2025, 2)),
        ("2025-09-30", (2025, 3)),
        ("2025-12-31", (2025, 4)),
        ("2026-01-01", (2026, 1)),
        ("2026-02-28", (2026, 1)),
        ("2026-04-01", (2026, 2)),
        ("2025-05-31", (2025, 2)),   # ORCL FYE
        ("2025-08-31", (2025, 3)),   # ORCL Q1 FY26
    ]
    for period, expected in cases:
        assert ic_calendar_quarter(period) == expected, period
        assert ex_calendar_quarter(period) == expected, period


def test_non_dec_fye_labels():
    """MSFT, ORCL, BABA quarter ends map to correct calendar quarter labels."""
    # MSFT FY2026 quarters
    assert ic_qlabel("2025-09-30") == "2025Q3"   # FY26 Q1
    assert ic_qlabel("2025-12-31") == "2025Q4"   # FY26 Q2
    assert ic_qlabel("2026-03-31") == "2026Q1"   # FY26 Q3
    assert ic_qlabel("2026-06-30") == "2026Q2"   # FY26 Q4 (10-K)
    # BABA FY2026 quarters (FYE March)
    assert ic_qlabel("2025-06-30") == "2025Q2"   # FY26 Q1
    assert ic_qlabel("2025-12-31") == "2025Q4"   # FY26 Q3
    # Matches between interactive_chart and excel helpers
    assert ex_qlabel("2025-09-30") == ic_qlabel("2025-09-30")


def test_qsort_key_chronological():
    labels = ["2026Q3", "2025Q4", "2026Q1", "2025Q3", "2026Q2"]
    assert sorted(labels, key=_qsort_key) == [
        "2025Q3", "2025Q4", "2026Q1", "2026Q2", "2026Q3"
    ]


def test_period_sort_key_mixed_annual_quarterly():
    """Excel sheets may mix FY{year} and YYYYQN labels; both sort together."""
    labels = ["FY2025", "FY2024", "FY2026"]
    assert sorted(labels, key=_period_sort_key) == ["FY2024", "FY2025", "FY2026"]
    qlabels = ["2026Q3", "2025Q4", "2026Q1"]
    assert sorted(qlabels, key=_period_sort_key) == ["2025Q4", "2026Q1", "2026Q3"]


if __name__ == "__main__":
    tests = [
        test_calendar_quarter_mapping,
        test_non_dec_fye_labels,
        test_qsort_key_chronological,
        test_period_sort_key_mixed_annual_quarterly,
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
