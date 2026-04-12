"""Source citation formatter for Excel cell comments.

Generates self-contained, human-readable provenance citations for every
data point in the workbook. An analyst can Shift+F2 any cell and see
exactly which filing, section, and line item the number came from —
plus a direct download URL to the source report.

IMPORTANT: citations NEVER reference our codebase (no Python files, no
YAML configs, no internal paths). They reference the company's public
filings only. For derived values, the reasoning is stated in full —
quoting the filing's footnotes and explaining the deduction logic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COVERAGE_PATH = REPO_ROOT / "data" / "seeds" / "coverage.yaml"

# Cache coverage.yaml adjustments
_coverage_cache: dict | None = None


def format_citation(
    extraction: dict[str, Any],
    source_doc: dict[str, Any],
    company: dict[str, Any],
) -> str:
    """Format a complete source citation for an Excel cell comment.

    Args:
        extraction: row from extractions table
        source_doc: row from source_documents table
        company: row from companies table

    Returns:
        Multi-line citation string ready for openpyxl Comment.
    """
    ticker = source_doc.get("ticker", "?")
    source = source_doc.get("source", "")
    ext_type = extraction.get("extraction_type", "direct")

    if ext_type == "derived":
        return _format_derived(extraction, source_doc, company)
    elif source == "hkex" or ticker == "0700":
        return _format_hkex(extraction, source_doc, company)
    else:
        return _format_sec(extraction, source_doc, company)


def _format_sec(
    ext: dict[str, Any],
    doc: dict[str, Any],
    co: dict[str, Any],
) -> str:
    """SEC filing citation (10-K, 10-Q, 20-F)."""
    ticker = doc.get("ticker", "?")
    form = doc.get("form_type", "?")
    period = doc.get("period_of_report", "?")
    filed = doc.get("filing_date", "?")
    section = ext.get("locator_section", "")
    quote = ext.get("quote", "")
    model = ext.get("extracting_model", "")
    value = ext.get("value")
    value_usd = ext.get("value_usd")
    currency = ext.get("reporting_currency", "USD")
    fx_rate = ext.get("fx_rate")
    source_url = doc.get("source_url", "")

    fy = _fiscal_year_label(period, form)

    lines = [f"Source: [{ticker}] {fy} {form} (filed {filed})"]

    if section:
        lines.append(f"Section: {section}")
    if quote and not quote.startswith("XBRL"):
        lines.append(f'Line item: "{quote}"')

    if currency != "USD" and value is not None and value_usd is not None:
        lines.append(f"Value: {currency} {value:,.0f}M → ${value_usd:,.0f}M USD")
        if fx_rate:
            fx_date = ext.get("fx_rate_date", period)
            lines.append(
                f"FX: {currency}/USD {fx_rate:.4f} @ {fx_date} "
                f"(source: ECB via frankfurter.app)"
            )
    elif value is not None:
        lines.append(f"Value: ${value:,.0f}M (as reported)")

    if "xbrl" in model:
        concept = _get_xbrl_concept(ext)
        if concept:
            lines.append(f"Method: XBRL companyfacts API ({concept})")
        else:
            lines.append(f"Method: {model}")
    else:
        lines.append(f"Method: {model}")

    # Download link after 2-line break
    if source_url and not source_url.startswith("xbrl://"):
        lines.append("")
        lines.append(f"Report: {source_url}")

    return "\n".join(lines)


def _format_hkex(
    ext: dict[str, Any],
    doc: dict[str, Any],
    co: dict[str, Any],
) -> str:
    """HKEX annual/interim report citation."""
    ticker = doc.get("ticker", "?")
    form = doc.get("form_type", "HK-AR")
    period = doc.get("period_of_report", "?")
    filed = doc.get("filing_date", "?")
    section = ext.get("locator_section", "")
    quote = ext.get("quote", "")
    model = ext.get("extracting_model", "")
    value = ext.get("value")
    value_usd = ext.get("value_usd")
    fx_rate = ext.get("fx_rate")
    source_url = doc.get("source_url", "")

    form_label = "Annual Report" if "AR" in form else "Interim Report"
    fy = period[:4]

    lines = [
        f"Source: [{ticker}] FY{fy} {form_label} "
        f"(HKEXnews{', filed ' + filed if filed else ''})"
    ]

    if section:
        lines.append(f"Section: {section}")
    if quote:
        lines.append(f'Line item: "{quote}"')

    if value is not None and value_usd is not None:
        lines.append(f"Value: RMB {value:,.0f}M → ${value_usd:,.0f}M USD")
        if fx_rate:
            fx_date = ext.get("fx_rate_date", period)
            lines.append(
                f"FX: CNY/USD {fx_rate:.4f} @ {fx_date} "
                f"(source: ECB via frankfurter.app)"
            )

    # Tencent proxy warning
    if ticker == "0700" and ext.get("metric_key") == "cloud_segment_revenue":
        lines.append("")
        lines.append(
            "IMPORTANT: This is a PROXY, not pure cloud revenue. "
            "The \"FinTech and Business Services\" segment includes "
            "WeChat Pay payment processing (~60-70% of segment) and "
            "enterprise SaaS alongside cloud services. Tencent does "
            "not separately disclose cloud revenue."
        )

    lines.append(f"Method: {model}")

    if source_url and not source_url.startswith("xbrl://"):
        lines.append("")
        lines.append(f"Report: {source_url}")

    return "\n".join(lines)


def _format_derived(
    ext: dict[str, Any],
    doc: dict[str, Any],
    co: dict[str, Any],
) -> str:
    """Derived value citation with full reasoning."""
    ticker = doc.get("ticker", "?")
    form = doc.get("form_type", "?")
    period = doc.get("period_of_report", "?")
    filed = doc.get("filing_date", "?")
    quote = ext.get("quote", "")
    value = ext.get("value")
    value_usd = ext.get("value_usd")
    fx_rate = ext.get("fx_rate")
    currency = ext.get("reporting_currency", "USD")
    source_url = doc.get("source_url", "")

    fy = _fiscal_year_label(period, form)
    form_label = "20-F" if "20-F" in form else form

    lines = [f"Source: [{ticker}] {fy} {form_label} (filed {filed})"]
    lines.append("Derivation:")

    # Get the adjustment reasoning from coverage.yaml
    adjustment = _get_adjustment(ticker)

    if adjustment and ticker == "BIDU":
        lines.append(
            '  Baidu reports revenue as "Online marketing services" '
            'and "Others".'
        )
        lines.append(
            '  Per 20-F footnote (i): "Others mainly include revenue '
            'from cloud services and iQIYI\'s video membership services".'
        )
        lines.append(
            "  iQIYI (NASDAQ: IQ) is a separately listed subsidiary "
            "whose standalone revenue is disclosed in Baidu's segment table."
        )
        if quote and "-" in quote:
            lines.append(f"  Calculation: {quote}")
        lines.append(
            "  Note: overstates cloud slightly — \"Others\" includes "
            "non-cloud, non-iQIYI items."
        )
    elif adjustment:
        rationale = adjustment.get("rationale", "")
        formula = adjustment.get("formula", "")
        if formula:
            lines.append(f"  Formula: {formula}")
        if rationale:
            for rline in rationale.strip().split("\n"):
                lines.append(f"  {rline.strip()}")
    else:
        if quote:
            lines.append(f"  {quote}")

    if currency != "USD" and value is not None and value_usd is not None:
        lines.append(f"Value: {currency} {value:,.0f}M → ${value_usd:,.0f}M USD")
        if fx_rate:
            fx_date = ext.get("fx_rate_date", period)
            lines.append(
                f"FX: {currency}/USD {fx_rate:.4f} @ {fx_date} "
                f"(source: ECB via frankfurter.app)"
            )

    if source_url and not source_url.startswith("xbrl://"):
        lines.append("")
        lines.append(f"Report: {source_url}")

    return "\n".join(lines)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _fiscal_year_label(period: str, form: str) -> str:
    """Derive a human-readable fiscal year label."""
    if not period:
        return "?"
    year = period[:4]
    month = period[5:7] if len(period) >= 7 else "12"
    if form in ("10-Q",) and month in ("03", "06", "09"):
        q_map = {"03": "Q1", "06": "Q2", "09": "Q3"}
        return f"FY{year} {q_map.get(month, '')}"
    return f"FY{year}"


def _get_xbrl_concept(ext: dict[str, Any]) -> str | None:
    """Try to find the XBRL concept from the quote field."""
    quote = ext.get("quote", "")
    if quote and quote.startswith("XBRL:"):
        return quote.split("XBRL:")[1].strip().split(" ")[0]
    return None


def _get_adjustment(ticker: str) -> dict | None:
    """Look up the adjustment config for a ticker from coverage.yaml."""
    global _coverage_cache
    if _coverage_cache is None:
        if COVERAGE_PATH.exists():
            _coverage_cache = yaml.safe_load(
                COVERAGE_PATH.read_text(encoding="utf-8")
            )
        else:
            _coverage_cache = {}

    datasets = _coverage_cache.get("datasets", {})
    cloud = datasets.get("cloud_segment_revenue", {})
    included = cloud.get("companies_included", [])
    for entry in included:
        if isinstance(entry, dict) and entry.get("ticker") == ticker:
            return entry.get("adjustment")
    return None
