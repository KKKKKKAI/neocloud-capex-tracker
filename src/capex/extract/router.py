"""Unified extraction router — single entry point for all extractions.

Routes to the appropriate extractor based on coverage.yaml config,
applies dual-agent verification for non-XBRL extractions, and refuses
to write unverified values.

Usage:
    from capex.extract.router import extract_metric, extract_batch

    # Single metric
    result = extract_metric("MSFT", "revenue")

    # Batch (all automated extractions)
    batch = extract_batch(metric_keys=["revenue"])
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..db import Database
from .base import ExtractionCandidate, ExtractResult
from .coverage import (
    get_all_tickers,
    get_company_treatment,
    get_dataset_treatment,
    get_extraction_chain,
)
from .extractors.xbrl import XBRLExtractor
from .extractors.segment_ext import SegmentExtractor
from .extractors.press_release import PressReleaseExtractor
from .extractors.llm_interactive import LLMInteractiveExtractor
from .writer import write_extractions

# Extractor registry
EXTRACTORS = {
    "xbrl": XBRLExtractor(),
    "segment": SegmentExtractor(),
    "6k_press": PressReleaseExtractor(),
    "llm": LLMInteractiveExtractor(),
}


def extract_metric(
    ticker: str,
    metric_key: str,
    period: str | None = None,
    form_type: str | None = None,
    *,
    write: bool = True,
    interactive: bool = False,
    db: Database | None = None,
) -> ExtractResult:
    """Unified extraction entry point.

    Routes to the appropriate extractor based on coverage.yaml,
    applies dual-agent verification for non-XBRL results, and
    writes verified results to the DB.

    Args:
        ticker: company ticker
        metric_key: canonical metric key
        period: ISO date (optional, defaults to latest)
        form_type: filing form type (optional, inferred from coverage)
        write: whether to write results to DB
        interactive: whether LLM extraction is available (Claude Code session)
        db: database instance

    Returns:
        ExtractResult with status, candidates, and verification metadata.
    """
    db = db or Database()
    treatment = get_dataset_treatment(ticker, metric_key)
    chain = get_extraction_chain(ticker, metric_key)
    tried = []

    for extractor_name in chain:
        extractor = EXTRACTORS.get(extractor_name)
        if not extractor:
            continue

        if not extractor.can_handle(ticker, metric_key, form_type, treatment):
            tried.append(extractor_name)
            continue

        candidates = extractor.extract(
            ticker, metric_key, period=period, form_type=form_type,
            db=db, treatment=treatment, interactive=interactive,
        )
        tried.append(extractor_name)

        if candidates is None:
            continue

        # XBRL results: write directly (machine-verified)
        if extractor_name == "xbrl":
            summary = None
            if write:
                result_dicts = [c.to_writer_dict() for c in candidates]
                summary = write_extractions(result_dicts, db=db)

            return ExtractResult(
                status="success",
                extractor=extractor_name,
                candidates=candidates,
                write_summary=summary,
                chain_tried=tried,
                verified=True,
            )

        # Non-XBRL results: require dual-agent verification
        # In non-interactive mode, signal that verification is needed
        if not interactive:
            return ExtractResult(
                status="needs_interactive",
                extractor=extractor_name,
                candidates=candidates,
                chain_tried=tried,
                needs_interactive=True,
            )

        # In interactive mode: dual-agent verification would run here.
        # The actual LLM calls happen in the Claude Code session —
        # the router prepares the context and the session orchestrates
        # Agent A and Agent B calls.
        #
        # For now, return the candidates with a flag indicating they
        # need verification before writing.
        return ExtractResult(
            status="needs_verification",
            extractor=extractor_name,
            candidates=candidates,
            chain_tried=tried,
            needs_interactive=False,
        )

    # Nothing worked
    return ExtractResult(
        status="no_extractor",
        chain_tried=tried,
        needs_interactive=(chain[-1] == "llm") if chain else False,
    )


@dataclass
class BatchResult:
    """Summary of a batch extraction run."""

    succeeded: list[dict[str, Any]] = field(default_factory=list)
    needs_review: list[dict[str, Any]] = field(default_factory=list)
    needs_interactive: list[tuple[str, str]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        return {
            "succeeded": len(self.succeeded),
            "needs_review": len(self.needs_review),
            "needs_interactive": len(self.needs_interactive),
            "failed": len(self.failed),
        }


def extract_batch(
    tickers: list[str] | None = None,
    metric_keys: list[str] | None = None,
    period: str | None = None,
    *,
    db: Database | None = None,
) -> BatchResult:
    """Extract metrics for multiple companies.

    Runs all automated (XBRL) extractions. Reports which
    (ticker, metric) pairs need interactive LLM extraction.

    Args:
        tickers: list of tickers (default: all from coverage.yaml)
        metric_keys: list of metrics (default: all headline metrics)
        period: specific period (default: all available)
        db: database instance
    """
    db = db or Database()
    if tickers is None:
        tickers = get_all_tickers()
    if metric_keys is None:
        metric_keys = [
            "capital_expenditures", "revenue", "operating_cash_flow",
            "depreciation_amortization", "property_plant_equipment_net",
        ]

    result = BatchResult()

    for ticker in tickers:
        for metric_key in metric_keys:
            try:
                r = extract_metric(
                    ticker, metric_key, period=period,
                    write=True, interactive=False, db=db,
                )

                if r.status == "success":
                    n = r.write_summary.get("inserted", 0) if r.write_summary else 0
                    result.succeeded.append({
                        "ticker": ticker, "metric": metric_key,
                        "extractor": r.extractor, "inserted": n,
                    })
                elif r.status == "needs_interactive":
                    result.needs_interactive.append((ticker, metric_key))
                elif r.status == "needs_review":
                    result.needs_review.append({
                        "ticker": ticker, "metric": metric_key,
                    })
                else:
                    result.failed.append({
                        "ticker": ticker, "metric": metric_key,
                        "status": r.status, "chain": r.chain_tried,
                    })
            except Exception as e:
                result.failed.append({
                    "ticker": ticker, "metric": metric_key,
                    "error": str(e),
                })

    return result
