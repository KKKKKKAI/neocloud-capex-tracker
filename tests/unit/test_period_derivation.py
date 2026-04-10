"""Period derivation unit tests for organize-sources naming.

Covers the 5 non-Dec FYE companies in the watchlist (MSFT, ORCL, APLD,
IREN, BABA) plus calendar-year baselines and the Q4 edge case.

Compatible with pytest if installed; if not, the file is also runnable
as a script (`python tests/unit/test_period_derivation.py`).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script without pytest installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from capex.organize.namer import (
    PeriodDerivationError,
    canonical_name,
    compute_fiscal_year,
    compute_period_token,
)

# (ticker, fye_month, form, period, expected_token, expected_fy)
CASES = [
    # Microsoft (FYE June)
    ("MSFT", 6, "10-K", "2025-06-30", "AR", 2025),
    ("MSFT", 6, "10-Q", "2025-09-30", "Q1", 2026),
    ("MSFT", 6, "10-Q", "2025-12-31", "Q2", 2026),
    ("MSFT", 6, "10-Q", "2026-03-31", "Q3", 2026),
    # Oracle (FYE May)
    ("ORCL", 5, "10-K", "2025-05-31", "AR", 2025),
    ("ORCL", 5, "10-Q", "2025-08-31", "Q1", 2026),
    ("ORCL", 5, "10-Q", "2025-11-30", "Q2", 2026),
    ("ORCL", 5, "10-Q", "2026-02-28", "Q3", 2026),
    # Applied Digital (FYE May)
    ("APLD", 5, "10-K", "2025-05-31", "AR", 2025),
    ("APLD", 5, "10-Q", "2025-08-31", "Q1", 2026),
    # IREN (FYE June, files 20-F)
    ("IREN", 6, "20-F", "2024-06-30", "AR", 2024),
    ("IREN", 6, "20-F", "2025-06-30", "AR", 2025),
    # Alibaba (FYE March, files 20-F)
    ("BABA", 3, "20-F", "2025-03-31", "AR", 2025),
    # Calendar-year baselines
    ("GOOGL", 12, "10-K", "2025-12-31", "AR", 2025),
    ("GOOGL", 12, "10-Q", "2025-03-31", "Q1", 2025),
    ("GOOGL", 12, "10-Q", "2025-06-30", "Q2", 2025),
    ("GOOGL", 12, "10-Q", "2025-09-30", "Q3", 2025),
    ("AMZN", 12, "10-K", "2024-12-31", "AR", 2024),
    # HKEX (Tencent, FYE December)
    ("0700", 12, "HK-AR", "2024-12-31", "AR", 2024),
    ("0700", 12, "HK-IR", "2024-06-30", "H1", 2024),
    # Half-year edge: HKEX-IR landing in second half
    ("0700", 12, "HK-IR", "2024-12-31", "H2", 2024),
]


def test_period_derivation_cases():
    """Run all canonical (form, period, FYE) → token + fiscal year cases."""
    failures = []
    for ticker, fye, form, period, expected_token, expected_fy in CASES:
        token = compute_period_token(form, period, fye, ticker=ticker)
        fy = compute_fiscal_year(period, fye)
        if token != expected_token or fy != expected_fy:
            failures.append(
                f"{ticker} {form} {period} (FYE {fye}): "
                f"got {token} FY{fy}, expected {expected_token} FY{expected_fy}"
            )
    assert not failures, "Period derivation failures:\n  " + "\n  ".join(failures)


def test_q4_10q_raises():
    """A 10-Q whose period_of_report lands on the fiscal year end is Q4 — must raise."""
    try:
        compute_period_token("10-Q", "2025-06-30", 6, ticker="MSFT")
    except PeriodDerivationError as e:
        assert "Q4" in str(e), f"expected Q4 in error, got: {e}"
        return
    raise AssertionError("expected PeriodDerivationError for Q4 10-Q")


def test_canonical_name_format():
    """Filename format: [dd.mm.yyyy][TICKER][PERIOD][FORM].ext"""
    name = canonical_name("2025-07-30", "MSFT", "AR", "10-K", ".htm")
    assert name == "[30.07.2025][MSFT][AR][10-K].htm", name

    name = canonical_name("2024-10-24", "NVDA", "Q3", "10-Q", ".htm")
    assert name == "[24.10.2024][NVDA][Q3][10-Q].htm", name

    # Extension without dot is also accepted.
    name = canonical_name("2025-03-20", "0700", "AR", "HK-AR", "pdf")
    assert name == "[20.03.2025][0700][AR][HK-AR].pdf", name


def test_unknown_form_type_raises():
    try:
        compute_period_token("8-K", "2025-06-30", 12, ticker="MSFT")
    except PeriodDerivationError:
        return
    raise AssertionError("expected PeriodDerivationError for 8-K")


if __name__ == "__main__":
    # Standalone runner for environments without pytest.
    tests = [
        test_period_derivation_cases,
        test_q4_10q_raises,
        test_canonical_name_format,
        test_unknown_form_type_raises,
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
