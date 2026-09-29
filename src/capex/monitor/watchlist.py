"""Which companies the watcher follows, and which filing to expect.

The `watchlist` table (migration 0011) is the runtime switchboard the
admin panel edits: watch on/off plus the quarterly and annual form per
company. sync_watchlist() seeds missing rows from coverage.yaml's
filing_cadence and never overwrites a row that already exists, so edits
made at runtime survive every deploy.
"""
from __future__ import annotations

from typing import Any

from ..db import Database
from ..extract.coverage import get_all_tickers, get_company_treatment
from .clock import utc_iso

QUARTERLY_FORMS = ("10-Q", "6-K", "HK-IR")
ANNUAL_FORMS = ("10-K", "20-F", "HK-AR")


def default_forms(ticker: str) -> tuple[str | None, str | None]:
    """(quarterly_form, annual_form) implied by coverage.yaml."""
    company = get_company_treatment(ticker)
    cadence = company.filing_cadence if company else {}
    annual = cadence.get("annual")
    quarterly = cadence.get("quarterly")
    if quarterly is None and annual == "20-F":
        # Foreign private issuers publish quarterly results as 6-K
        # press releases; the 20-F only covers the full year.
        quarterly = "6-K"
    if quarterly is None and annual == "HK-AR":
        quarterly = "HK-IR"
    return (
        quarterly if quarterly in QUARTERLY_FORMS else None,
        annual if annual in ANNUAL_FORMS else None,
    )


def sync_watchlist(db: Database | None = None) -> int:
    """Insert a row for every covered company that has none. Returns count."""
    db = db or Database()
    now = utc_iso()
    inserted = 0
    with db.mutating() as conn:
        sources = {
            r["ticker"]: r["preferred_source"]
            for r in conn.execute("SELECT ticker, preferred_source FROM companies")
        }
        existing = {r["ticker"] for r in conn.execute("SELECT ticker FROM watchlist")}
        for ticker in get_all_tickers():
            if ticker in existing or ticker not in sources:
                continue
            quarterly, annual = default_forms(ticker)
            # HKEX automation isn't verified end to end yet: start unwatched.
            watch = 0 if sources[ticker] == "hkex" else 1
            conn.execute(
                "INSERT INTO watchlist (ticker, watch, quarterly_form, annual_form, "
                "notes, updated_at) VALUES (?, ?, ?, ?, NULL, ?)",
                (ticker, watch, quarterly, annual, now),
            )
            inserted += 1
    return inserted


def get_entry(ticker: str, db: Database) -> dict[str, Any] | None:
    """The watchlist row for `ticker`, or None."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT ticker, watch, quarterly_form, annual_form, notes "
            "FROM watchlist WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    return dict(row) if row else None


def watched_tickers(db: Database) -> set[str]:
    with db.connect() as conn:
        return {r["ticker"] for r in conn.execute("SELECT ticker FROM watchlist WHERE watch = 1")}


def expected_form(ticker: str, fiscal_date_ending: str, db: Database) -> str | None:
    """The form that should carry results for the period ending then.

    6-K filers announce every quarter — the fiscal year-end one included —
    in a 6-K press release; their 20-F follows weeks later and is picked
    up by the filings sweep. The exception is a company whose fiscal Q4 is
    derived from the annual report (coverage.yaml `filing_cadence.fiscal_q4:
    annual`, e.g. BABA): its year-end period expects the annual form.
    Everyone else files the annual form for the fiscal year-end period and
    the quarterly form otherwise.
    """
    entry = get_entry(ticker, db)
    if entry is None:
        quarterly, annual = default_forms(ticker)
    else:
        quarterly, annual = entry["quarterly_form"], entry["annual_form"]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT fiscal_year_end_month FROM companies WHERE ticker = ?", (ticker,)
        ).fetchone()
    fye_month = row["fiscal_year_end_month"] if row else 12
    year_end = int(fiscal_date_ending[5:7]) == fye_month
    if quarterly == "6-K":
        company = get_company_treatment(ticker)
        q4_from_annual = bool(company) and company.filing_cadence.get("fiscal_q4") == "annual"
        return (annual or "6-K") if (year_end and q4_from_annual) else "6-K"
    if year_end:
        return annual or quarterly
    return quarterly or annual
