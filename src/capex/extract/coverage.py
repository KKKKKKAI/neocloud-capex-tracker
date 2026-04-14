"""Programmatic loader for coverage.yaml.

Provides structured lookups for per-company treatments, per-dataset
treatments, and the extraction chain (ordered list of extractors to
try for a given ticker + metric pair).

This is the bridge between the human-edited YAML configuration and
the extraction router.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COVERAGE_PATH = REPO_ROOT / "data" / "seeds" / "coverage.yaml"

_cache: dict | None = None


@dataclass
class CompanyTreatment:
    ticker: str
    full_name: str
    category: str  # hyperscaler, chinese_hyperscaler, neocloud_pureplay
    reporting_currency: str
    coverage_start: str
    filing_cadence: dict[str, str | None]  # {"annual": "10-K", "quarterly": "10-Q"}
    extraction_approach: str  # xbrl_primary, llm_primary
    notes: str = ""


@dataclass
class DatasetTreatment:
    ticker: str
    metric_key: str
    treatment: str  # named_segment, whole_company, derived_subtraction, named_segment_fuzzy
    segment_names: list[str] = field(default_factory=list)
    segment_start: str | None = None
    adjustment: dict | None = None
    extraction_method: str | None = None
    notes: str = ""


def _load_raw() -> dict:
    """Load and cache coverage.yaml."""
    global _cache
    if _cache is None:
        _cache = yaml.safe_load(COVERAGE_PATH.read_text(encoding="utf-8"))
    return _cache


def reload() -> None:
    """Force reload of coverage.yaml (useful after edits)."""
    global _cache
    _cache = None


def get_company_treatment(ticker: str) -> CompanyTreatment | None:
    """Look up a company's extraction treatment."""
    raw = _load_raw()
    companies = raw.get("companies", {})
    co = companies.get(ticker)
    if not co:
        return None
    return CompanyTreatment(
        ticker=ticker,
        full_name=co.get("full_name", ticker),
        category=co.get("category", "unknown"),
        reporting_currency=co.get("reporting_currency", "USD"),
        coverage_start=co.get("coverage_start", ""),
        filing_cadence=co.get("filing_cadence", {}),
        extraction_approach=co.get("extraction_approach", "xbrl_primary"),
        notes=co.get("notes", ""),
    )


def get_dataset_treatment(
    ticker: str, metric_key: str
) -> DatasetTreatment | None:
    """Look up a company's treatment for a specific dataset/metric.

    Searches all datasets in coverage.yaml for the one containing
    this metric_key, then finds the company's treatment within it.
    """
    raw = _load_raw()
    datasets = raw.get("datasets", {})

    for _ds_name, ds in datasets.items():
        metric_keys = ds.get("metric_keys", [])
        if metric_key not in metric_keys:
            continue

        included = ds.get("companies_included", [])
        for entry in included:
            # Simple string entry (no per-company treatment)
            if isinstance(entry, str):
                if entry == ticker:
                    return DatasetTreatment(
                        ticker=ticker,
                        metric_key=metric_key,
                        treatment="xbrl_default",
                    )
                continue

            # Dict entry with per-company treatment
            if isinstance(entry, dict) and entry.get("ticker") == ticker:
                return DatasetTreatment(
                    ticker=ticker,
                    metric_key=metric_key,
                    treatment=entry.get("treatment", "xbrl_default"),
                    segment_names=entry.get("segment_names", []),
                    segment_start=entry.get("segment_start"),
                    adjustment=entry.get("adjustment"),
                    extraction_method=entry.get("extraction_method"),
                    notes=entry.get("notes", ""),
                )

        # Check exclusions
        excluded = ds.get("companies_excluded", [])
        for exc in excluded:
            if isinstance(exc, dict) and exc.get("ticker") == ticker:
                return None  # explicitly excluded

    return None


def get_all_tickers() -> list[str]:
    """Return all tickers from coverage.yaml."""
    raw = _load_raw()
    return list(raw.get("companies", {}).keys())


def get_extraction_chain(ticker: str, metric_key: str) -> list[str]:
    """Return the ordered list of extractors to try for this (ticker, metric).

    The chain determines the fallback order. The router walks this list
    and stops at the first extractor that succeeds.

    Returns extractor names: "xbrl", "segment", "llm", "6k_press"
    """
    company = get_company_treatment(ticker)
    if not company:
        return []

    dataset = get_dataset_treatment(ticker, metric_key)
    approach = company.extraction_approach

    # Headline metrics (revenue, capex, OCF, D&A, PP&E)
    headline_metrics = {
        "capital_expenditures", "revenue", "operating_cash_flow",
        "depreciation_amortization", "property_plant_equipment_net",
    }

    if metric_key in headline_metrics:
        if approach == "xbrl_primary":
            return ["xbrl", "llm"]
        elif approach == "llm_primary":
            # 20-F filers: XBRL has annual data, try it first
            return ["xbrl", "llm"]
        return ["xbrl", "llm"]

    # cloud_segment_revenue — depends on treatment
    if metric_key == "cloud_segment_revenue" and dataset:
        treatment = dataset.treatment

        if treatment == "whole_company":
            # Total revenue = cloud revenue. Reuse headline revenue chain.
            return ["xbrl", "llm"]

        if treatment in ("named_segment", "named_segment_fuzzy"):
            if approach == "xbrl_primary":
                return ["segment", "llm"]
            else:
                # Chinese companies: segment extractor might work,
                # but LLM is more reliable for fuzzy segment names
                return ["llm"]

        if treatment == "derived_subtraction":
            # Derived values always need LLM judgment
            return ["llm"]

    # Unknown metric — try LLM
    return ["llm"]
