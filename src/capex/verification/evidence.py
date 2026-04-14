"""CRUD operations for the extraction_evidence table.

Stores the verbatim text excerpts that prove an extracted value,
along with the dual-agent verification verdict.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..db import Database

VALID_ROLES = ("primary_value", "supporting", "derivation_input", "footnote")


def store_evidence(
    extraction_id: int,
    excerpts: list[dict[str, str]],
    *,
    db: Database | None = None,
) -> list[int]:
    """Store context excerpts for an extraction.

    Each excerpt dict should have:
        text: str — verbatim text from the filing
        location: str — section/page reference
        role: str — one of VALID_ROLES

    Returns list of inserted evidence IDs.
    """
    db = db or Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ids = []

    with db.mutating() as conn:
        for exc in excerpts:
            role = exc.get("role", "supporting")
            if role not in VALID_ROLES:
                role = "supporting"

            cur = conn.execute(
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
            ids.append(cur.lastrowid)

    return ids


def store_verification_verdict(
    extraction_id: int,
    value_a: float | None,
    value_b: float | None,
    match_type: str,
    reasoning_a: str,
    reasoning_b: str,
    attempt: int = 1,
    *,
    db: Database | None = None,
) -> int:
    """Store the dual-agent verification verdict in validation_results.

    Returns the validation_results row ID.
    """
    import json

    db = db or Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    passed = 1 if match_type in ("exact", "approximate") else 0

    details = json.dumps({
        "value_a": value_a,
        "value_b": value_b,
        "match_type": match_type,
        "reasoning_a": reasoning_a,
        "reasoning_b": reasoning_b,
        "attempt": attempt,
        "max_attempts": 3,
    })

    with db.mutating() as conn:
        cur = conn.execute(
            """
            INSERT INTO validation_results
                (extraction_id, check_name, passed, details, checked_at)
            VALUES (?, 'dual_agent_verification', ?, ?, ?)
            """,
            (extraction_id, passed, details, now),
        )
        return cur.lastrowid


def get_evidence(
    extraction_id: int,
    *,
    role: str | None = None,
    db: Database | None = None,
) -> list[dict[str, Any]]:
    """Retrieve evidence excerpts for an extraction."""
    db = db or Database()
    with db.connect() as conn:
        query = "SELECT * FROM extraction_evidence WHERE extraction_id = ?"
        params: list[Any] = [extraction_id]
        if role:
            query += " AND excerpt_role = ?"
            params.append(role)
        query += " ORDER BY id"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_primary_quote(
    extraction_id: int,
    *,
    max_length: int = 200,
    db: Database | None = None,
) -> str | None:
    """Get the most relevant sentence from the primary_value excerpt.

    Used by citations.py to populate the quote field in Excel comments.
    Returns the first sentence containing a number, truncated to max_length.
    """
    import re

    evidence = get_evidence(extraction_id, role="primary_value", db=db)
    if not evidence:
        return None

    text = evidence[0]["excerpt_text"]
    # Find the first sentence containing a number
    sentences = re.split(r'(?<=[.!?])\s+', text)
    for sentence in sentences:
        if re.search(r'\d', sentence):
            return sentence[:max_length]

    # Fallback: first sentence
    return sentences[0][:max_length] if sentences else text[:max_length]


def get_unverified_extractions(
    *,
    ticker: str | None = None,
    db: Database | None = None,
) -> list[dict[str, Any]]:
    """Get extractions that lack dual-agent verification.

    Used by `capex review` CLI to show items needing human review.
    """
    db = db or Database()
    with db.connect() as conn:
        query = """
            SELECT e.id, sd.ticker, e.metric_key, e.value, e.value_usd,
                   sd.period_of_report, sd.form_type, e.extracting_model
            FROM extractions e
            JOIN source_documents sd ON e.source_document_id = sd.id
            WHERE e.extracting_model NOT LIKE 'xbrl%'
            AND e.id NOT IN (
                SELECT extraction_id FROM validation_results
                WHERE check_name = 'dual_agent_verification'
                AND passed = 1
            )
        """
        params: list[Any] = []
        if ticker:
            query += " AND sd.ticker = ?"
            params.append(ticker)
        query += " ORDER BY sd.ticker, sd.period_of_report"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
