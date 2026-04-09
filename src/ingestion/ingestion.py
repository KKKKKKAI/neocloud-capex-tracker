"""Ingestion entry point — placeholder.

Responsibilities (when implemented):
    - Download filing from URL emitted by the watcher.
    - Extract text (pdfplumber for PDFs, bs4 for HTML).
    - Assign canonical page IDs injected as `<page id="pN">` markers in text.
    - Compute SHA-256 of raw source; store alongside normalized text.
    - Preserve table structure where possible for downstream extraction.

Design notes:
    - No LLM calls.
    - Canonical page IDs are the only locator format the extraction layer should trust.
    - Source files are stored under data/sources/ and tracked via Git LFS.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IngestedDocument:
    """Normalized filing ready for extraction."""

    company: str
    period: str           # e.g. "2025-Q4"
    filing_type: str      # e.g. "10-Q"
    source_url: str
    source_hash: str      # SHA-256 hex
    canonical_text: str   # with <page id="pN"> markers
    ingested_at: str      # ISO 8601


def ingest(url: str) -> IngestedDocument:
    """Placeholder. Not yet implemented."""
    raise NotImplementedError("Ingestion layer is not yet implemented.")
