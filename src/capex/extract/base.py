"""Extractor protocol — common interface for all extraction backends.

Every extractor (XBRL, segment regex, 6-K press release, LLM) implements
this protocol. The router walks the extraction chain and calls each
extractor in order until one succeeds.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .coverage import DatasetTreatment


@runtime_checkable
class Extractor(Protocol):
    """Protocol that all extraction backends must implement."""

    name: str

    def can_handle(
        self,
        ticker: str,
        metric_key: str,
        form_type: str | None,
        treatment: DatasetTreatment | None,
    ) -> bool:
        """Quick pre-flight: can this extractor handle this request?"""
        ...

    def extract(
        self,
        ticker: str,
        metric_key: str,
        period: str | None = None,
        form_type: str | None = None,
        **kwargs: Any,
    ) -> list[ExtractionCandidate] | None:
        """Attempt extraction. Returns candidates or None on failure.

        None means "I can't handle this, try the next extractor."
        An empty list means "I tried but found nothing" (still a valid result).
        """
        ...


@dataclass
class ExtractionCandidate:
    """A single extracted data point, ready for verification and writing.

    This is the intermediate representation between an extractor and
    the writer. For XBRL extractions, these are written directly.
    For LLM extractions, these go through dual-agent verification first.
    """
    source_document_id: int
    metric_key: str
    value: float | None
    value_text: str
    unit: str = "USD_millions"
    quote: str = ""
    locator_section: str = ""
    locator_page: int | None = None
    extraction_type: str = "direct"  # direct, inferred, derived
    extracting_model: str = ""
    reporting_currency: str = "USD"

    # Dual-agent verification fields (populated after verification)
    excerpts: list[dict[str, str]] = field(default_factory=list)
    reasoning: str = ""
    derivation: str | None = None

    # Period classification (used by chart / audit selectors)
    period_type: str = ""              # "FY", "Q1", "Q2", "Q3", "Q4", "H1", "9M"
    basis_period_months: int | None = None

    def to_writer_dict(self) -> dict[str, Any]:
        """Convert to the dict format expected by writer.write_extractions().

        `excerpts` and `reasoning` ride along so the writer can persist
        them to extraction_evidence (read by exporters/citations.py to
        produce the Quote: line in Excel cell comments).
        """
        return {
            "source_document_id": self.source_document_id,
            "metric_key": self.metric_key,
            "value": self.value,
            "value_text": self.value_text,
            "unit": self.unit,
            "quote": self.quote,
            "locator_section": self.locator_section,
            "locator_page": self.locator_page,
            "extraction_type": self.extraction_type,
            "extracting_model": self.extracting_model,
            "reporting_currency": self.reporting_currency,
            "period_type": self.period_type,
            "basis_period_months": self.basis_period_months,
            "excerpts": self.excerpts,
            "reasoning": self.reasoning,
        }


@dataclass
class ExtractResult:
    """Result from the extraction router."""

    status: str  # "success", "needs_review", "needs_interactive", "no_extractor"
    extractor: str | None = None  # which extractor produced the result
    candidates: list[ExtractionCandidate] = field(default_factory=list)
    write_summary: dict[str, Any] | None = None
    chain_tried: list[str] = field(default_factory=list)
    needs_interactive: bool = False

    # Dual-agent verification metadata
    verified: bool = False
    verification_details: dict[str, Any] | None = None
    review_queue: list[dict[str, Any]] = field(default_factory=list)
