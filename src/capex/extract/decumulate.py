"""Canonical de-cumulation for quarterly financial data.

SEC XBRL 10-Q data is cumulative year-to-date. This module provides
a single implementation used by the XBRL extractor, the Excel exporter,
and the interactive chart generator.

Flow metrics (revenue, capex, OCF, D&A, cloud_segment_revenue) need
de-cumulation. Stock metrics (PP&E net) do not.

    Q1_standalone = Q1               (already standalone)
    Q2_standalone = Q2_ytd - Q1
    Q3_standalone = Q3_ytd - Q2_ytd
    Q4_standalone = FY - Q3_ytd
"""
from __future__ import annotations

from typing import Any

# Metrics that are flow (cumulative YTD in SEC filings)
FLOW_METRICS = frozenset({
    "capital_expenditures",
    "revenue",
    "operating_cash_flow",
    "depreciation_amortization",
    "cloud_segment_revenue",
})

# Metrics that are point-in-time (no de-cumulation)
STOCK_METRICS = frozenset({
    "property_plant_equipment_net",
})


def is_flow_metric(metric_key: str) -> bool:
    """Return True if this metric needs quarterly de-cumulation."""
    return metric_key in FLOW_METRICS


def decumulate_series(
    series: list[dict[str, Any]],
    *,
    val_key: str = "val",
    fy_key: str = "fy",
    fp_key: str = "fp",
) -> list[dict[str, Any]]:
    """Convert cumulative YTD values to standalone quarterly values.

    Generic function that works on any list of dicts. The caller
    specifies which keys hold the value, fiscal year, and fiscal period.

    Fiscal period values: "Q1", "Q2", "Q3", "FY" (standard XBRL fp tags).

    Returns a new list (original dicts are not mutated). Each dict
    gets an additional 'val_quarterly' key with the standalone value.
    The original cumulative value is preserved under the original key.
    """
    if not series:
        return []

    # Group by fiscal year
    by_fy: dict[Any, list[dict]] = {}
    for point in series:
        fy = point.get(fy_key)
        if fy is None:
            continue
        by_fy.setdefault(fy, []).append(point)

    result = []
    for fy in sorted(by_fy.keys()):
        points = sorted(by_fy[fy], key=lambda x: x.get("end", ""))

        prev_val = 0
        for point in points:
            fp = point.get(fp_key, "")
            val = point[val_key]

            if fp == "Q1":
                quarterly_val = val
                prev_val = val
            elif fp in ("Q2", "Q3"):
                quarterly_val = val - prev_val
                prev_val = val
            elif fp == "FY":
                quarterly_val = val - prev_val
                prev_val = 0
            else:
                quarterly_val = val

            result.append({**point, "val_quarterly": quarterly_val})

    result.sort(key=lambda x: x.get("end", ""))
    return result


def decumulate_db_rows(
    rows: list[dict[str, Any]],
    *,
    metric_key: str,
) -> list[tuple[dict, float]]:
    """De-cumulate extraction rows fetched from the DB.

    Takes rows with 'period_token', 'fiscal_year', and a value field
    (typically 'value_usd' or 'value'). Returns (row, quarterly_value)
    pairs.

    For stock metrics, returns the value unchanged.
    For flow metrics, applies the standard de-cumulation.

    Used by exporters/excel.py and exporters/interactive_chart.py.
    """
    if not is_flow_metric(metric_key):
        return [(r, r.get("value_usd") or r.get("value", 0)) for r in rows]

    # Group by ticker + fiscal year for de-cumulation
    by_key: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r.get("ticker", ""), r.get("fiscal_year", 0))
        by_key.setdefault(key, []).append(r)

    result = []
    for _key, group in by_key.items():
        group.sort(key=lambda x: x.get("period_of_report", ""))
        prev_val = 0
        for r in group:
            val = abs(r.get("value_usd") or r.get("value", 0) or 0)
            token = r.get("period_token", "")

            if token == "Q1":
                qv = val
                prev_val = val
            elif token in ("Q2", "Q3"):
                qv = val - prev_val
                prev_val = val
            elif token == "AR":
                # Annual — not de-cumulated, skip in quarterly context
                qv = val
            else:
                qv = val

            result.append((r, qv))

    return result
