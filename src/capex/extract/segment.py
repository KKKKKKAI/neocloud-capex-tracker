"""Segment revenue extraction from SEC filings.

Generalizable approach for extracting cloud/datacenter segment revenue
from any company's 10-K or 20-F filing. Works across different HTML
formats and filing vintages by:

1. Parsing ALL tables from the filing text
2. Scoring each table for relevance to segment revenue
3. Extracting the target segment's revenue from the best-matching table
4. Returning multi-year comparative data when available

The segment name mapping comes from data/seeds/coverage.yaml — each
company has a list of historical segment names (since they change over
time).

Usage:
    from capex.extract.segment import extract_segment_revenue

    results = extract_segment_revenue(
        filepath="data/_sources/AMZN/_raw/amzn-20251231.htm",
        ticker="AMZN",
        segment_names=["AWS", "Amazon Web Services"],
    )
    # → [{"period": "2025-12-31", "value": 128725}, ...]
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..read.text import extract_text


def extract_segment_revenue(
    filepath: str | Path,
    ticker: str,
    segment_names: list[str],
    form_type: str = "10-K",
) -> list[dict[str, Any]]:
    """Extract segment revenue from a filing.

    Args:
        filepath: path to the filing (HTML or PDF)
        ticker: company ticker
        segment_names: list of possible segment names to search for
            (e.g. ["Intelligent Cloud", "Commercial Cloud"] for MSFT)
        form_type: filing type (affects year header parsing)

    Returns:
        List of dicts: [{"period": "2025-12-31", "value": 128725,
                         "segment_name": "AWS", "table_context": "..."}]
    """
    text = extract_text(Path(filepath))
    tables = _parse_all_tables(text)

    # Score each table for relevance
    scored = []
    for table in tables:
        score, matched_segment = _score_table(table, segment_names)
        if score > 0:
            scored.append((score, table, matched_segment))

    scored.sort(key=lambda x: -x[0])

    if not scored:
        return []

    # Extract from the best-matching table
    best_score, best_table, matched_segment = scored[0]
    return _extract_from_table(best_table, matched_segment, ticker)


def _parse_all_tables(text: str) -> list[dict[str, Any]]:
    """Parse all [TABLE]...[/TABLE] blocks into structured dicts."""
    tables = []
    for m in re.finditer(r"\[TABLE\](.*?)\[/TABLE\]", text, re.DOTALL):
        raw = m.group(1).strip()
        rows = [line.strip() for line in raw.split("\n") if line.strip()]
        tables.append({
            "raw": raw,
            "rows": rows,
            "position": m.start(),
        })
    return tables


def _score_table(
    table: dict, segment_names: list[str]
) -> tuple[float, str | None]:
    """Score how likely a table contains segment revenue data.

    Returns (score, matched_segment_name). Higher = better match.
    """
    raw = table["raw"]
    score = 0.0
    matched = None

    # Must contain at least one segment name
    for seg in segment_names:
        if seg in raw:
            score += 10
            matched = seg
            break

    if score == 0:
        return 0, None

    # Bonus for revenue indicators
    if re.search(r"(?i)net\s+sales|revenue|total\s+revenues", raw):
        score += 5

    # Bonus for having year headers (YYYY format)
    year_matches = re.findall(r"20[12]\d", raw)
    if len(set(year_matches)) >= 2:
        score += 3

    # Bonus for having dollar amounts in the right range ($1B+)
    big_numbers = re.findall(r"[\d,]{4,}", raw)
    if big_numbers:
        largest = max(int(n.replace(",", "")) for n in big_numbers)
        if largest > 1000:  # > $1B in millions
            score += 3

    # Penalty for non-revenue tables
    if re.search(r"(?i)square\s+foot|headcount|employees|shares", raw):
        score -= 20
    if re.search(r"(?i)goodwill|intangible|restructur", raw):
        score -= 10

    # Bonus for segment-style tables (multiple segments listed)
    segment_indicators = [
        "North America", "International", "Total",
        "Productivity", "Personal Computing",
        "Google Services", "Other Bets",
    ]
    si_count = sum(1 for si in segment_indicators if si in raw)
    score += si_count * 2

    return score, matched


def _extract_from_table(
    table: dict, segment_name: str, ticker: str
) -> list[dict[str, Any]]:
    """Extract revenue values for the target segment from a table.

    Handles multiple formats:
    - "AWS  $90,757  $107,556  $128,725" (values on same line)
    - Multi-line format where segment name and values are on
      separate rows (with a "Revenue" or "Net sales" label between)
    """
    raw = table["raw"]
    rows = table["rows"]

    # Find years from the table header
    years = _extract_years(raw)

    # Strategy 1: values on the same line as segment name
    results = _extract_same_line(rows, segment_name, years)
    if results:
        return results

    # Strategy 2: segment name as a section header, with "Revenue"
    # or "Net sales" line below containing the values
    results = _extract_section_style(rows, segment_name, years)
    if results:
        return results

    # Strategy 3: broad search — find the segment name, then find
    # the nearest line with dollar values
    results = _extract_nearest_values(rows, segment_name, years)
    return results


def _extract_years(raw: str) -> list[int]:
    """Extract fiscal years from the table header."""
    # Look for "Year Ended December 31, YYYY YYYY YYYY" or similar
    year_line = re.search(
        r"(?:Year\s+Ended|Fiscal\s+Year|For\s+the\s+year)[^\n]*"
        r"((?:20[12]\d[\s,]*)+)",
        raw,
        re.IGNORECASE,
    )
    if year_line:
        return [int(y) for y in re.findall(r"20[12]\d", year_line.group(0))]

    # Fallback: find all 4-digit years in the first few lines
    header = "\n".join(raw.split("\n")[:5])
    found = re.findall(r"20[12]\d", header)
    return [int(y) for y in dict.fromkeys(found)]  # dedupe, preserve order


def _extract_same_line(
    rows: list[str], segment_name: str, years: list[int]
) -> list[dict]:
    """Extract values from a line like: "AWS  $90,757  $107,556  $128,725"."""
    for row in rows:
        if segment_name in row:
            # Extract all dollar amounts from this line
            amounts = re.findall(r"\$?\s*([\d,]+)", row)
            # Filter to reasonable revenue values (> $100M = 100 in millions)
            values = []
            for a in amounts:
                v = int(a.replace(",", ""))
                if v > 100:  # > $100M
                    values.append(v)

            if values and years and len(values) <= len(years):
                return [
                    {
                        "period_year": years[i],
                        "value": values[i],
                        "segment_name": segment_name,
                    }
                    for i in range(len(values))
                ]
    return []


def _extract_section_style(
    rows: list[str], segment_name: str, years: list[int]
) -> list[dict]:
    """Extract from format where segment is a header and Revenue is below."""
    for i, row in enumerate(rows):
        if segment_name in row:
            # Look at the next few rows for "Revenue" or "Net sales"
            for j in range(i + 1, min(i + 5, len(rows))):
                if re.search(r"(?i)^revenue|^net\s+sales", rows[j].strip()):
                    amounts = re.findall(r"\$?\s*([\d,]+)", rows[j])
                    values = [
                        int(a.replace(",", ""))
                        for a in amounts
                        if int(a.replace(",", "")) > 100
                    ]
                    if values and years and len(values) <= len(years):
                        return [
                            {
                                "period_year": years[k],
                                "value": values[k],
                                "segment_name": segment_name,
                            }
                            for k in range(len(values))
                        ]
    return []


def _extract_nearest_values(
    rows: list[str], segment_name: str, years: list[int]
) -> list[dict]:
    """Fallback: find segment name, then nearest row with values."""
    for i, row in enumerate(rows):
        if segment_name in row:
            # Check this row and the next 3 rows for values
            for j in range(i, min(i + 4, len(rows))):
                amounts = re.findall(r"\$?\s*([\d,]+)", rows[j])
                values = [
                    int(a.replace(",", ""))
                    for a in amounts
                    if int(a.replace(",", "")) > 100
                ]
                if len(values) >= 2:
                    if years and len(values) <= len(years):
                        return [
                            {
                                "period_year": years[k],
                                "value": values[k],
                                "segment_name": segment_name,
                            }
                            for k in range(len(values))
                        ]
    return []
