"""SEC EDGAR polling for the watcher pipeline (see pipeline.py).

poll_for_row() answers one question for a calendar row: has the filing
for this period appeared yet? It separates four outcomes that used to
collapse into "nothing new": a hit, not filed yet, SEC unreachable, and
no automated fetcher for this form.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from ..db import Database
from ..fetch.errors import SourceUnavailableError
from ..fetch.sec import SEC_FORM_TYPES, get_submissions, list_filings

# A filing's EDGAR reportDate should equal the calendar's fiscal period
# end; allow for 52/53-week years and provider rounding.
MATCH_WINDOW_DAYS = 10

HIT, NOT_YET, ERROR, UNSUPPORTED = "hit", "not_yet", "error", "unsupported"


@dataclass
class PollResult:
    status: str                       # hit | not_yet | error | unsupported
    filing: dict[str, str] | None = None
    detail: str = ""


def edgar_cik(ticker: str, db: Database) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT edgar_cik FROM companies WHERE ticker = ?", (ticker,)
        ).fetchone()
    return row["edgar_cik"] if row and row["edgar_cik"] else None


def submissions_for(ticker: str, db: Database, cache: dict[str, Any]) -> dict:
    """EDGAR submissions for `ticker`, fetched once per run via `cache`."""
    if ticker not in cache:
        cik = edgar_cik(ticker, db)
        if cik is None:
            raise LookupError(f"{ticker} has no EDGAR CIK")
        cache[ticker] = get_submissions(cik)
    return cache[ticker]


def poll_for_row(
    ticker: str,
    form_type: str | None,
    fiscal_date_ending: str,
    *,
    db: Database,
    cache: dict[str, Any] | None = None,
) -> PollResult:
    """Has `ticker` filed its `form_type` for the period ending then?"""
    if form_type not in SEC_FORM_TYPES:
        return PollResult(UNSUPPORTED, detail=f"no automated fetcher for {form_type or '?'} yet")
    try:
        submissions = submissions_for(ticker, db, cache if cache is not None else {})
    except LookupError as e:
        return PollResult(ERROR, detail=str(e))
    except SourceUnavailableError as e:
        return PollResult(ERROR, detail=str(e))
    target = date.fromisoformat(fiscal_date_ending)
    for filing in list_filings(submissions, form_type):  # amendments excluded
        report_date = filing.get("reportDate")
        if not report_date:
            continue
        if abs((date.fromisoformat(report_date) - target).days) <= MATCH_WINDOW_DAYS:
            return PollResult(HIT, filing=filing)
    return PollResult(NOT_YET)


def already_in_db(
    ticker: str,
    form_type: str,
    period: str,
    *,
    db: Database,
    accession: str | None = None,
) -> bool:
    """Is this filing already in source_documents (by accession or period)?"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM source_documents "
            "WHERE (accession_number = ? AND accession_number != '') "
            "OR (ticker = ? AND form_type = ? AND period_of_report = ?)",
            (accession or "", ticker, form_type, period),
        ).fetchone()
    return row is not None
