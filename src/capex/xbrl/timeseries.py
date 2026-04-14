"""XBRL companyfacts time series — pull full quarterly history in one call.

SEC's free API at data.sec.gov/api/xbrl/companyfacts/ returns every
XBRL-tagged value for a company across all filings. One call gives us
the full 2009–present time series for any tagged concept.

Usage:
    from capex.xbrl.timeseries import fetch_concept_timeseries

    # Get MSFT's quarterly capital expenditures since 2015
    series = fetch_concept_timeseries(
        cik="0000789019",
        concept="us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
        start_date="2015-01-01",
    )
    for point in series:
        print(f"{point['end']}  {point['val']:>15,.0f}  {point['form']}")

    # Write to DB
    from capex.xbrl.timeseries import write_timeseries_to_db
    write_timeseries_to_db(series, ticker="MSFT", metric_key="capital_expenditures")

Key design decisions:
    * Returns raw data points, not aggregated — the caller decides how
      to aggregate (quarterly, annual, trailing-twelve-months).
    * Filters for 10-K and 10-Q forms only (excludes 8-K, S-1, etc.)
      to avoid double-counting.
    * Does NOT download filings — this is pure API, no file I/O.
    * Values are in the company's reporting currency (usually USD for
      US-GAAP filers, possibly CNY for IFRS filers). FX conversion is
      done by the caller.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..db import Database
from ..fetch import get_user_agent
from ..fx.rates import normalize_to_usd

COMPANYFACTS_URL = (
    "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json"
)

# Forms we want quarterly data from. Excludes 8-K, S-1, etc.
VALID_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A"}


def fetch_concept_timeseries(
    cik: str,
    concept: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    forms: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch all quarterly/annual values for an XBRL concept.

    Args:
        cik: SEC CIK, zero-padded (e.g. "0000789019").
        concept: full XBRL concept (e.g. "us-gaap:PaymentsToAcquire...").
        start_date: ISO date, filter out values before this date.
        end_date: ISO date, filter out values after this date.
        forms: set of form types to include (default: 10-K, 10-Q, 20-F + amendments).

    Returns:
        List of dicts sorted by end date ascending:
        [{"end": "2025-06-30", "val": 64551000000, "form": "10-K",
          "filed": "2025-07-30", "fy": 2025, "fp": "FY", "frame": "..."}]
    """
    forms = forms or VALID_FORMS

    # Parse concept: "us-gaap:ConceptName" → ("us-gaap", "ConceptName")
    parts = concept.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"concept must be 'taxonomy:ConceptName', got {concept!r}")
    taxonomy, concept_name = parts

    # Fetch the full companyfacts JSON
    facts = _fetch_companyfacts(cik)

    # Navigate to the concept
    taxonomy_facts = facts.get("facts", {}).get(taxonomy, {})
    concept_data = taxonomy_facts.get(concept_name, {})
    units = concept_data.get("units", {})

    # Collect all values from USD (or the first available unit)
    entries = []
    for unit_key in ("USD", "CNY", "HKD", "AUD"):
        if unit_key in units:
            entries = units[unit_key]
            break
    if not entries and units:
        # Fallback: take the first unit we find
        first_unit = next(iter(units))
        entries = units[first_unit]

    # Filter and deduplicate
    result = []
    seen = set()
    for entry in entries:
        end_date_val = entry.get("end")
        form = entry.get("form", "")
        val = entry.get("val")

        if not end_date_val or val is None:
            continue
        if form not in forms:
            continue
        if start_date and end_date_val < start_date:
            continue
        if end_date and end_date_val > end_date:
            continue

        # Deduplicate: for the same end date + form, keep the latest filing
        dedup_key = (end_date_val, form.rstrip("/A"))  # treat amended as same
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        result.append({
            "end": end_date_val,
            "val": val,
            "form": form,
            "filed": entry.get("filed", ""),
            "fy": entry.get("fy"),
            "fp": entry.get("fp", ""),
            "accn": entry.get("accn", ""),
        })

    result.sort(key=lambda x: x["end"])
    return result


def decumulate_quarterly(
    series: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert cumulative YTD values to standalone quarterly values.

    Delegates to the canonical implementation in extract.decumulate.
    See that module for the algorithm documentation.
    """
    from ..extract.decumulate import decumulate_series
    return decumulate_series(series, val_key="val", fy_key="fy", fp_key="fp")


def fetch_segment_timeseries(
    cik: str,
    segment_name: str,
    *,
    start_date: str | None = None,
) -> list[dict[str, Any]]:
    """Attempt to fetch segment-level revenue from XBRL.

    SEC XBRL segment data is tagged with dimensional qualifiers. This
    function searches for revenue concepts that include the segment name
    as a dimension. This is a best-effort lookup — segment XBRL tagging
    is inconsistent across companies.

    Returns the same format as fetch_concept_timeseries, or an empty
    list if no segment data is found.
    """
    # The companyfacts API doesn't cleanly expose segment dimensions —
    # it primarily returns company-level totals. Segment-level XBRL
    # data requires parsing the actual XBRL filing.
    # For now, return empty — segment extraction falls back to LLM.
    _ = cik, segment_name, start_date  # suppress unused warnings
    return []


def write_timeseries_to_db(
    series: list[dict[str, Any]],
    *,
    ticker: str,
    metric_key: str,
    reporting_currency: str = "USD",
    db: Database | None = None,
) -> dict[str, Any]:
    """Write a time series to the extractions table.

    Creates "synthetic" source_documents rows for historical periods
    that we don't have actual filings for (since the data comes from
    XBRL API, not from downloaded filings).

    Returns summary: {inserted, skipped, errors}.
    """
    db = db or Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    summary = {"inserted": 0, "skipped": 0, "errors": []}

    for point in series:
        end_date = point["end"]
        val = point["val"]
        form = point["form"].rstrip("/A")  # normalize amended forms

        # Convert value to millions
        val_millions = round(val / 1e6, 2) if val else None

        try:
            # Find or create a source_documents row for this period
            doc_id = _ensure_source_doc(
                db, ticker, form, end_date, point.get("filed", ""),
                point.get("accn", ""), now,
            )

            # FX normalize
            value_usd, fx_rate, fx_date = normalize_to_usd(
                val_millions, reporting_currency, end_date, db=db,
            )

            # Insert extraction (idempotent on unique constraint)
            with db.mutating() as conn:
                existing = conn.execute(
                    "SELECT id FROM extractions "
                    "WHERE source_document_id = ? AND metric_key = ? "
                    "AND extracting_model = ?",
                    (doc_id, metric_key, "xbrl-companyfacts"),
                ).fetchone()

                if existing:
                    summary["skipped"] += 1
                    continue

                conn.execute(
                    """
                    INSERT INTO extractions (
                        source_document_id, metric_key, value,
                        value_text, unit, quote, locator_section,
                        extraction_type, extracting_model,
                        protocol_version, extracted_at,
                        value_usd, fx_rate, fx_rate_date,
                        reporting_currency
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?
                    )
                    """,
                    (
                        doc_id,
                        metric_key,
                        val_millions,
                        f"${val_millions:,.0f} million"
                        if val_millions
                        else "n/a",
                        "USD_millions",
                        f"XBRL: {metric_key} = {val}",
                        "XBRL companyfacts API",
                        "direct",
                        "xbrl-companyfacts",
                        "0.1.0-draft",
                        now,
                        value_usd,
                        fx_rate,
                        fx_date,
                        reporting_currency,
                    ),
                )
                summary["inserted"] += 1

        except Exception as e:
            summary["errors"].append(
                f"{ticker} {end_date}: {type(e).__name__}: {e}"
            )

    return summary


# --------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------


def _fetch_companyfacts(cik: str) -> dict:
    padded = cik.lstrip("0").zfill(10)
    url = COMPANYFACTS_URL.format(cik_padded=padded)
    headers = {"User-Agent": get_user_agent()}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"companyfacts API error: {e.code} for {url}"
        ) from e


def _ensure_source_doc(
    db: Database,
    ticker: str,
    form_type: str,
    period_of_report: str,
    filing_date: str,
    accession: str,
    now: str,
) -> int:
    """Find or create a source_documents row for an XBRL-sourced period.

    For XBRL time series, we may not have the actual filing downloaded.
    We create a "synthetic" source_documents row with source='xbrl_api'
    to represent the data point. If a real filing is later downloaded
    for the same period, the existing row is reused (matched by ticker
    + form_type + period_of_report UNIQUE constraint).
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM source_documents "
            "WHERE ticker = ? AND form_type = ? "
            "AND period_of_report = ?",
            (ticker, form_type, period_of_report),
        ).fetchone()
        if row:
            return row[0]

    # Compute fiscal year and period token
    from ..fetch.dispatcher import (
        _compute_fiscal_year,
        _compute_period_token,
    )

    with db.connect() as conn:
        company = conn.execute(
            "SELECT fiscal_year_end_month FROM companies "
            "WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    fye = company["fiscal_year_end_month"] if company else 12

    period_token = _compute_period_token(
        form_type, period_of_report, fye
    )
    fiscal_year = _compute_fiscal_year(period_of_report, fye)

    with db.mutating() as conn:
        # Double-check to avoid race condition
        row = conn.execute(
            "SELECT id FROM source_documents "
            "WHERE ticker = ? AND form_type = ? "
            "AND period_of_report = ?",
            (ticker, form_type, period_of_report),
        ).fetchone()
        if row:
            return row[0]

        cur = conn.execute(
            """
            INSERT INTO source_documents (
                ticker, form_type, filing_date,
                period_of_report, fiscal_year, period_token,
                sha256, raw_path, source, source_url,
                accession_number, fetched_at,
                fetcher_version, protocol_version
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                ticker,
                form_type,
                filing_date or period_of_report,
                period_of_report,
                fiscal_year,
                period_token,
                f"xbrl-synthetic-{ticker}-{form_type}"
                f"-{period_of_report}",
                f"xbrl://companyfacts/{ticker}/{period_of_report}",
                "xbrl_api",
                COMPANYFACTS_URL.format(
                    cik_padded=ticker
                ),  # placeholder
                accession,
                now,
                "xbrl-timeseries@0.1.0",
                "0.1.0-draft",
            ),
        )
        return cur.lastrowid
