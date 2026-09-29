"""SEC EDGAR polling for the watcher pipeline (see pipeline.py).

poll_for_row() answers one question for a calendar row: has the filing
for this period appeared yet? It separates four outcomes that used to
collapse into "nothing new": a hit, not filed yet, SEC unreachable, and
no automated fetcher for this form.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..db import Database
from ..fetch import sec_6k
from ..fetch.errors import SourceUnavailableError
from ..fetch.sec import SEC_FORM_TYPES, get_submissions, list_filings

# A filing's EDGAR reportDate should equal the calendar's fiscal period
# end; allow for 52/53-week years and provider rounding.
MATCH_WINDOW_DAYS = 10

HIT, NOT_YET, ERROR, UNSUPPORTED, KNOWN = "hit", "not_yet", "error", "unsupported", "known"


@dataclass
class PollResult:
    status: str                       # hit | not_yet | error | unsupported | known
    filing: dict[str, str] | None = None
    detail: str = ""
    # 6-Ks examined and found not to be earnings releases (filing, reason):
    # remembered so they are never downloaded again.
    ignored: list[tuple[dict[str, str], str]] = field(default_factory=list)


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
    report_date: str | None = None,
) -> PollResult:
    """Has `ticker` filed its `form_type` for the period ending then?"""
    if form_type == "6-K":
        return poll_6k(ticker, fiscal_date_ending, db=db, cache=cache, report_date=report_date)
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


def poll_6k(
    ticker: str,
    fiscal_date_ending: str,
    *,
    db: Database,
    cache: dict[str, Any] | None = None,
    report_date: str | None = None,
) -> PollResult:
    """Look for the 6-K earnings release covering the quarter (see sec_6k)."""
    cik = edgar_cik(ticker, db)
    if cik is None:
        return PollResult(ERROR, detail=f"{ticker} has no EDGAR CIK")
    with db.connect() as conn:
        seen = {r[0] for r in conn.execute(
            "SELECT accession_number FROM filing_events WHERE ticker = ? AND status = 'ignored'",
            (ticker,),
        )}
    try:
        submissions = submissions_for(ticker, db, cache if cache is not None else {})
        search = sec_6k.find_earnings_release(
            cik, date.fromisoformat(fiscal_date_ending), submissions,
            report_date=date.fromisoformat(report_date) if report_date else None,
            skip=seen,
        )
    except SourceUnavailableError as e:
        return PollResult(ERROR, detail=str(e))
    if search.release is None:
        return PollResult(NOT_YET, ignored=search.ignored)
    release = search.release
    with db.connect() as conn:
        # (ticker, 6-K, period) is unique in source_documents, and a later
        # filing's comparative column may already hold it as a
        # restated-virtual:// row; either way the period is covered.
        known = conn.execute(
            "SELECT id, raw_path FROM source_documents WHERE accession_number = ? "
            "OR (ticker = ? AND form_type = '6-K' AND period_of_report = ?)",
            (release["accessionNumber"], ticker, release["reportDate"]),
        ).fetchone()
    if known:
        how = (" from a later filing's comparatives"
               if str(known["raw_path"]).startswith("restated-virtual://") else "")
        detail = f"period already recorded{how} (source_documents id {known['id']})"
        return PollResult(KNOWN, filing=release, ignored=search.ignored, detail=detail)
    return PollResult(HIT, filing=release, ignored=search.ignored)


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
