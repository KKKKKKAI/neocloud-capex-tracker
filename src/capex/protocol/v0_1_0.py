"""Interchange protocol v0.1.0 — typed contracts for extraction results.

Uses stdlib dataclasses (no Pydantic dependency in v1). When Phase 3.5
adds API adapters with structured output schemas, Pydantic models can
be derived from these dataclasses or replace them — the writer.py layer
only cares about the field names, not the validation framework.

Key types:
    ExtractionResult — one extracted metric from one filing.
    ExtractionBatch  — a batch of results from one extraction run.

The validate() function checks structural correctness before DB write.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

PROTOCOL_VERSION = "0.1.0-draft"

VALID_EXTRACTION_TYPES = ("direct", "inferred", "derived")
VALID_UNITS = ("USD_millions", "USD_thousands", "USD_actual", "ratio", "count", "percent")


@dataclass
class ExtractionResult:
    """One extracted metric from one filing."""

    source_document_id: int
    metric_key: str
    value: float | None  # None means "not disclosed"
    value_text: str  # raw text as it appears, e.g. "$88.0 billion"
    unit: str
    quote: str  # verbatim ≤30 words, ctrl-F-able
    locator_section: str  # e.g. "Item 8 - Consolidated Statements of Cash Flows"
    locator_page: int | None = None  # nullable, only for paginated PDFs
    extraction_type: str = "direct"  # direct | inferred | derived
    confidence: float | None = None
    extracting_model: str = "claude-code"
    protocol_version: str = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExtractionBatch:
    """A batch of results from one extraction run."""

    ticker: str
    form_type: str
    source_document_id: int
    results: list[ExtractionResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    extracting_model: str = "claude-code"

    @property
    def extracted_count(self) -> int:
        return len(self.results)

    @property
    def error_count(self) -> int:
        return len(self.errors)


def validate_result(result: dict[str, Any]) -> list[str]:
    """Validate an extraction result dict. Returns a list of error strings (empty = valid)."""
    errors = []

    required = ("source_document_id", "metric_key", "value_text", "unit", "quote", "locator_section")
    for f in required:
        if f not in result or result[f] is None or (isinstance(result[f], str) and not result[f].strip()):
            errors.append(f"missing or empty required field: {f}")

    if "extraction_type" in result and result["extraction_type"] not in VALID_EXTRACTION_TYPES:
        errors.append(
            f"invalid extraction_type: {result['extraction_type']!r} "
            f"(valid: {VALID_EXTRACTION_TYPES})"
        )

    if "unit" in result and result["unit"] not in VALID_UNITS:
        errors.append(
            f"invalid unit: {result['unit']!r} (valid: {VALID_UNITS})"
        )

    # Quote length check: ≤30 words
    quote = result.get("quote", "")
    if isinstance(quote, str) and len(quote.split()) > 40:  # generous 40 to allow some slack
        errors.append(f"quote too long: {len(quote.split())} words (target ≤30)")

    # Value should be a number if provided
    if "value" in result and result["value"] is not None:
        if not isinstance(result["value"], (int, float)):
            errors.append(f"value must be a number or None, got {type(result['value']).__name__}")

    return errors


def make_result(
    source_document_id: int,
    metric_key: str,
    value: float | None,
    value_text: str,
    unit: str,
    quote: str,
    locator_section: str,
    extraction_type: str = "direct",
    **kwargs: Any,
) -> ExtractionResult:
    """Convenience constructor with defaults filled in."""
    return ExtractionResult(
        source_document_id=source_document_id,
        metric_key=metric_key,
        value=value,
        value_text=value_text,
        unit=unit,
        quote=quote,
        locator_section=locator_section,
        extraction_type=extraction_type,
        **kwargs,
    )
