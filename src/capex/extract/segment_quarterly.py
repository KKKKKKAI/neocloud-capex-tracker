"""Quarterly segment revenue extractor for 10-Q filings.

10-Q segment tables follow a consistent SEC convention: columns are
"Three Months Ended <DATE>" (current and prior year) followed by either
"Six/Nine Months Ended <DATE>" (current and prior year). For a given
segment row labeled with revenue/net sales, we expect 4 dollar values
laid out as:

    [prior-year 3M] [current-year 3M] [prior-year 6M/9M] [current-year 6M/9M]

Extracts the current-year 3M and 6M/9M values for the target segment.

Works across AMZN, GOOGL, MSFT, ORCL 10-Q vintages from 2015 onwards.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..read.text import extract_text

# Period header regexes. Tolerate "Three Months Ended September 30,"
# "Three Months EndedSeptember 30," (no space), and line breaks.
PERIOD_HEADER_RE = re.compile(
    r"(Three|Six|Nine)\s+Months?\s+Ended\s*([A-Z][a-z]+\s+\d{1,2}),?",
    re.IGNORECASE,
)

# Data row patterns (4 values) — try the pattern with '$' and with
# commas to cover both AMZN/GOOGL styles.
VALUES_4_RE = re.compile(
    r"\$?\s*([\d,]+)\s+\$?\s*([\d,]+)\s+\$?\s*([\d,]+)\s+\$?\s*([\d,]+)"
)
VALUES_2_RE = re.compile(
    r"\$?\s*([\d,]+)\s+\$?\s*([\d,]+)"
)


@dataclass
class SegmentQuarterlyResult:
    period_type: str     # 'Q1', 'H1', '9M'
    value_millions: float
    quote: str           # the filing-text row
    segment_name: str
    basis_months: int


def extract_segment_quarterly(
    filepath: str | Path,
    segment_names: list[str],
    *,
    period_token: str,  # source_document.period_token: Q1 / Q2 / Q3
) -> list[SegmentQuarterlyResult]:
    """Extract quarterly segment revenue from a 10-Q filing.

    Returns up to two results: the 3-month standalone value and the
    cumulative YTD value, both for the *current* year (second of the
    two year columns).
    """
    text = extract_text(Path(filepath))
    tables = _find_segment_tables(text, segment_names)
    for table_text in tables:
        result = _extract_from_table_text(table_text, segment_names, period_token)
        if result:
            return result
    return []


def _find_segment_tables(
    text: str, segment_names: list[str]
) -> list[str]:
    """Collect [TABLE]...[/TABLE] blocks that mention a segment + revenue."""
    tables = []
    for m in re.finditer(r"\[TABLE\](.*?)\[/TABLE\]", text, re.DOTALL):
        raw = m.group(1)
        if not any(seg in raw for seg in segment_names):
            continue
        if not re.search(r"(?i)net\s+sales|revenue", raw):
            continue
        if not PERIOD_HEADER_RE.search(raw):
            # Also accept the loose-spaced variant (AMZN style: "EndedSeptember")
            if not re.search(
                r"(?i)(three|six|nine)\s*months?\s*ended",
                raw,
            ):
                continue
        tables.append(raw)
    return tables


def _extract_from_table_text(
    raw: str, segment_names: list[str], period_token: str,
) -> list[SegmentQuarterlyResult]:
    """Look for a row like: 'AWS … Net sales $ A $ B $ C $ D' and pull B+D."""
    rows = raw.split("\n")

    # Detect whether the cumulative header is "Six" or "Nine" months.
    has_nine = bool(re.search(r"(?i)nine\s+months?\s+ended", raw))
    has_six = bool(re.search(r"(?i)six\s+months?\s+ended", raw))
    # Fall back to the period_token to infer: Q2 → H1 (6M), Q3 → 9M
    if has_nine:
        cumulative_period = "9M"
        cumulative_months = 9
    elif has_six:
        cumulative_period = "H1"
        cumulative_months = 6
    elif period_token == "Q3":
        cumulative_period = "9M"
        cumulative_months = 9
    elif period_token == "Q2":
        cumulative_period = "H1"
        cumulative_months = 6
    else:
        cumulative_period = ""
        cumulative_months = 0

    # Q1 10-Qs often have only 2 value columns (prior-3M, current-3M) —
    # no cumulative YTD. Detect that so we try the 2-value layout.
    is_two_value = (
        period_token == "Q1"
        and cumulative_period == ""
        and not has_nine and not has_six
    )

    # Locate the segment name and find its revenue row.
    # Two layouts supported:
    #   Layout A (AMZN): segment name is on its own line, then
    #     "Net sales $ v1 $ v2 [$ v3 $ v4]" is the NEXT line.
    #   Layout B (MSFT/GOOGL/ORCL): "<Segment> $ v1 $ v2 [$ v3 $ v4]" all
    #     on one line.
    for i, row in enumerate(rows):
        for seg in segment_names:
            if seg not in row:
                continue
            # Layout B: values on the same line as segment name.
            tail = row[row.index(seg) + len(seg):]
            m = VALUES_4_RE.search(tail) if not is_two_value else None
            if m:
                return _build_results(
                    m.groups(), seg, row, period_token,
                    cumulative_period, cumulative_months,
                )
            if is_two_value:
                m2p = VALUES_2_RE.search(tail)
                if m2p:
                    return _build_two(
                        m2p.groups(), seg, row, period_token,
                    )
            # Layout A: values on the Next line with 'Net sales' or
            # 'Revenue' at the start.
            for j in range(i + 1, min(i + 5, len(rows))):
                nxt = rows[j]
                if not re.search(r"(?i)^(net\s+sales|revenue)", nxt.strip()):
                    continue
                if not is_two_value:
                    m2 = VALUES_4_RE.search(nxt)
                    if m2:
                        return _build_results(
                            m2.groups(), seg, nxt.strip(), period_token,
                            cumulative_period, cumulative_months,
                        )
                else:
                    m2q = VALUES_2_RE.search(nxt)
                    if m2q:
                        return _build_two(
                            m2q.groups(), seg, nxt.strip(), period_token,
                        )
                break
    return []


def _build_two(
    values: tuple[str, str],
    segment: str,
    row_text: str,
    period_token: str,
) -> list[SegmentQuarterlyResult]:
    """Handle the 2-value layout (Q1 10-Q: prior-3M, current-3M)."""
    try:
        v1 = int(values[0].replace(",", ""))
        v2 = int(values[1].replace(",", ""))
    except ValueError:
        return []
    # Reasonableness: both should be >100 and same order of magnitude.
    if v1 < 100 or v2 < 100:
        return []
    if max(v1, v2) / max(1, min(v1, v2)) > 5:
        return []
    return [SegmentQuarterlyResult(
        period_type=period_token if period_token in ("Q1",) else "3M_reported",
        value_millions=float(v2),
        quote=row_text,
        segment_name=segment,
        basis_months=3,
    )]


def _build_results(
    values: tuple[str, str, str, str],
    segment: str,
    row_text: str,
    period_token: str,
    cumulative_period: str,
    cumulative_months: int,
) -> list[SegmentQuarterlyResult]:
    """Build output list from 4 extracted values.

    Columns layout (standard SEC 10-Q): prior-3M | current-3M | prior-YTD | current-YTD.
    We return the two CURRENT year values, each annotated with its period_type.
    """
    try:
        _prior_3m = int(values[0].replace(",", ""))
        v2 = int(values[1].replace(",", ""))
        _prior_ytd = int(values[2].replace(",", ""))
        v4 = int(values[3].replace(",", ""))
    except ValueError:
        return []

    # Sanity check: current_YTD (v4) must not be less than current_3M (v2).
    if v4 < v2:
        return []

    out: list[SegmentQuarterlyResult] = []

    # Current-year 3M — map to the filing's quarter index (Q1/Q2/Q3).
    out.append(SegmentQuarterlyResult(
        period_type=period_token if period_token in ("Q1", "Q2", "Q3") else "3M_reported",
        value_millions=float(v2),
        quote=row_text,
        segment_name=segment,
        basis_months=3,
    ))

    # Current-year cumulative YTD (H1 for Q2 10-Q, 9M for Q3 10-Q).
    if cumulative_period:
        out.append(SegmentQuarterlyResult(
            period_type=cumulative_period,
            value_millions=float(v4),
            quote=row_text,
            segment_name=segment,
            basis_months=cumulative_months,
        ))

    return out
