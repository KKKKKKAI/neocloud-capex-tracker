"""XBRL extractor — wraps xbrl/timeseries.py behind the Extractor protocol.

Handles headline financial metrics for all SEC filers. Free, fast,
fully automated. Covers ~90% of all extractions.
"""
from __future__ import annotations

from typing import Any

from ...db import Database
from ...xbrl.timeseries import fetch_concept_timeseries
from ..base import ExtractionCandidate
from ..coverage import DatasetTreatment

# Map metric_key → XBRL concept candidates (try in order)
CONCEPT_MAP: dict[str, list[str]] = {
    "capital_expenditures": [
        "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
    ],
    "revenue": [
        "us-gaap:Revenues",
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:SalesRevenueNet",
    ],
    "operating_cash_flow": [
        "us-gaap:NetCashProvidedByOperatingActivities",
    ],
    "depreciation_amortization": [
        "us-gaap:DepreciationDepletionAndAmortization",
        "us-gaap:DepreciationAndAmortization",
    ],
    "property_plant_equipment_net": [
        "us-gaap:PropertyPlantAndEquipmentNet",
    ],
}


class XBRLExtractor:
    """Extraction backend using SEC XBRL companyfacts API."""

    name = "xbrl"

    def can_handle(
        self,
        ticker: str,
        metric_key: str,
        form_type: str | None,
        treatment: DatasetTreatment | None,
    ) -> bool:
        """XBRL can handle headline metrics for companies with a CIK."""
        if metric_key not in CONCEPT_MAP:
            return False
        # Check company has a CIK
        db = Database()
        with db.connect() as conn:
            row = conn.execute(
                "SELECT edgar_cik FROM companies WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        return bool(row and row["edgar_cik"])

    def extract(
        self,
        ticker: str,
        metric_key: str,
        period: str | None = None,
        form_type: str | None = None,
        **kwargs: Any,
    ) -> list[ExtractionCandidate] | None:
        """Pull data from XBRL companyfacts API.

        Returns ExtractionCandidate list or None if no data found.
        """
        db = kwargs.get("db") or Database()

        # Get CIK
        with db.connect() as conn:
            row = conn.execute(
                "SELECT edgar_cik, reporting_currency FROM companies WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        if not row or not row["edgar_cik"]:
            return None

        cik = row["edgar_cik"]
        currency = row["reporting_currency"] or "USD"

        # Try each XBRL concept
        concepts = CONCEPT_MAP.get(metric_key, [])
        series = []
        for concept in concepts:
            try:
                series = fetch_concept_timeseries(
                    cik=cik, concept=concept, start_date="2015-01-01",
                )
                if series:
                    break
            except Exception:
                continue

        if not series:
            return None

        # Filter to requested period if specified
        if period:
            series = [p for p in series if p["end"] == period]

        # Convert to ExtractionCandidates
        candidates = []
        for point in series:
            val_raw = point["val"]
            val_millions = round(val_raw / 1e6, 2) if val_raw else None

            # Find or reference the source document
            form = point["form"].rstrip("/A")
            doc_id = self._find_source_doc(
                db, ticker, form, point["end"], point.get("filed", ""),
                point.get("accn", ""),
            )
            if doc_id is None:
                continue

            candidates.append(ExtractionCandidate(
                source_document_id=doc_id,
                metric_key=metric_key,
                value=val_millions,
                value_text=f"${val_millions:,.0f} million" if val_millions else "n/a",
                unit="USD_millions",
                quote=f"XBRL: {metric_key}",
                locator_section="SEC XBRL companyfacts API",
                extraction_type="direct",
                extracting_model="xbrl-verified",
                reporting_currency=currency,
            ))

        return candidates if candidates else None

    def _find_source_doc(
        self, db: Database, ticker: str, form_type: str,
        period: str, filed: str, accn: str,
    ) -> int | None:
        """Find existing source_documents row, or create synthetic one."""
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM source_documents "
                "WHERE ticker = ? AND form_type = ? AND period_of_report = ?",
                (ticker, form_type, period),
            ).fetchone()
            if row:
                return row[0]

        # Create synthetic row via existing helper
        from datetime import datetime, timezone

        from ...xbrl.timeseries import _ensure_source_doc
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return _ensure_source_doc(db, ticker, form_type, period, filed, accn, now)
