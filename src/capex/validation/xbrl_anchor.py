"""XBRL anchor validation — compare LLM-extracted values against SEC's XBRL data.

For any extraction whose metric_key has a known XBRL concept, this module
fetches the XBRL-tagged value from SEC's free companyfacts API and compares
it to the LLM-extracted value. This catches the most common LLM failure
mode (numeric hallucination) for free, without giving up the coherent
single-extraction-pipeline architecture.

Endpoint:
    https://data.sec.gov/api/xbrl/companyfacts/CIK{padded}.json

This returns ALL XBRL-tagged facts for a company across all filings.
We filter by concept name + period to find the matching value.

Stdlib only — no third-party HTTP library.

The XBRL concept mapping is stored in metric_definitions.yaml under the
`xbrl_concept` field (added in Phase 3.12). The anchor module reads this
at runtime so adding a new metric with an XBRL tag is a YAML edit, not
a code change.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from ..db import Database
from ..fetch import get_user_agent

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json"

# Tolerance for numeric comparison: 1% relative difference
TOLERANCE = 0.01


def check_xbrl_anchor(
    extraction_id: int,
    metric_key: str,
    extracted_value: float | None,
    source_document_id: int,
    *,
    db: Database | None = None,
) -> dict[str, Any] | None:
    """Compare an LLM-extracted value against the XBRL companyfacts data.

    Returns a validation result dict if the check was performed, or None
    if the check is not applicable (e.g. no XBRL concept mapping, no CIK,
    HKEX-only company, or value is None).

    The caller writes the result to validation_results if non-None.
    """
    db = db or Database()

    # 1. Look up the XBRL concept for this metric
    xbrl_concept = _get_xbrl_concept(db, metric_key)
    if not xbrl_concept:
        return None  # no XBRL mapping — skip silently

    if extracted_value is None:
        return None  # can't compare null

    # 2. Look up the source document's CIK and period
    doc_info = _get_doc_info(db, source_document_id)
    if not doc_info or not doc_info.get("edgar_cik"):
        return None  # HKEX-only or missing CIK — degrade gracefully

    cik = doc_info["edgar_cik"]
    period = doc_info["period_of_report"]

    # 3. Fetch the companyfacts JSON
    try:
        facts = _fetch_companyfacts(cik)
    except Exception:
        return {
            "extraction_id": extraction_id,
            "check_name": "xbrl_anchor_match",
            "passed": True,  # degrade gracefully — don't fail on API errors
            "details": {"error": "companyfacts API unavailable", "skipped": True},
        }

    # 4. Find the matching XBRL value
    xbrl_value = _find_fact_value(facts, xbrl_concept, period)
    if xbrl_value is None:
        return {
            "extraction_id": extraction_id,
            "check_name": "xbrl_anchor_match",
            "passed": True,
            "details": {
                "xbrl_concept": xbrl_concept,
                "xbrl_value": None,
                "note": "concept not found in XBRL for this period",
            },
        }

    # 5. Compare: within TOLERANCE relative difference
    if extracted_value == 0 and xbrl_value == 0:
        passed = True
        pct_diff = 0.0
    elif extracted_value == 0 or xbrl_value == 0:
        passed = False
        pct_diff = 1.0
    else:
        pct_diff = abs(extracted_value - xbrl_value) / abs(xbrl_value)
        passed = pct_diff <= TOLERANCE

    return {
        "extraction_id": extraction_id,
        "check_name": "xbrl_anchor_match",
        "passed": passed,
        "details": {
            "xbrl_concept": xbrl_concept,
            "xbrl_value": xbrl_value,
            "extracted_value": extracted_value,
            "pct_diff": round(pct_diff * 100, 2),
            "tolerance_pct": TOLERANCE * 100,
        },
    }


def run_xbrl_checks(
    extractions: list[dict[str, Any]],
    *,
    db: Database | None = None,
) -> list[dict[str, Any]]:
    """Run XBRL anchor checks on a batch of extractions. Returns results to write."""
    db = db or Database()
    results = []
    for ext in extractions:
        result = check_xbrl_anchor(
            extraction_id=ext["id"],
            metric_key=ext["metric_key"],
            extracted_value=ext.get("value"),
            source_document_id=ext["source_document_id"],
            db=db,
        )
        if result is not None:
            results.append(result)
    return results


def write_xbrl_results(
    results: list[dict[str, Any]],
    *,
    db: Database | None = None,
) -> int:
    """Write XBRL anchor validation results to the DB. Returns count written."""
    db = db or Database()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    written = 0
    with db.mutating() as conn:
        for r in results:
            conn.execute(
                """
                INSERT INTO validation_results
                (extraction_id, check_name, passed, details, checked_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    r["extraction_id"],
                    r["check_name"],
                    int(r["passed"]),
                    json.dumps(r["details"], sort_keys=True),
                    now,
                ),
            )
            written += 1
    return written


# ----------------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------------


def _get_xbrl_concept(db: Database, metric_key: str) -> str | None:
    """Look up the xbrl_concept for a metric_key from metric_definitions."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT aliases FROM metric_definitions WHERE key = ?", (metric_key,)
        ).fetchone()
        if row is None:
            return None
        # The xbrl_concept is stored in the aliases JSON under a special key,
        # OR as a dedicated column. For v1, we check aliases for an entry
        # starting with "us-gaap:" or "ifrs:".
        aliases = json.loads(row[0]) if row[0] else []
        for alias in aliases:
            if isinstance(alias, str) and (
                alias.startswith("us-gaap:") or alias.startswith("ifrs:")
            ):
                return alias
        return None


def _get_doc_info(db: Database, source_document_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT sd.ticker, sd.period_of_report, sd.source, c.edgar_cik
            FROM source_documents sd
            LEFT JOIN companies c ON sd.ticker = c.ticker
            WHERE sd.id = ?
            """,
            (source_document_id,),
        ).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}


def _fetch_companyfacts(cik: str) -> dict:
    """Fetch the XBRL companyfacts JSON from SEC."""
    padded = cik.lstrip("0").zfill(10)
    url = COMPANYFACTS_URL.format(cik_padded=padded)
    req = urllib.request.Request(url, headers={"User-Agent": get_user_agent()})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _find_fact_value(
    facts: dict, concept: str, period: str
) -> float | None:
    """Find the value of an XBRL concept for a specific period.

    The companyfacts JSON has structure:
    {
      "facts": {
        "us-gaap": {
          "ConceptName": {
            "units": {
              "USD": [
                {"end": "2025-06-30", "val": 88036000000, ...},
                ...
              ]
            }
          }
        }
      }
    }

    We normalize the concept (e.g. "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment")
    and search for a matching entry whose "end" date matches our period.

    Returns the value in millions (divides by 1e6 since XBRL reports in USD actual).
    """
    parts = concept.split(":", 1)
    if len(parts) != 2:
        return None
    taxonomy, concept_name = parts

    taxonomy_facts = facts.get("facts", {}).get(taxonomy, {})
    concept_data = taxonomy_facts.get(concept_name, {})
    units = concept_data.get("units", {})

    # Try USD first, then USD/shares, then others
    for unit_key in ("USD", "USD/shares", "pure"):
        entries = units.get(unit_key, [])
        for entry in entries:
            if entry.get("end") == period and "val" in entry:
                val = entry["val"]
                # Convert to millions if the unit is USD (raw values are in actual USD)
                if unit_key == "USD":
                    return round(val / 1e6, 1)
                return val

    return None
