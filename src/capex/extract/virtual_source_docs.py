"""Virtual source_documents for restated comparative periods.

When the LLM extractor reads a later 10-K / 10-Q that contains a
prior-period comparative column, the extraction for that prior
period logically belongs to the prior period's `fiscal_year` and
`period_of_report` — but its `source_url` + `filing_date` +
`accession_number` should cite the *restating* filing so Excel
comments, chart hover, and audit trails point at the filing where
the restated figure appeared.

The UNIQUE(ticker, form_type, period_of_report) constraint on
`source_documents` prevents us from reusing the original filing's
slot. We resolve this by creating a *virtual* `source_documents`
row with:

- `form_type='6-K'` (valid per the CHECK constraint, semantically
  reasonable — a restated comparative IS a supplementary disclosure
  of a prior period; a collision with a real 6-K at the same
  period_of_report is extremely unlikely given the `raw_path`
  differentiator)
- `period_of_report` = the comparative period's FYE date
- `fiscal_year` = the comparative period's fiscal year
- `raw_path` = `restated-virtual://<ticker>/<fiscal_year>`
- `source_url` / `accession_number` / `filing_date` copied from the
  *restating* filing so citations auto-rewire via the existing Excel
  + chart flow

The `filing_date DESC` tiebreaker in every downstream selector then
promotes the extraction written against this virtual row over the
original row for the prior fiscal year on next read.
"""
from __future__ import annotations

import sqlite3

LAST_DAY_OF_MONTH = {
    1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
    7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
}


def ensure_restated_source_doc(
    conn: sqlite3.Connection,
    ticker: str,
    fiscal_year: int,
    restating_sd_id: int,
    now: str,
    *,
    period_of_report: str | None = None,
) -> int:
    """Get/create a virtual source_documents row for a restated period.

    Idempotent — returns the existing row's id if already created.

    `period_of_report` — optional ISO date (YYYY-MM-DD) for quarterly
    restatements. When omitted, defaults to the company's fiscal-
    year-end date derived from `fiscal_year` + `companies.fiscal_year_end_month`
    (the standard annual comparative case).
    """
    restating = conn.execute(
        "SELECT ticker, form_type, filing_date, period_of_report, "
        "       fiscal_year, source_url, accession_number, source "
        "FROM source_documents WHERE id = ?",
        (restating_sd_id,),
    ).fetchone()
    if not restating:
        raise ValueError(
            f"restating source_doc {restating_sd_id} not found"
        )
    if period_of_report:
        period_end = period_of_report
    else:
        co = conn.execute(
            "SELECT fiscal_year_end_month FROM companies WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        fye_month = (co["fiscal_year_end_month"] or 12) if co else 12
        last_day = LAST_DAY_OF_MONTH[fye_month]
        period_end = f"{fiscal_year:04d}-{fye_month:02d}-{last_day:02d}"

    # Virtual raw_path encodes both period + accession for traceability,
    # but the real source_documents constraint is UNIQUE(ticker, form_type,
    # period_of_report) — dedup on that, not on raw_path, or a second
    # restating filing for the same prior period (different accession)
    # will pass this check and then crash on the INSERT below.
    virt_raw = (
        f"restated-virtual://{ticker}/{period_end}"
        f"/{restating['accession_number'] or 'unknown'}"
    )
    existing = conn.execute(
        "SELECT id FROM source_documents "
        "WHERE ticker=? AND form_type='6-K' AND period_of_report=?",
        (ticker, period_end),
    ).fetchone()
    if existing:
        return existing["id"]

    src_value = (restating["source"] or "sec_edgar") or "sec_edgar"
    cur = conn.execute(
        """
        INSERT INTO source_documents
            (ticker, form_type, filing_date, period_of_report, fiscal_year,
             period_token, sha256, raw_path, source, source_url,
             accession_number, fetched_at, fetcher_version, protocol_version)
        VALUES (?, '6-K', ?, ?, ?, 'AR', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker, restating["filing_date"],
            period_end, fiscal_year,
            f"restated-{ticker}-{period_end}-{restating['accession_number'] or ''}",
            virt_raw,
            src_value,
            restating["source_url"] or "",
            restating["accession_number"] or "",
            now, "llm-dual-agent@0.1.0", "0.1.0-draft",
        ),
    )
    return cur.lastrowid
