"""Parse filing text into a section tree.

Given the plain text output of text.py, identify section boundaries
(SEC Items, HKEX sections) and return a dict mapping section names to
their text content. The extractor uses this to select only the relevant
sections for the LLM context.

SEC 10-K / 10-Q / 20-F section structure:
    Item 1    — Business
    Item 1A   — Risk Factors
    Item 1B   — Unresolved Staff Comments
    Item 1C   — Cybersecurity
    Item 2    — Properties
    Item 3    — Legal Proceedings
    Item 4    — Mine Safety
    Item 5    — Market for Common Equity
    Item 6    — [Reserved]
    Item 7    — Management's Discussion and Analysis (MD&A)  ← WE WANT THIS
    Item 7A   — Quantitative and Qualitative Disclosures About Market Risk
    Item 8    — Financial Statements and Supplementary Data  ← WE WANT THIS
    Item 9    — Changes in and Disagreements
    Item 9A   — Controls and Procedures
    ...

We extract Items 7, 8 (which includes Notes to Financial Statements),
and any contractual-obligations / purchase-commitments sections we find.

For HKEX PDFs, section headings are different — we look for:
    - "Management Discussion" / "MD&A"
    - "Consolidated Statement of Cash Flows"
    - "Notes to" (the financial statements)
"""
from __future__ import annotations

import re

# Sections we care about for capex extraction, in priority order
EXTRACTION_SECTIONS_SEC = [
    "Item 7",   # MD&A
    "Item 8",   # Financial Statements (includes Notes)
]

EXTRACTION_SECTIONS_HKEX = [
    "Management Discussion",
    "Cash Flow",
    "Notes to",
]


def parse_sections(text: str, form_type: str) -> dict[str, str]:
    """Parse the full filing text into named sections.

    Args:
        text: plain text from text.py
        form_type: '10-K', '10-Q', '20-F', 'HK-AR', 'HK-IR'

    Returns:
        Dict mapping section name → section text content.
        Includes a special '_full' key with the complete text.
        Section names are normalized: "Item 7", "Item 8", etc.
    """
    sections: dict[str, str] = {"_full": text}

    if form_type in ("10-K", "10-Q", "20-F"):
        sections.update(_parse_sec_sections(text))
    elif form_type in ("HK-AR", "HK-IR"):
        sections.update(_parse_hkex_sections(text))

    return sections


def get_extraction_sections(
    sections: dict[str, str],
    form_type: str,
) -> dict[str, str]:
    """Return only the sections relevant for metric extraction.

    This is the subset the extractor sends to the LLM — typically
    Items 7 + 8 for SEC filings, or their HKEX equivalents.
    """
    if form_type in ("10-K", "10-Q", "20-F"):
        target_prefixes = EXTRACTION_SECTIONS_SEC
    elif form_type in ("HK-AR", "HK-IR"):
        target_prefixes = EXTRACTION_SECTIONS_HKEX
    else:
        return {"_full": sections.get("_full", "")}

    result: dict[str, str] = {}
    for section_name, content in sections.items():
        if section_name.startswith("_"):
            continue
        for prefix in target_prefixes:
            if section_name.lower().startswith(prefix.lower()):
                result[section_name] = content
                break

    # If we found nothing, fall back to the full text (truncated)
    if not result:
        full = sections.get("_full", "")
        # Take a reasonable chunk that won't blow context
        result["_full (truncated)"] = full[:500_000]

    return result


def estimate_tokens(sections: dict[str, str]) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    total_chars = sum(len(v) for v in sections.values())
    return total_chars // 4


# ----------------------------------------------------------------------------
# SEC section parser
# ----------------------------------------------------------------------------

# Pattern: "Item N" or "Item NA" at the start of a line, possibly with
# trailing period, dash, or colon. Case-insensitive.
_SEC_ITEM_PATTERN = re.compile(
    r"^\s*(Item\s+(\d+[A-Za-z]?))\s*[\.\:\—\-–]?\s*(.*)",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_sec_sections(text: str) -> dict[str, str]:
    """Split SEC filing text by Item headings.

    SEC filings repeat "Item N" headers as running headers within each
    section (e.g. every subsection of Item 7 starts with "Item 7").
    We handle this by only creating a section boundary when the Item
    NUMBER changes, and by skipping the Table of Contents (typically
    the first ~15% of the text).
    """
    matches = list(_SEC_ITEM_PATTERN.finditer(text))
    if not matches:
        return {}

    # Skip TOC: find where the actual body starts. The body usually
    # begins at or after "PART I" appearing as a standalone heading.
    # Use a heuristic: skip the first 10% of the text as likely TOC.
    body_start = len(text) // 10
    body_matches = [m for m in matches if m.start() >= body_start]
    if not body_matches:
        body_matches = matches

    # Walk matches in position order. Only break when the Item number
    # CHANGES — repeated "Item 7" headers within Item 7 are subsections,
    # not new sections.
    boundaries: list[tuple[str, int]] = []  # (normalized_name, position)
    prev_num = None

    for match in body_matches:
        item_num = match.group(2).upper()
        normalized = f"Item {item_num}"
        if item_num != prev_num:
            boundaries.append((normalized, match.start()))
            prev_num = item_num

    # Extract section content between boundaries
    sections: dict[str, str] = {}
    for i, (name, start) in enumerate(boundaries):
        end = boundaries[i + 1][1] if i + 1 < len(boundaries) else len(text)
        content = text[start:end].strip()
        sections[name] = content

    return sections


# ----------------------------------------------------------------------------
# HKEX section parser
# ----------------------------------------------------------------------------

_HKEX_SECTION_PATTERNS = [
    (r"management\s+discussion\s+and\s+analysis", "Management Discussion and Analysis"),
    (r"management\s+discussion", "Management Discussion"),
    (r"md&a", "Management Discussion"),
    (r"consolidated\s+statement\s+of\s+cash\s+flows?", "Consolidated Statement of Cash Flows"),
    (r"consolidated\s+cash\s+flow", "Consolidated Cash Flow Statement"),
    (
        r"notes?\s+to\s+(?:the\s+)?(?:consolidated\s+)?financial\s+statements?",
        "Notes to Financial Statements",
    ),
    (r"notes?\s+to\s+(?:the\s+)?(?:consolidated\s+)?accounts?", "Notes to Accounts"),
    (r"financial\s+review", "Financial Review"),
    (r"report\s+of\s+the\s+directors", "Report of the Directors"),
]


def _parse_hkex_sections(text: str) -> dict[str, str]:
    """Split HKEX filing text by common section headings.

    HKEX PDFs are less structured than SEC HTML — sections are
    identified by heading text patterns rather than numbered Items.
    """
    # Find all section heading positions
    heading_positions: list[tuple[int, str]] = []

    for pattern, label in _HKEX_SECTION_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            # Only count headings that appear at or near the start of a line
            line_start = text.rfind("\n", 0, match.start())
            prefix = text[line_start + 1 : match.start()].strip()
            if len(prefix) < 20:  # heading is near the start of a line
                heading_positions.append((match.start(), label))

    if not heading_positions:
        return {}

    # Sort by position and deduplicate by label (keep last/longest)
    heading_positions.sort(key=lambda x: x[0])

    sections: dict[str, str] = {}
    for i, (pos, label) in enumerate(heading_positions):
        end = heading_positions[i + 1][0] if i + 1 < len(heading_positions) else len(text)
        content = text[pos:end].strip()
        if len(content) > 500 or label not in sections:
            sections[label] = content

    return sections
