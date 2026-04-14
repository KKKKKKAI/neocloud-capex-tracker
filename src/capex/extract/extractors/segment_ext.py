"""Segment extractor — wraps extract/segment.py behind the Extractor protocol.

Handles cloud_segment_revenue for US hyperscalers with clean segment
tables (MSFT, AMZN, GOOGL, ORCL). Uses regex-based table scoring.
"""
from __future__ import annotations

from typing import Any

from ...db import Database
from ..base import ExtractionCandidate
from ..coverage import DatasetTreatment
from ..segment import extract_segment_revenue


class SegmentExtractor:
    """Extraction backend using regex table scoring for segment revenue."""

    name = "segment"

    def can_handle(
        self,
        ticker: str,
        metric_key: str,
        form_type: str | None,
        treatment: DatasetTreatment | None,
    ) -> bool:
        """Only handles cloud_segment_revenue with named_segment treatment."""
        if metric_key != "cloud_segment_revenue":
            return False
        if not treatment:
            return False
        return treatment.treatment in ("named_segment",)

    def extract(
        self,
        ticker: str,
        metric_key: str,
        period: str | None = None,
        form_type: str | None = None,
        **kwargs: Any,
    ) -> list[ExtractionCandidate] | None:
        """Extract segment revenue from a downloaded filing using table scoring."""
        db = kwargs.get("db") or Database()
        treatment = kwargs.get("treatment")

        if not treatment or not treatment.segment_names:
            return None

        # Find the filing path
        with db.connect() as conn:
            query = (
                "SELECT id, raw_path, form_type, period_of_report "
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

        if not row or not row["raw_path"]:
            return None

        from pathlib import Path
        filepath = Path(row["raw_path"])
        if not filepath.exists():
            return None

        # Run the segment extractor
        try:
            results = extract_segment_revenue(
                str(filepath),
                ticker,
                treatment.segment_names,
                form_type=row["form_type"],
            )
        except Exception:
            return None

        if not results:
            return None

        # Convert to ExtractionCandidates
        candidates = []
        for r in results:
            candidates.append(ExtractionCandidate(
                source_document_id=row["id"],
                metric_key=metric_key,
                value=r.get("value"),
                value_text=f"${r.get('value', 0):,.0f} million",
                unit="USD_millions",
                quote=r.get("segment_context", ""),
                locator_section=f"Segment table: {r.get('segment_name', '')}",
                extraction_type="direct",
                extracting_model="regex-segment",
            ))

        return candidates if candidates else None
