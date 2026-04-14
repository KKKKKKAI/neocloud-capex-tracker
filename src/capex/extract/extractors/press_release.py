"""6-K press release extractor — regex-based, returns None on failure.

When this extractor returns None, the router falls through to the
LLM extractor (which applies dual-agent verification).
"""
from __future__ import annotations

import re
from typing import Any

from ...db import Database
from ..base import ExtractionCandidate
from ..coverage import DatasetTreatment


# Per-company regex patterns for quarterly earnings press releases
REVENUE_PATTERNS: dict[str, list[str]] = {
    "BABA": [
        r"[Rr]evenue\s+(?:for the (?:quarter|three months)[^.]*?\s+)?(?:was|were)\s+RMB\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*million",
        r"[Rr]evenue\s+(?:was|were)\s+RMB\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*million",
    ],
    "BIDU": [
        r"[Tt]otal\s+revenues?\s+(?:was|were)\s+RMB\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?:million|billion)",
        r"[Rr]evenues?\s+(?:was|were)\s+RMB\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?:million|billion)",
    ],
    "GDS": [
        r"[Rr]evenue\s+(?:was|were|of)\s+RMB\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*million",
    ],
    "NBIS": [
        r"[Rr]evenue\s+(?:was|were|of)\s+(?:US\$|\$)\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*million",
    ],
}


class PressReleaseExtractor:
    """Extraction backend for 6-K quarterly earnings press releases."""

    name = "6k_press"

    def can_handle(
        self,
        ticker: str,
        metric_key: str,
        form_type: str | None,
        treatment: DatasetTreatment | None,
    ) -> bool:
        """Handles revenue extraction from 6-K press releases."""
        return (
            form_type == "6-K"
            and metric_key in ("revenue", "cloud_segment_revenue")
            and ticker in REVENUE_PATTERNS
        )

    def extract(
        self,
        ticker: str,
        metric_key: str,
        period: str | None = None,
        form_type: str | None = None,
        **kwargs: Any,
    ) -> list[ExtractionCandidate] | None:
        """Try regex patterns on 6-K filing text.

        Returns None if regex fails — router falls through to LLM.
        """
        db = kwargs.get("db") or Database()

        # Find the 6-K filing
        with db.connect() as conn:
            query = (
                "SELECT id, raw_path, period_of_report "
                "FROM source_documents WHERE ticker = ? "
                "AND form_type = '6-K' "
            )
            params: list[Any] = [ticker]
            if period:
                query += "AND period_of_report = ? "
                params.append(period)
            query += "ORDER BY period_of_report DESC LIMIT 1"
            row = conn.execute(query, params).fetchone()

        if not row or not row["raw_path"]:
            return None

        # Read the filing text
        from pathlib import Path
        filepath = Path(row["raw_path"])
        if not filepath.exists():
            return None

        try:
            text = filepath.read_text(encoding="utf-8", errors="replace")
            # Strip HTML
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"&nbsp;", " ", text)
            text = re.sub(r"&[a-z]+;", " ", text)
            text = re.sub(r"\s+", " ", text)
        except Exception:
            return None

        # Try patterns
        patterns = REVENUE_PATTERNS.get(ticker, [])
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                val_str = m.group(1).replace(",", "")
                val = round(float(val_str))

                return [ExtractionCandidate(
                    source_document_id=row["id"],
                    metric_key=metric_key,
                    value=val,
                    value_text=f"{val:,.0f} million",
                    unit="USD_millions",
                    quote=m.group(0)[:100],
                    locator_section="6-K Press Release — Revenue Summary",
                    extraction_type="direct",
                    extracting_model="regex-6k",
                )]

        # Regex failed — return None so router tries LLM
        return None
