"""LLM interactive extractor — prepares structured prompts for dual-agent verification.

In v1, this extractor runs inside a Claude Code session. It:
1. Reads the filing sections
2. Formats the Agent A prompt (extraction + context capture)
3. Returns a "needs_interactive" marker in batch mode
4. In interactive mode, the dual-agent verification module handles
   both Agent A and Agent B calls

In Phase 3.5, this will gain a headless API adapter that can call
the Anthropic API directly.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...db import Database
from ...read.sections import get_extraction_sections, parse_sections
from ...read.text import extract_text
from ..base import ExtractionCandidate
from ..coverage import DatasetTreatment


class LLMInteractiveExtractor:
    """Extraction backend using LLM with dual-agent verification."""

    name = "llm"

    def can_handle(
        self,
        ticker: str,
        metric_key: str,
        form_type: str | None,
        treatment: DatasetTreatment | None,
    ) -> bool:
        """LLM is the universal fallback — always returns True."""
        return True

    def extract(
        self,
        ticker: str,
        metric_key: str,
        period: str | None = None,
        form_type: str | None = None,
        **kwargs: Any,
    ) -> list[ExtractionCandidate] | None:
        """Prepare filing sections for LLM extraction.

        In batch mode (interactive=False), returns None with a side-effect
        of recording this as a "needs_interactive" item.

        In interactive mode, this prepares the filing text and returns
        candidates that need dual-agent verification.
        """
        interactive = kwargs.get("interactive", False)
        db = kwargs.get("db") or Database()

        if not interactive:
            # In batch mode, we can't run LLM — signal to router
            return None

        # Find the filing
        with db.connect() as conn:
            query = (
                "SELECT id, raw_path, form_type, period_of_report, ticker "
                "FROM source_documents WHERE ticker = ? "
            )
            params: list[Any] = [ticker]
            if period:
                query += "AND period_of_report = ? "
                params.append(period)
            if form_type:
                query += "AND form_type = ? "
                params.append(form_type)
            query += "ORDER BY period_of_report DESC LIMIT 1"
            row = conn.execute(query, params).fetchone()

        if not row:
            return None

        filepath = Path(row["raw_path"])
        if not filepath.exists():
            return None

        # Extract text and sections
        text = extract_text(str(filepath))
        sections = parse_sections(text, row["form_type"])
        extraction_sections = get_extraction_sections(sections, row["form_type"])

        if not extraction_sections:
            return None

        sections_text = "\n\n".join(
            f"## {name}\n{content}"
            for name, content in extraction_sections.items()
        )

        # Store prepared context for the dual-agent verification module
        # The actual LLM call happens in verification/dual_agent.py
        return [ExtractionCandidate(
            source_document_id=row["id"],
            metric_key=metric_key,
            value=None,  # to be filled by dual-agent
            value_text="",
            extracting_model="claude-code",
            extraction_type="direct",
            # Store the filing sections in excerpts for the verification module
            excerpts=[{
                "text": sections_text,
                "location": "Full extraction sections",
                "role": "filing_sections",
            }],
        )]

    def get_filing_sections(
        self,
        ticker: str,
        period: str | None = None,
        form_type: str | None = None,
        *,
        db: Database | None = None,
    ) -> dict[str, str] | None:
        """Load and parse filing sections for external callers.

        Used by the dual-agent verification module to get the full
        filing text for Agent A.
        """
        db = db or Database()

        with db.connect() as conn:
            query = (
                "SELECT raw_path, form_type "
                "FROM source_documents WHERE ticker = ? "
            )
            params: list[Any] = [ticker]
            if period:
                query += "AND period_of_report = ? "
                params.append(period)
            if form_type:
                query += "AND form_type = ? "
                params.append(form_type)
            query += "ORDER BY period_of_report DESC LIMIT 1"
            row = conn.execute(query, params).fetchone()

        if not row:
            return None

        filepath = Path(row["raw_path"])
        if not filepath.exists():
            return None

        text = extract_text(str(filepath))
        sections = parse_sections(text, row["form_type"])
        return get_extraction_sections(sections, row["form_type"])
