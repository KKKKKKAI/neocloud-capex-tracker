"""Post-extraction validation checks.

Run after every extraction to catch implausible values, inconsistencies,
and outliers. Results are written to the validation_results table.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..db import Database

# Plausible ranges per metric (in USD millions)
RANGE_LIMITS: dict[str, tuple[float, float]] = {
    "capital_expenditures": (0, 200_000),
    "revenue": (0, 700_000),
    "operating_cash_flow": (-50_000, 250_000),
    "depreciation_amortization": (0, 100_000),
    "property_plant_equipment_net": (0, 500_000),
    "cloud_segment_revenue": (0, 200_000),
}

# YoY thresholds that trigger a flag (not a hard fail)
YOY_UPPER = 2.0   # >100% growth
YOY_LOWER = -0.50  # >50% decline


def check_range_plausibility(
    extraction_id: int,
    metric_key: str,
    value_usd: float | None,
    *,
    db: Database | None = None,
) -> dict[str, Any] | None:
    """Check that the value falls within a plausible range.

    Returns check result dict, or None if not applicable.
    """
    if value_usd is None:
        return None

    limits = RANGE_LIMITS.get(metric_key)
    if not limits:
        return None

    lo, hi = limits
    passed = lo <= value_usd <= hi
    details = {
        "value_usd": value_usd,
        "range": [lo, hi],
        "metric_key": metric_key,
    }
    if not passed:
        details["reason"] = f"Value ${value_usd:,.0f}M outside range [${lo:,.0f}M, ${hi:,.0f}M]"

    return {
        "extraction_id": extraction_id,
        "check_name": "range_plausibility",
        "passed": passed,
        "details": details,
    }


def check_yoy_outlier(
    extraction_id: int,
    ticker: str,
    metric_key: str,
    period: str,
    value_usd: float | None,
    *,
    db: Database | None = None,
) -> dict[str, Any] | None:
    """Flag YoY changes > 100% or < -50%.

    Not a hard fail — some companies genuinely double revenue.
    But flags for human review.
    """
    if value_usd is None or value_usd == 0:
        return None

    db = db or Database()

    # Find prior year's value
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(e.value_usd, e.value) as prior_val
            FROM extractions e
            JOIN source_documents sd ON e.source_document_id = sd.id
            WHERE sd.ticker = ? AND e.metric_key = ?
            AND sd.period_of_report < ?
            ORDER BY sd.period_of_report DESC LIMIT 1
            """,
            (ticker, metric_key, period),
        ).fetchone()

    if not row or not row["prior_val"] or row["prior_val"] == 0:
        return None

    prior = row["prior_val"]
    yoy = (value_usd - prior) / abs(prior)
    passed = YOY_LOWER <= yoy <= YOY_UPPER

    return {
        "extraction_id": extraction_id,
        "check_name": "yoy_outlier",
        "passed": passed,
        "details": {
            "value_usd": value_usd,
            "prior_value_usd": prior,
            "yoy_change": round(yoy, 4),
            "thresholds": [YOY_LOWER, YOY_UPPER],
        },
    }


def run_checks(
    extraction_id: int,
    ticker: str,
    metric_key: str,
    period: str,
    value_usd: float | None,
    *,
    db: Database | None = None,
) -> list[dict[str, Any]]:
    """Run all applicable validation checks on an extraction.

    Returns list of check results. Each is written to validation_results.
    """
    db = db or Database()
    results = []

    for check_fn in [
        lambda: check_range_plausibility(extraction_id, metric_key, value_usd, db=db),
        lambda: check_yoy_outlier(extraction_id, ticker, metric_key, period, value_usd, db=db),
    ]:
        result = check_fn()
        if result:
            results.append(result)

    # Write results to validation_results table
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.mutating() as conn:
        for r in results:
            conn.execute(
                """
                INSERT OR REPLACE INTO validation_results
                    (extraction_id, check_name, passed, details, checked_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    r["extraction_id"],
                    r["check_name"],
                    1 if r["passed"] else 0,
                    json.dumps(r["details"]),
                    now,
                ),
            )

    return results
