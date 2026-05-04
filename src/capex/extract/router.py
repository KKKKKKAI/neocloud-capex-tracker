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
from .base import ExtractResult
from .coverage import (
    get_all_tickers,
    get_dataset_treatment,
    get_extraction_chain,
)
from .extractors.llm_interactive import LLMInteractiveExtractor
from .extractors.press_release import PressReleaseExtractor
from .extractors.segment_ext import SegmentExtractor
from .extractors.xbrl import XBRLExtractor
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
    backend: Any | None = None,
    db: Database | None = None,
    force: bool = False,
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

        # If we have a CLI backend and this is the LLM extractor,
        # use the headless extractor instead of the interactive one.
        # The headless path performs Agent A → Agent B → compare
        # internally, so its returned candidates are already
        # dual-agent verified and safe to write without an
        # interactive Claude Code session.
        used_headless = False
        if extractor_name == "llm" and backend is not None:
            from .extractors.llm_headless import LLMHeadlessExtractor
            extractor = LLMHeadlessExtractor()
            used_headless = True

        candidates = extractor.extract(
            ticker, metric_key, period=period, form_type=form_type,
            db=db, treatment=treatment, interactive=interactive,
            backend=backend,
        )
        tried.append(extractor_name)

        if candidates is None:
            continue

        # XBRL results and headless dual-agent results both arrive
        # pre-verified — write them directly.
        if extractor_name == "xbrl" or used_headless:
            summary = None
            if write:
                result_dicts = [c.to_writer_dict() for c in candidates]
                summary = write_extractions(result_dicts, db=db, force=force)

            return ExtractResult(
                status="success",
                extractor="llm-headless" if used_headless else extractor_name,
                candidates=candidates,
                write_summary=summary,
                chain_tried=tried,
                verified=True,
            )

        # Non-XBRL results without a backend: signal that an
        # interactive Claude Code session is required to verify.
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
    backend: Any | None = None,
    db: Database | None = None,
    force: bool = False,
) -> BatchResult:
    """Extract metrics for multiple companies.

    With a backend: runs full LLM dual-agent extraction for all metrics.
    Without a backend: runs XBRL-only, reports LLM items as needs_interactive.

    Args:
        tickers: list of tickers (default: all from coverage.yaml)
        metric_keys: list of metrics (default: all headline metrics)
        period: specific period (default: all available)
        backend: CLI backend for LLM calls (None = XBRL only)
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
                    write=True, interactive=False,
                    backend=backend, db=db, force=force,
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


def extract_filing(
    ticker: str,
    form_type: str,
    period: str,
    metric_keys: list[str] | None = None,
    *,
    write: bool = True,
    backend: Any | None = None,
    db: Database | None = None,
    force: bool = False,
) -> dict[str, ExtractResult]:
    """Bulk per-filing extraction with one Agent A call covering all metrics.

    Flow:
      1. For each metric_key, walk the chain; XBRL successes are kept.
      2. For metrics that XBRL did NOT satisfy AND have 'llm' in their
         chain, run ONE multi-metric Agent A pass via
         LLMHeadlessFilingExtractor, then per-metric Agent B.
      3. Any metric the multi-metric pass returned None for falls back
         to the per-metric extract_metric() path (which retains the
         3-attempt context-broadening loop).

    Returns a dict {metric_key: ExtractResult} with one entry per
    requested metric. The watcher can iterate this exactly like the
    per-metric loop it replaces.
    """
    db = db or Database()
    if metric_keys is None:
        metric_keys = [
            "capital_expenditures", "revenue", "operating_cash_flow",
            "depreciation_amortization", "property_plant_equipment_net",
            "cloud_segment_revenue",
        ]

    out: dict[str, ExtractResult] = {}
    pending_llm: list[str] = []

    # Phase 1: try XBRL per metric. Anything that succeeds is done.
    for mk in metric_keys:
        chain = get_extraction_chain(ticker, mk)
        if not chain:
            out[mk] = ExtractResult(status="no_extractor", chain_tried=[])
            continue
        if "xbrl" not in chain:
            # Pure-LLM metric (e.g. derived cloud_segment_revenue) —
            # straight to the multi-metric pass.
            if "llm" in chain:
                pending_llm.append(mk)
            else:
                out[mk] = ExtractResult(
                    status="no_extractor", chain_tried=chain,
                )
            continue

        # Try XBRL only — if it doesn't fire, queue for the LLM pass
        # rather than letting extract_metric escalate to LLM per metric.
        xbrl_result = _try_xbrl_only(
            ticker, mk, period=period, form_type=form_type,
            write=write, backend=backend, db=db, force=force,
        )
        if xbrl_result is not None:
            out[mk] = xbrl_result
        elif "llm" in chain:
            pending_llm.append(mk)
        else:
            out[mk] = ExtractResult(status="no_extractor", chain_tried=chain)

    # Phase 2: one multi-metric Agent A pass for everything pending,
    # provided we have a backend.
    if pending_llm and backend is not None:
        from .extractors.llm_headless_filing import LLMHeadlessFilingExtractor
        extractor = LLMHeadlessFilingExtractor()
        try:
            multi = extractor.extract_filing(
                ticker, form_type, period, pending_llm,
                backend=backend, db=db,
            )
        except Exception:
            multi = {k: None for k in pending_llm}

        for mk in pending_llm:
            cands = multi.get(mk)
            if cands is None:
                # Fallback: per-metric path.
                out[mk] = extract_metric(
                    ticker, mk, period=period, form_type=form_type,
                    write=write, backend=backend, db=db, force=force,
                )
                continue

            summary = None
            if write and cands:
                result_dicts = [c.to_writer_dict() for c in cands]
                summary = write_extractions(result_dicts, db=db, force=force)

            out[mk] = ExtractResult(
                status="success",
                extractor="llm-filing",
                candidates=cands,
                write_summary=summary,
                chain_tried=get_extraction_chain(ticker, mk),
                verified=True,
            )
    elif pending_llm:
        # No backend → can't run LLM. Fall back to extract_metric for
        # each so the existing needs_interactive signaling still works.
        for mk in pending_llm:
            out[mk] = extract_metric(
                ticker, mk, period=period, form_type=form_type,
                write=write, backend=backend, db=db, force=force,
            )

    return out


def _try_xbrl_only(
    ticker: str,
    metric_key: str,
    *,
    period: str | None,
    form_type: str | None,
    write: bool,
    backend: Any | None,
    db: Database,
    force: bool,
) -> ExtractResult | None:
    """Run only the XBRL extractor. Returns ExtractResult if XBRL
    produced candidates (success), else None so the caller can route
    the metric to the multi-metric LLM pass."""
    extractor = EXTRACTORS.get("xbrl")
    if extractor is None:
        return None
    treatment = get_dataset_treatment(ticker, metric_key)
    if not extractor.can_handle(ticker, metric_key, form_type, treatment):
        return None
    candidates = extractor.extract(
        ticker, metric_key, period=period, form_type=form_type,
        db=db, treatment=treatment, interactive=False, backend=backend,
    )
    if candidates is None:
        return None
    summary = None
    if write:
        result_dicts = [c.to_writer_dict() for c in candidates]
        summary = write_extractions(result_dicts, db=db, force=force)
    return ExtractResult(
        status="success",
        extractor="xbrl",
        candidates=candidates,
        write_summary=summary,
        chain_tried=["xbrl"],
        verified=True,
    )
