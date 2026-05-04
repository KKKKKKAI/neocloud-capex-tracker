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
    convention_check_fn: Any | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Validate and write a batch of extraction results to the DB.

    Args:
        results: list of extraction result dicts. Each must have the
            fields defined in protocol.v0_1_0.ExtractionResult.
        db: Database instance (defaults to project DB).
        provenance_check_fn: optional callable(quote, source_text) → bool
            for substring verification. If provided, each quote is checked
            and the result is written to validation_results.
        force: if True, overwrite an existing row for
            (source_document_id, metric_key, extracting_model,
            period_type) instead of skipping it. Writes an
            `extraction_overwritten` audit_log entry carrying both
            the old and new values. Used by the Protocol Elicitation
            Loop re-extract path.

    Returns:
        Summary dict: {inserted, overwritten, skipped_existing, errors, ids}
    """
    db = db or Database()
    now = _now_iso()

    summary: dict[str, Any] = {
        "inserted": 0,
        "overwritten": 0,
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

        # 2c. Optional convention check (callable takes result, returns
        # ConventionCheck-like object with .ok and .warnings). Failures
        # are appended to summary["errors"] and the row is still
        # written — the writer never drops data, it flags it.
        if convention_check_fn is not None:
            check = convention_check_fn(result)
            if check is not None and not getattr(check, "ok", True):
                summary["errors"].append({
                    "metric_key": result.get("metric_key", "?"),
                    "convention_warnings": getattr(check, "warnings", []),
                })

        # 3. Write to DB
        try:
            row_id, was_overwrite = _insert_extraction(db, result, now, force)
            if row_id is None:
                summary["skipped_existing"] += 1
            else:
                if was_overwrite:
                    summary["overwritten"] += 1
                else:
                    summary["inserted"] += 1
                summary["ids"].append(row_id)

                # 4. Persist dual-agent excerpts so citations.py can
                # surface the Quote: "..." line in Excel cell comments.
                # The headless extractor populates these; XBRL rows
                # don't have them and skip this branch.
                excerpts = result.get("excerpts") or []
                if excerpts:
                    _store_evidence(db, row_id, excerpts, now)

                # 5. Provenance check if function provided
                if provenance_check_fn is not None:
                    _run_provenance_check(db, row_id, result, provenance_check_fn, now)

        except Exception as e:
            summary["errors"].append({
                "metric_key": result.get("metric_key", "?"),
                "exception": f"{type(e).__name__}: {e}",
            })

    return summary


def _insert_extraction(
    db: Database, result: dict[str, Any], now: str, force: bool = False,
) -> tuple[int | None, bool]:
    """Insert one extraction row.

    Returns `(row_id, was_overwrite)`. When `force=True` and a row
    already exists for the same (source_document_id, metric_key,
    extracting_model, period_type), the old row is deleted and a
    new one inserted, with an `extraction_overwritten` audit_log
    entry carrying both the old row id and the old numeric value.
    """
    period_type = result.get("period_type", "") or ""
    with db.mutating() as conn:
        # Idempotent: include period_type so one filing can carry
        # multiple period-basis rows for the same metric + model.
        existing = conn.execute(
            """
            SELECT id, value, value_usd FROM extractions
            WHERE source_document_id = ? AND metric_key = ?
              AND extracting_model = ? AND period_type = ?
            """,
            (
                result["source_document_id"],
                result["metric_key"],
                result["extracting_model"],
                period_type,
            ),
        ).fetchone()

        was_overwrite = False
        if existing:
            if not force:
                return None, False
            # Delete the old row + dependent evidence rows; the audit
            # trail below records the prior value so we never lose it.
            old_id, old_value, old_value_usd = (
                existing[0], existing[1], existing[2],
            )
            conn.execute(
                "DELETE FROM extraction_evidence WHERE extraction_id = ?",
                (old_id,),
            )
            conn.execute(
                "DELETE FROM validation_results WHERE extraction_id = ?",
                (old_id,),
            )
            conn.execute("DELETE FROM extractions WHERE id = ?", (old_id,))
            conn.execute(
                """
                INSERT INTO audit_log
                    (ts, actor, action, target_table, target_id, payload)
                VALUES (?, ?, 'extraction_overwritten', 'extractions', ?, ?)
                """,
                (
                    now,
                    ACTOR_EXTRACT,
                    old_id,
                    json.dumps({
                        "old_value": old_value,
                        "old_value_usd": old_value_usd,
                        "new_value": result.get("value"),
                        "metric_key": result["metric_key"],
                        "source_document_id": result["source_document_id"],
                        "extracting_model": result["extracting_model"],
                        "period_type": period_type,
                    }, sort_keys=True),
                ),
            )
            was_overwrite = True

        cur = conn.execute(
            """
            INSERT INTO extractions (
                source_document_id, metric_key, value, value_text, unit,
                quote, locator_page, locator_section, extraction_type,
                confidence, extracting_model, protocol_version, extracted_at,
                value_usd, fx_rate, fx_rate_date, reporting_currency,
                period_type, basis_period_months, reporting_convention
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                period_type,
                result.get("basis_period_months"),
                result.get("reporting_convention"),
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

        return row_id, was_overwrite


_VALID_EVIDENCE_ROLES = (
    "primary_value", "supporting", "derivation_input", "footnote",
)


def _store_evidence(
    db: Database,
    extraction_id: int,
    excerpts: list[dict[str, Any]],
    now: str,
) -> None:
    """Insert each excerpt into extraction_evidence.

    First excerpt without an explicit role is tagged 'primary_value'
    (citations.py looks for that role to build the Quote: line). Any
    role outside the valid set falls back to 'supporting'.
    """
    promoted_primary = False
    with db.mutating() as conn:
        for exc in excerpts:
            role = (exc.get("role") or "").strip()
            if not role:
                role = "primary_value" if not promoted_primary else "supporting"
            if role not in _VALID_EVIDENCE_ROLES:
                role = "supporting"
            if role == "primary_value":
                promoted_primary = True
            conn.execute(
                """
                INSERT INTO extraction_evidence
                    (extraction_id, excerpt_text, excerpt_location,
                     excerpt_role, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    extraction_id,
                    exc.get("text", ""),
                    exc.get("location", ""),
                    role,
                    now,
                ),
            )


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
