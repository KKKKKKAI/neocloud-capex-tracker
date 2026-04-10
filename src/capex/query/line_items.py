"""User-facing line-item lookup.

Resolves a question like "what was MSFT's FY2025 capex?" into a
(ticker, metric_key, period) tuple, checks the extractions cache, and
returns the value with full provenance.

If the value hasn't been extracted yet (cache miss), the caller is
responsible for invoking the read-and-extract skill to fill the cache
and then retrying. This module does NOT invoke the extraction itself —
it's a pure data lookup with metric resolution.

Usage:
    from capex.query.line_items import query_metric

    result = query_metric("MSFT", "capex")
    if result is None:
        print("cache miss — run read-and-extract first")
    else:
        print(f"value: {result['value']} {result['unit']}")
        print(f"quote: {result['quote']}")
"""
from __future__ import annotations

import json
from typing import Any

from ..db import Database


def query_metric(
    ticker: str,
    metric: str,
    period: str | None = None,
    *,
    db: Database | None = None,
) -> dict[str, Any] | None:
    """Look up an extracted metric value with provenance.

    Args:
        ticker: companies.ticker key (e.g. "MSFT")
        metric: natural-language phrase or canonical metric_key
        period: ISO date period_of_report (e.g. "2025-06-30") or None for latest

    Returns:
        Dict with value + provenance fields, or None if not extracted yet (cache miss).
    """
    db = db or Database()

    # 1. Resolve the metric phrase to a canonical key
    metric_key = resolve_metric_key(metric, db=db)
    if metric_key is None:
        return {"error": f"unknown metric: {metric!r}", "cache_hit": False}

    # 2. Find the source document
    with db.connect() as conn:
        if period:
            doc_row = conn.execute(
                """
                SELECT id, ticker, form_type, period_of_report, sha256, raw_path
                FROM source_documents
                WHERE ticker = ? AND period_of_report = ?
                ORDER BY period_of_report DESC LIMIT 1
                """,
                (ticker, period),
            ).fetchone()
        else:
            # Latest annual filing
            doc_row = conn.execute(
                """
                SELECT id, ticker, form_type, period_of_report, sha256, raw_path
                FROM source_documents
                WHERE ticker = ? AND form_type IN ('10-K', '20-F', 'HK-AR')
                ORDER BY period_of_report DESC LIMIT 1
                """,
                (ticker,),
            ).fetchone()

    if doc_row is None:
        return {"error": f"no source document for {ticker}", "cache_hit": False}

    doc_id = doc_row["id"]
    period_of_report = doc_row["period_of_report"]
    sha256 = doc_row["sha256"]
    raw_path = doc_row["raw_path"]

    # 3. Check the extractions cache
    with db.connect() as conn:
        ext_row = conn.execute(
            """
            SELECT e.id, e.value, e.value_text, e.unit, e.quote,
                   e.locator_section, e.locator_page, e.extraction_type,
                   e.confidence, e.extracting_model, e.extracted_at
            FROM extractions e
            WHERE e.source_document_id = ? AND e.metric_key = ?
            ORDER BY e.extracted_at DESC LIMIT 1
            """,
            (doc_id, metric_key),
        ).fetchone()

    if ext_row is None:
        return None  # cache miss

    # 4. Check for XBRL anchor validation result
    xbrl_match = None
    with db.connect() as conn:
        vr_row = conn.execute(
            """
            SELECT passed, details FROM validation_results
            WHERE extraction_id = ? AND check_name = 'xbrl_anchor_match'
            ORDER BY checked_at DESC LIMIT 1
            """,
            (ext_row["id"],),
        ).fetchone()
        if vr_row:
            xbrl_match = bool(vr_row["passed"])

    return {
        "ticker": ticker,
        "period": period_of_report,
        "metric_key": metric_key,
        "value": ext_row["value"],
        "value_text": ext_row["value_text"],
        "unit": ext_row["unit"],
        "quote": ext_row["quote"],
        "section_ref": ext_row["locator_section"],
        "extraction_type": ext_row["extraction_type"],
        "source_path": raw_path,
        "sha256": sha256,
        "cache_hit": True,
        "xbrl_anchor_match": xbrl_match,
        "extracting_model": ext_row["extracting_model"],
        "extracted_at": ext_row["extracted_at"],
    }


def resolve_metric_key(
    metric: str,
    *,
    db: Database | None = None,
) -> str | None:
    """Resolve a natural-language metric phrase to a canonical metric_key.

    Checks:
    1. Exact match against metric_definitions.key
    2. Case-insensitive match against aliases
    3. Substring match against aliases (partial matching)

    Returns the canonical key or None.
    """
    db = db or Database()
    metric_lower = metric.strip().lower()

    with db.connect() as conn:
        rows = conn.execute("SELECT key, aliases FROM metric_definitions").fetchall()

    # Exact key match
    for row in rows:
        if row["key"] == metric_lower or row["key"] == metric:
            return row["key"]

    # Alias match (case-insensitive)
    for row in rows:
        aliases = json.loads(row["aliases"]) if row["aliases"] else []
        for alias in aliases:
            if isinstance(alias, str) and alias.lower() == metric_lower:
                return row["key"]

    # Substring match (e.g. "capex" matches "capital expenditures")
    for row in rows:
        if metric_lower in row["key"]:
            return row["key"]
        aliases = json.loads(row["aliases"]) if row["aliases"] else []
        for alias in aliases:
            if isinstance(alias, str) and metric_lower in alias.lower():
                return row["key"]

    return None


def list_available_metrics(
    ticker: str,
    *,
    db: Database | None = None,
) -> list[dict[str, Any]]:
    """List all extracted metrics for a ticker (cache state overview)."""
    db = db or Database()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT e.metric_key, e.value, e.unit, e.extracted_at,
                   sd.period_of_report, sd.form_type
            FROM extractions e
            JOIN source_documents sd ON e.source_document_id = sd.id
            WHERE sd.ticker = ?
            ORDER BY sd.period_of_report DESC, e.metric_key
            """,
            (ticker,),
        ).fetchall()
    return [{k: row[k] for k in row.keys()} for row in rows]
