"""Fetch dispatcher: ticker → fetcher → file + sidecar + DB row.

Public API:
    fetch_filing(ticker, form_type) → dict
        Read-only path. Looks up the company, picks the per-source fetcher,
        downloads the bytes, writes the sidecar, returns the metadata. Does
        NOT touch the database.

    fetch_and_record(ticker, form_type) → dict
        The version that wires into the DB. Calls fetch_filing(), then
        inserts a source_documents row + audit_log row in one mutating()
        block. Returns the metadata dict augmented with the new row's id.

For Phase 2a, only the SEC fetcher is wired. HKEX raises
NotImplementedError until Phase 2b lands. Dual-listed companies (BABA,
BIDU, GDS) currently use SEC because their preferred_source is sec_edgar
in _identity.yaml — the timeliness-based dual dispatcher also waits for
Phase 2b.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..db import Database
from .errors import FormTypeMismatchError, UnknownCompanyError
from .hkex import HKEX_FORM_TYPES, fetch_latest as hkex_fetch_latest
from .sec import SEC_FORM_TYPES, fetch_latest as sec_fetch_latest
from .sidecar import write_sidecar

ACTOR_FETCH = "fetch-company-report@0.1.0"


def fetch_filing(ticker: str, form_type: str, db: Database | None = None) -> dict[str, Any]:
    """Fetch the latest filing matching (ticker, form_type) from the regulator.

    Writes the bytes to data/_sources/<TICKER>/_raw/, writes the sidecar JSON,
    and returns the metadata dict. Does NOT touch the database — call
    fetch_and_record() if you want a DB row.
    """
    db = db or Database()
    company = _lookup_company(db, ticker)

    source = company["preferred_source"]

    # Form-type-based dispatch: SEC forms → sec.py, HKEX forms → hkex.py.
    # For dual-listed companies, the form_type itself determines the source.
    if form_type in SEC_FORM_TYPES:
        if source == "hkex" and not company.get("edgar_cik"):
            raise FormTypeMismatchError(ticker, form_type, HKEX_FORM_TYPES)
        cik = company["edgar_cik"]
        if not cik:
            raise FormTypeMismatchError(
                ticker, form_type, HKEX_FORM_TYPES
            )
        metadata = sec_fetch_latest(ticker, cik, form_type)
    elif form_type in HKEX_FORM_TYPES:
        hk_code = company.get("hkex_stock_code")
        if not hk_code:
            raise FormTypeMismatchError(ticker, form_type, SEC_FORM_TYPES)
        metadata = hkex_fetch_latest(ticker, hk_code, form_type)
    else:
        all_supported = SEC_FORM_TYPES + HKEX_FORM_TYPES
        raise FormTypeMismatchError(ticker, form_type, all_supported)

    # Write the sidecar next to the file. The fetcher gave us a repo-relative
    # path; resolve it back to absolute for the sidecar writer.
    from .sec import REPO_ROOT  # local import to avoid circular at module load
    raw_path_abs = REPO_ROOT / metadata["raw_path"]
    write_sidecar(raw_path_abs, metadata)

    return metadata


def fetch_and_record(
    ticker: str, form_type: str, db: Database | None = None
) -> dict[str, Any]:
    """Fetch + write source_documents row + audit_log row, all atomic."""
    db = db or Database()
    metadata = fetch_filing(ticker, form_type, db=db)

    # Compute derived fields needed for the source_documents row.
    company = _lookup_company(db, ticker)
    fye_month = company["fiscal_year_end_month"]
    period_token = _compute_period_token(form_type, metadata["period_of_report"], fye_month)
    fiscal_year = _compute_fiscal_year(metadata["period_of_report"], fye_month)

    with db.mutating() as conn:
        # Idempotent on sha256 — same filing fetched twice produces no new rows.
        existing = conn.execute(
            "SELECT id FROM source_documents WHERE sha256 = ?", (metadata["sha256"],)
        ).fetchone()
        if existing:
            metadata["id"] = existing[0]
            metadata["already_existed"] = True
            return metadata

        cur = conn.execute(
            """
            INSERT INTO source_documents (
                ticker, form_type, filing_date, period_of_report,
                fiscal_year, period_token, sha256, raw_path, canonical_path,
                source, source_url, accession_number,
                fetched_at, fetcher_version, protocol_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                metadata["ticker"],
                metadata["form_type"],
                metadata["filing_date"],
                metadata["period_of_report"],
                fiscal_year,
                period_token,
                metadata["sha256"],
                metadata["raw_path"],
                metadata["source"],
                metadata["source_url"],
                metadata.get("accession_number"),
                metadata["fetched_at"],
                metadata["fetcher_version"],
                metadata["protocol_version"],
            ),
        )
        row_id = cur.lastrowid
        metadata["id"] = row_id
        metadata["already_existed"] = False

        conn.execute(
            """
            INSERT INTO audit_log (ts, actor, action, target_table, target_id, payload)
            VALUES (?, ?, 'source_document_inserted', 'source_documents', ?, ?)
            """,
            (
                _now_iso(),
                ACTOR_FETCH,
                row_id,
                json.dumps(
                    {
                        "ticker": metadata["ticker"],
                        "form_type": metadata["form_type"],
                        "period_of_report": metadata["period_of_report"],
                        "sha256": metadata["sha256"],
                        "raw_path": metadata["raw_path"],
                    },
                    sort_keys=True,
                ),
            ),
        )

    return metadata


# ----------------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------------


def _lookup_company(db: Database, ticker: str) -> dict[str, Any]:
    """Read a row from companies. Raises UnknownCompanyError if not present."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT ticker, name, preferred_source, edgar_cik, hkex_stock_code, fiscal_year_end_month "
            "FROM companies WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        if row is None:
            raise UnknownCompanyError(ticker)
        return {k: row[k] for k in row.keys()}


def _compute_period_token(form_type: str, period_of_report: str, fye_month: int) -> str:
    """Derive AR / Q1 / Q2 / Q3 / H1 / H2 from form + period + FYE month.

    Period derivation rules from skills/organize-sources/SKILL.md:
    * 10-K, 20-F, HK-AR → AR (always)
    * 10-Q → Q1, Q2, Q3 (Q4 rolls into the annual; raise if math says Q4)
    * HK-IR → H1 or H2 based on which fiscal half the period ends in
    """
    if form_type in ("10-K", "20-F", "HK-AR"):
        return "AR"

    period_month = int(period_of_report.split("-")[1])

    if form_type == "10-Q":
        # Months elapsed in fiscal year (1..12). Q1=1-3, Q2=4-6, Q3=7-9, Q4=10-12.
        elapsed = ((period_month - fye_month - 1) % 12) + 1
        if elapsed <= 3:
            return "Q1"
        if elapsed <= 6:
            return "Q2"
        if elapsed <= 9:
            return "Q3"
        # Elapsed > 9 means we landed in Q4, which 10-Q doesn't cover.
        from .errors import FetchError
        raise FetchError(
            f"period_of_report {period_of_report} computes to fiscal Q4 for "
            f"{form_type} (FYE month {fye_month}); 10-Q does not cover Q4"
        )

    if form_type == "HK-IR":
        elapsed = ((period_month - fye_month - 1) % 12) + 1
        return "H1" if elapsed <= 6 else "H2"

    raise ValueError(f"unknown form_type for period derivation: {form_type}")


def _compute_fiscal_year(period_of_report: str, fye_month: int) -> int:
    """Compute the fiscal year a period_of_report belongs to.

    A company with fye_month=6 (Microsoft, June) and period_of_report
    2025-06-30 is FY2025. A 10-Q with period 2025-09-30 (start of next
    fiscal year) is FY2026.

    Rule: the fiscal year is named for the calendar year in which the
    fiscal year *ends*. If period_month <= fye_month, fiscal_year =
    calendar_year. Otherwise fiscal_year = calendar_year + 1.
    """
    parts = period_of_report.split("-")
    year = int(parts[0])
    month = int(parts[1])
    if month <= fye_month:
        return year
    return year + 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
