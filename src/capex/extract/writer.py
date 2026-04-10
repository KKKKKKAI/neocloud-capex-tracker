"""Adapter-agnostic extraction result writer.

This module is the STABLE INTERFACE between extraction (however it
happens — Claude Code, Anthropic API, Gemini, a human) and the database.
It validates incoming result dicts against the protocol schema, writes
rows to `extractions` + `validation_results` + `audit_log`, and returns
the inserted row IDs.

Deliberately thin: validate → insert → audit. No LLM calls, no file I/O,
no network. Testable against an in-memory SQLite DB.

Usage:
    from capex.extract.writer import write_extractions
    from capex.db import Database

    results = [
        {
            "source_document_id": 1,
            "metric_key": "capital_expenditures",
            "value": 88000,
            "value_text": "$88.0 billion",
            "unit": "USD_millions",
            "quote": "purchases of property and equipment were $88.0 billion",
            "locator_section": "Item 8 - Cash Flow Statements",
            "extraction_type": "direct",
            "extracting_model": "claude-code",
        },
        ...
    ]

    ids = write_extractions(results, db=Database())
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..db import Database
from ..protocol.v0_1_0 import PROTOCOL_VERSION, validate_result

ACTOR_EXTRACT = "read-and-extract@0.1.0"


def write_extractions(
    results: list[dict[str, Any]],
    *,
    db: Database | None = None,
    provenance_check_fn: Any | None = None,
) -> dict[str, Any]:
    """Validate and write a batch of extraction results to the DB.

    Args:
        results: list of extraction result dicts. Each must have the
            fields defined in protocol.v0_1_0.ExtractionResult.
        db: Database instance (defaults to project DB).
        provenance_check_fn: optional callable(quote, source_text) → bool
            for substring verification. If provided, each quote is checked
            and the result is written to validation_results.

    Returns:
        Summary dict: {inserted: N, skipped_existing: N, errors: [...]}
    """
    db = db or Database()
    now = _now_iso()

    summary: dict[str, Any] = {
        "inserted": 0,
        "skipped_existing": 0,
        "errors": [],
        "ids": [],
    }

    for result in results:
        # 1. Validate
        validation_errors = validate_result(result)
        if validation_errors:
            summary["errors"].append({
                "metric_key": result.get("metric_key", "?"),
                "validation_errors": validation_errors,
            })
            continue

        # 2. Fill defaults
        result.setdefault("extracting_model", "claude-code")
        result.setdefault("protocol_version", PROTOCOL_VERSION)
        result.setdefault("extracted_at", now)
        result.setdefault("locator_page", None)
        result.setdefault("confidence", None)
        result.setdefault("extraction_type", "direct")

        # 2b. FX normalization — compute value_usd if not already set
        if "value_usd" not in result or result["value_usd"] is None:
            _apply_fx_normalization(result, db)

        # 3. Write to DB
        try:
            row_id = _insert_extraction(db, result, now)
            if row_id is None:
                summary["skipped_existing"] += 1
            else:
                summary["inserted"] += 1
                summary["ids"].append(row_id)

                # 4. Provenance check if function provided
                if provenance_check_fn is not None:
                    _run_provenance_check(db, row_id, result, provenance_check_fn, now)

        except Exception as e:
            summary["errors"].append({
                "metric_key": result.get("metric_key", "?"),
                "exception": f"{type(e).__name__}: {e}",
            })

    return summary


def _insert_extraction(
    db: Database, result: dict[str, Any], now: str
) -> int | None:
    """Insert one extraction row. Returns row ID, or None if already exists."""
    with db.mutating() as conn:
        # Idempotent: check if (source_document_id, metric_key, extracting_model) exists
        existing = conn.execute(
            """
            SELECT id FROM extractions
            WHERE source_document_id = ? AND metric_key = ? AND extracting_model = ?
            """,
            (result["source_document_id"], result["metric_key"], result["extracting_model"]),
        ).fetchone()

        if existing:
            return None

        cur = conn.execute(
            """
            INSERT INTO extractions (
                source_document_id, metric_key, value, value_text, unit,
                quote, locator_page, locator_section, extraction_type,
                confidence, extracting_model, protocol_version, extracted_at,
                value_usd, fx_rate, fx_rate_date, reporting_currency
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["source_document_id"],
                result["metric_key"],
                result.get("value"),
                result["value_text"],
                result["unit"],
                result["quote"],
                result.get("locator_page"),
                result["locator_section"],
                result["extraction_type"],
                result.get("confidence"),
                result["extracting_model"],
                result["protocol_version"],
                result["extracted_at"],
                result.get("value_usd"),
                result.get("fx_rate"),
                result.get("fx_rate_date"),
                result.get("reporting_currency", "USD"),
            ),
        )
        row_id = cur.lastrowid

        conn.execute(
            """
            INSERT INTO audit_log (ts, actor, action, target_table, target_id, payload)
            VALUES (?, ?, 'extraction_inserted', 'extractions', ?, ?)
            """,
            (
                now,
                ACTOR_EXTRACT,
                row_id,
                json.dumps({
                    "metric_key": result["metric_key"],
                    "value": result.get("value"),
                    "source_document_id": result["source_document_id"],
                    "extracting_model": result["extracting_model"],
                }, sort_keys=True),
            ),
        )

        return row_id


def _run_provenance_check(
    db: Database,
    extraction_id: int,
    result: dict[str, Any],
    check_fn: Any,
    now: str,
) -> None:
    """Run provenance substring check and write validation_results row."""
    quote = result.get("quote", "")
    passed = bool(check_fn(quote))
    details = {"quote_length": len(quote.split())}

    with db.mutating() as conn:
        conn.execute(
            """
            INSERT INTO validation_results (
                extraction_id, check_name, passed, details, checked_at
            ) VALUES (?, 'provenance_substring', ?, ?, ?)
            """,
            (extraction_id, int(passed), json.dumps(details), now),
        )


def _apply_fx_normalization(result: dict[str, Any], db: Database) -> None:
    """Look up the company's reporting currency and compute value_usd.

    Modifies result dict in-place: sets value_usd, fx_rate, fx_rate_date,
    reporting_currency.
    """
    source_doc_id = result.get("source_document_id")
    if source_doc_id is None:
        result.setdefault("value_usd", result.get("value"))
        result.setdefault("fx_rate", 1.0)
        result.setdefault("fx_rate_date", None)
        result.setdefault("reporting_currency", "USD")
        return

    # Look up company ticker and period from source_documents
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT sd.ticker, sd.period_of_report, c.reporting_currency
            FROM source_documents sd
            JOIN companies c ON sd.ticker = c.ticker
            WHERE sd.id = ?
            """,
            (source_doc_id,),
        ).fetchone()

    if row is None:
        result.setdefault("value_usd", result.get("value"))
        result.setdefault("fx_rate", 1.0)
        result.setdefault("fx_rate_date", None)
        result.setdefault("reporting_currency", "USD")
        return

    currency = row["reporting_currency"] or "USD"
    period = row["period_of_report"]

    from ..fx.rates import normalize_to_usd

    value_usd, fx_rate, fx_rate_date = normalize_to_usd(
        result.get("value"), currency, period, db=db
    )
    result["value_usd"] = value_usd
    result["fx_rate"] = fx_rate
    result["fx_rate_date"] = fx_rate_date
    result["reporting_currency"] = currency


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
