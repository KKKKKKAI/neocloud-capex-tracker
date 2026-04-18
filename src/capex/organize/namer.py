"""Canonical filename grammar for the organize-sources skill.

Pure functions, no I/O. Given the metadata fields from a source_documents
row (or sidecar), produce the canonical filename and the fiscal-year
folder name. Re-runnable: same inputs always produce the same outputs.

Naming grammar (authoritative):
    [yyyy.mm.dd][TICKER][PERIOD][FORM].<ext>

Where:
    yyyy.mm.dd   - filing_date (NOT period_of_report)
    TICKER       - companies.ticker key
    PERIOD       - AR / Q1 / Q2 / Q3 / H1 / H2 (derived from form + period)
    FORM         - 10-K / 10-Q / 20-F / HK-AR / HK-IR
    ext          - the original file extension (.htm or .pdf)

Period derivation rules:
    10-K, 20-F, HK-AR  → AR (always)
    10-Q               → Q1, Q2, or Q3 based on (period_of_report month, FYE month).
                         Q4 raises PeriodDerivationError because 10-Q does not cover Q4.
    HK-IR              → H1 or H2 based on which fiscal half the period ends in.

Fiscal year for the folder:
    A company with FYE month 6 (Microsoft) and period 2025-06-30 is FY2025.
    A 10-Q with period 2025-09-30 (start of next fiscal year) is FY2026.
    Rule: if period_month <= fye_month, fiscal_year = calendar_year;
          otherwise fiscal_year = calendar_year + 1.
"""
from __future__ import annotations

VALID_FORM_TYPES = ("10-K", "10-Q", "20-F", "HK-AR", "HK-IR")
ANNUAL_FORM_TYPES = ("10-K", "20-F", "HK-AR")


class PeriodDerivationError(ValueError):
    """Cannot derive a period token from the given inputs."""

    def __init__(
        self,
        ticker: str,
        form_type: str,
        period_of_report: str,
        reason: str,
    ) -> None:
        super().__init__(
            f"{ticker} {form_type} period_of_report={period_of_report}: {reason}"
        )
        self.ticker = ticker
        self.form_type = form_type
        self.period_of_report = period_of_report
        self.reason = reason


def compute_period_token(
    form_type: str,
    period_of_report: str,
    fiscal_year_end_month: int,
    *,
    ticker: str = "",
) -> str:
    """Derive the PERIOD token (AR/Q1/Q2/Q3/H1/H2) from form + period + FYE.

    Raises PeriodDerivationError on Q4 for 10-Q (rolls into the annual)
    or unknown form types.
    """
    if form_type not in VALID_FORM_TYPES:
        raise PeriodDerivationError(
            ticker, form_type, period_of_report, f"unknown form_type {form_type!r}"
        )

    if form_type in ANNUAL_FORM_TYPES:
        return "AR"

    period_month = _parse_month(period_of_report)
    elapsed = _months_into_fiscal_year(period_month, fiscal_year_end_month)

    if form_type == "10-Q":
        if elapsed <= 3:
            return "Q1"
        if elapsed <= 6:
            return "Q2"
        if elapsed <= 9:
            return "Q3"
        # Elapsed in 10..12 means we landed in Q4 — 10-Q does not cover Q4.
        raise PeriodDerivationError(
            ticker,
            form_type,
            period_of_report,
            f"computes to Q4 (month {period_month} of FYE {fiscal_year_end_month} fiscal year, "
            f"elapsed={elapsed}); 10-Q does not cover Q4",
        )

    if form_type == "HK-IR":
        return "H1" if elapsed <= 6 else "H2"

    raise PeriodDerivationError(ticker, form_type, period_of_report, "unhandled form_type")


def compute_fiscal_year(period_of_report: str, fiscal_year_end_month: int) -> int:
    """Compute the fiscal year a period_of_report belongs to.

    Convention: a fiscal year is named for the calendar year in which it
    *ends*. For Microsoft (FYE June), the fiscal year ending 2025-06-30
    is FY2025. The 10-Q with period 2025-09-30 belongs to FY2026.
    """
    parts = period_of_report.split("-")
    year = int(parts[0])
    month = int(parts[1])
    if month <= fiscal_year_end_month:
        return year
    return year + 1


def canonical_name(
    filing_date: str,
    ticker: str,
    period_token: str,
    form_type: str,
    extension: str,
) -> str:
    """Build the canonical filename: [yyyy.mm.dd][TICKER][PERIOD][FORM].ext

    Args:
        filing_date: ISO date string YYYY-MM-DD.
        ticker: companies.ticker key, used as-is (already canonical).
        period_token: AR/Q1/Q2/Q3/H1/H2.
        form_type: 10-K/10-Q/20-F/HK-AR/HK-IR.
        extension: file extension including the leading dot (".htm" or ".pdf").

    Returns:
        The canonical filename. Pure function — no I/O, no path resolution.
    """
    parts = filing_date.split("-")
    yyyy_mm_dd = f"{parts[0]}.{parts[1]}.{parts[2]}"
    if not extension.startswith("."):
        extension = "." + extension
    return f"[{yyyy_mm_dd}][{ticker}][{period_token}][{form_type}]{extension}"


# ----------------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------------


def _parse_month(iso_date: str) -> int:
    """Extract the month integer from an ISO date string YYYY-MM-DD."""
    return int(iso_date.split("-")[1])


def _months_into_fiscal_year(period_month: int, fye_month: int) -> int:
    """Return how many months into the fiscal year period_month falls (1..12).

    For Microsoft (FYE month 6, June):
        July (7)      → 1
        August (8)    → 2
        September (9) → 3   (Q1 ends here)
        October (10)  → 4
        ...
        December (12) → 6   (Q2 ends here)
        January (1)   → 7
        ...
        March (3)     → 9   (Q3 ends here)
        April (4)     → 10
        ...
        June (6)      → 12  (Q4 / fiscal year end)
    """
    return ((period_month - fye_month - 1) % 12) + 1
