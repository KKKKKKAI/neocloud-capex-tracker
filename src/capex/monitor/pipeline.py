"""The watcher pipeline: calendar row → filing event → fetch → extract → outputs.

One run — `capex monitor --catch-up`, or the scheduler's watcher job:

1. Sync the watchlist; mark calendar rows stale whose filing never came.
2. Discover: poll SEC for every due calendar row of a watched company.
   A match becomes a `filing_events` row ('discovered') linked to the row.
3. Optionally sweep: queue recent periodic filings that have no calendar
   row at all (Alpha Vantage misses some).
4. Process up to llm.max_filings_per_run events, oldest filing first:
   fetch the exact accession, then extract (XBRL first, LLM for the
   rest). Every metric resolved → 'extracted'; retryable gaps → 'partial'
   (retried with backoff); errors back off too and end as 'failed' after
   watcher.max_attempts.
5. Regenerate outputs when anything was extracted; email subscribers
   about fresh filings.

All state lives in fiscal_calendar + filing_events, so each run resumes
where the last one stopped. A fatal LLM error (auth, usage limit, budget)
stops the run at once and never counts against the filing; a usage limit
also pauses LLM calls until it resets.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .. import settings
from ..adapters.errors import (
    FATAL_LLM_ERRORS,
    LLMAuthError,
    LLMBudgetError,
    LLMConfigError,
    LLMUsageLimitError,
)
from ..db import Database
from ..fetch.dispatcher import fetch_and_record
from ..fetch.errors import SourceUnavailableError
from ..fetch.sec import SEC_FORM_TYPES, list_filings
from .clock import today_eastern, utc_iso, utc_now
from .watcher import (
    ERROR,
    HIT,
    MATCH_WINDOW_DAYS,
    NOT_YET,
    PollResult,
    already_in_db,
    poll_for_row,
    submissions_for,
)
from .watchlist import get_entry, sync_watchlist, watched_tickers

EXIT_OK, EXIT_ERROR, EXIT_PARTIAL, EXIT_DEFERRED, EXIT_AUTH = 0, 1, 3, 75, 77

POLL_ERROR_BACKOFF = timedelta(hours=1)
UNSUPPORTED_BACKOFF = timedelta(days=1)
DEFAULT_STALE_DAYS = 45
DEFAULT_PAUSE = timedelta(hours=1)

# Extraction statuses: resolved, needs a human (not retried), or retryable.
RESOLVED = frozenset({"success", "no_extractor"})
NEEDS_REVIEW = frozenset({"needs_verification", "needs_review"})


def backoff(attempts: int) -> timedelta:
    """15 min, 30 min, 1 h, 2 h, ... capped at 12 h."""
    return min(timedelta(minutes=15) * 2 ** max(attempts - 1, 0), timedelta(hours=12))


@dataclass
class EventOutcome:
    event_id: int
    ticker: str
    form_type: str
    period: str | None
    filing_date: str
    status: str                  # extracted | partial | failed | retry
    metrics_extracted: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


@dataclass
class RunSummary:
    dry_run: bool = False
    due: int = 0
    polls: dict[str, int] = field(default_factory=dict)
    discovered: list[str] = field(default_factory=list)
    swept: list[str] = field(default_factory=list)
    stale: int = 0
    outcomes: list[EventOutcome] = field(default_factory=list)
    stopped: str | None = None       # why a fatal LLM error ended the run
    stop_kind: str | None = None     # auth | deferred | config
    outputs_regenerated: bool = False
    notify: dict[str, Any] | None = None

    def exit_code(self) -> int:
        if self.stop_kind == "auth":
            return EXIT_AUTH
        if self.stop_kind == "deferred":
            return EXIT_DEFERRED
        if self.stop_kind == "config":
            return EXIT_ERROR
        if any(o.status in ("partial", "failed") for o in self.outcomes):
            return EXIT_PARTIAL
        return EXIT_OK


# ---- calendar maintenance ----------------------------------------------------

def mark_stale_rows(db: Database, today: date) -> int:
    """'upcoming' rows whose filing never appeared → 'stale'. Returns count."""
    windows = settings.get("watcher.stale_after_days", db)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, form_type, report_date FROM fiscal_calendar WHERE status = 'upcoming'"
        ).fetchall()
    now = utc_iso()
    stale = []
    for r in rows:
        days = windows.get(r["form_type"] or "", DEFAULT_STALE_DAYS)
        if date.fromisoformat(r["report_date"]) + timedelta(days=days) < today:
            reason = (f"no {r['form_type'] or 'filing'} within {days} days of the "
                      f"{r['report_date']} report date")
            stale.append((reason, now, r["id"]))
    if stale:
        with db.mutating() as conn:
            conn.executemany(
                "UPDATE fiscal_calendar SET status = 'stale', last_error = ?, updated_at = ? "
                "WHERE id = ?",
                stale,
            )
    return len(stale)


def select_due_rows(
    db: Database,
    *,
    today: date,
    now: datetime,
    since: str | None = None,
    tickers: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Watched, reported, not yet found, not backing off, within lookback."""
    floor = (today - timedelta(days=settings.get("watcher.lookback_days", db))).isoformat()
    if since and since > floor:
        floor = since
    watched = watched_tickers(db)
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, ticker, report_date, fiscal_date_ending, form_type, status, attempts
            FROM fiscal_calendar
            WHERE status = 'upcoming' AND report_date <= ? AND report_date >= ?
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY report_date, ticker
            """,
            (today.isoformat(), floor, utc_iso(now)),
        ).fetchall()
    return [
        dict(r) for r in rows
        if r["ticker"] in watched and (tickers is None or r["ticker"] in tickers)
    ]


def _insert_event(
    conn: sqlite3.Connection, ticker: str, form_type: str, filing: dict[str, str],
    *, discovered_by: str, calendar_id: int | None, stamp: str,
) -> int:
    conn.execute(
        """
        INSERT INTO filing_events
            (ticker, form_type, accession_number, filing_date, period_of_report,
             primary_document, calendar_id, discovered_by, discovered_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accession_number) DO UPDATE SET
            calendar_id = COALESCE(filing_events.calendar_id, excluded.calendar_id)
        """,
        (ticker, form_type, filing["accessionNumber"], filing["filingDate"],
         filing.get("reportDate") or None, filing.get("primaryDocument"),
         calendar_id, discovered_by, stamp, stamp),
    )
    return conn.execute(
        "SELECT id FROM filing_events WHERE accession_number = ?",
        (filing["accessionNumber"],),
    ).fetchone()[0]


def record_poll(conn: sqlite3.Connection, row: dict[str, Any], result: PollResult,
                now: datetime) -> int | None:
    """Persist one poll result on its calendar row; a hit creates an event."""
    stamp = utc_iso(now)
    if result.status == HIT:
        event_id = _insert_event(conn, row["ticker"], row["form_type"], result.filing,
                                 discovered_by="calendar", calendar_id=row["id"], stamp=stamp)
        conn.execute(
            "UPDATE fiscal_calendar SET status = 'detected', filing_event_id = ?, "
            "last_attempt_at = ?, last_error = NULL, next_attempt_at = NULL, updated_at = ? "
            "WHERE id = ?",
            (event_id, stamp, stamp, row["id"]),
        )
        return event_id
    if result.status == NOT_YET:
        conn.execute(
            "UPDATE fiscal_calendar SET last_attempt_at = ?, last_error = NULL, "
            "next_attempt_at = NULL WHERE id = ?",
            (stamp, row["id"]),
        )
        return None
    delay = POLL_ERROR_BACKOFF if result.status == ERROR else UNSUPPORTED_BACKOFF
    conn.execute(
        "UPDATE fiscal_calendar SET last_attempt_at = ?, last_error = ?, next_attempt_at = ? "
        "WHERE id = ?",
        (stamp, result.detail[:500], utc_iso(now + delay), row["id"]),
    )
    return None


def sweep_new_filings(
    db: Database, *, today: date, cache: dict[str, Any], days: int | None = None,
) -> list[str]:
    """Queue recent periodic filings of watched companies that nothing knows about."""
    days = days if days is not None else settings.get("watcher.sweep_days", db)
    cutoff = (today - timedelta(days=days)).isoformat()
    stamp = utc_iso()
    found: list[str] = []
    for ticker in sorted(watched_tickers(db)):
        entry = get_entry(ticker, db) or {}
        forms = [f for f in (entry.get("quarterly_form"), entry.get("annual_form"))
                 if f in SEC_FORM_TYPES]
        if not forms:
            continue
        try:
            submissions = submissions_for(ticker, db, cache)
        except (LookupError, SourceUnavailableError):
            continue
        for form in forms:
            for filing in list_filings(submissions, form):  # newest first
                if filing["filingDate"] < cutoff:
                    break
                period = filing.get("reportDate")
                if not period or already_in_db(ticker, form, period, db=db,
                                               accession=filing["accessionNumber"]):
                    continue
                with db.mutating() as conn:
                    if conn.execute("SELECT 1 FROM filing_events WHERE accession_number = ?",
                                    (filing["accessionNumber"],)).fetchone():
                        continue
                    window = timedelta(days=MATCH_WINDOW_DAYS)
                    lo = (date.fromisoformat(period) - window).isoformat()
                    hi = (date.fromisoformat(period) + window).isoformat()
                    cal = conn.execute(
                        "SELECT id FROM fiscal_calendar WHERE ticker = ? AND "
                        "fiscal_date_ending BETWEEN ? AND ? AND status IN "
                        "('upcoming', 'stale', 'failed') LIMIT 1",
                        (ticker, lo, hi),
                    ).fetchone()
                    event_id = _insert_event(conn, ticker, form, filing, discovered_by="sweep",
                                             calendar_id=cal["id"] if cal else None, stamp=stamp)
                    if cal:
                        conn.execute(
                            "UPDATE fiscal_calendar SET status = 'detected', filing_event_id = ?, "
                            "updated_at = ? WHERE id = ?",
                            (event_id, stamp, cal["id"]),
                        )
                found.append(f"{ticker} {form} {period}")
    return found


def enqueue_latest(ticker: str, form_type: str, *, db: Database,
                   cache: dict[str, Any] | None = None) -> int:
    """Manual run: queue (or re-queue) the newest `form_type` filing of `ticker`."""
    submissions = submissions_for(ticker, db, cache if cache is not None else {})
    filings = list_filings(submissions, form_type)
    if not filings:
        raise LookupError(f"no {form_type} filings for {ticker} on EDGAR")
    stamp = utc_iso()
    with db.mutating() as conn:
        event_id = _insert_event(conn, ticker, form_type, filings[0], discovered_by="manual",
                                 calendar_id=None, stamp=stamp)
        conn.execute(
            "UPDATE filing_events SET status = CASE WHEN source_document_id IS NULL "
            "THEN 'discovered' ELSE 'fetched' END, attempts = 0, next_attempt_at = NULL, "
            "updated_at = ? WHERE id = ?",
            (stamp, event_id),
        )
    return event_id


# ---- processing ------------------------------------------------------------------

def due_events(db: Database, *, now: datetime, limit: int,
               event_ids: list[int] | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT * FROM filing_events WHERE status IN ('discovered', 'fetched', 'partial') "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
    )
    params: list[Any] = [utc_iso(now)]
    if event_ids is not None:
        sql += f" AND id IN ({','.join('?' * len(event_ids))})"
        params += event_ids
    sql += " ORDER BY filing_date, id LIMIT ?"
    params.append(limit)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def _save_event(db: Database, event: dict[str, Any], now: datetime, *,
                calendar_status: str | None) -> None:
    stamp = utc_iso(now)
    with db.mutating() as conn:
        conn.execute(
            "UPDATE filing_events SET status = ?, attempts = ?, last_error = ?, "
            "next_attempt_at = ?, source_document_id = ?, period_of_report = ?, "
            "summary_json = ?, updated_at = ? WHERE id = ?",
            (event["status"], event["attempts"], event["last_error"], event["next_attempt_at"],
             event["source_document_id"], event["period_of_report"], event["summary_json"],
             stamp, event["id"]),
        )
        if event["calendar_id"] and calendar_status:
            conn.execute(
                "UPDATE fiscal_calendar SET status = ?, attempts = ?, last_error = ?, "
                "last_attempt_at = ?, next_attempt_at = ?, filing_event_id = ?, "
                "updated_at = ? WHERE id = ?",
                (calendar_status, event["attempts"], event["last_error"], stamp,
                 event["next_attempt_at"], event["id"], stamp, event["calendar_id"]),
            )


def process_event(event: dict[str, Any], *, db: Database, backend: Any,
                  now: datetime) -> EventOutcome:
    """Fetch (if needed) and extract one filing event; persist the result."""
    from ..extract.router import extract_filing

    event = dict(event)
    max_attempts = settings.get("watcher.max_attempts", db)
    attempts = event["attempts"] + 1
    outcome = EventOutcome(event["id"], event["ticker"], event["form_type"],
                           event["period_of_report"], event["filing_date"], status="retry")
    try:
        if not event["source_document_id"]:
            meta = fetch_and_record(event["ticker"], event["form_type"], db=db, filing={
                "accessionNumber": event["accession_number"],
                "filingDate": event["filing_date"],
                "reportDate": event["period_of_report"] or "",
                "primaryDocument": event["primary_document"],
            })
            event.update(status="fetched", source_document_id=meta["id"],
                         period_of_report=meta["period_of_report"])
            outcome.period = meta["period_of_report"]
        results = extract_filing(event["ticker"], event["form_type"],
                                 period=event["period_of_report"], write=True,
                                 backend=backend, db=db)
    except FATAL_LLM_ERRORS as e:
        # Not the filing's fault: keep what was fetched, don't count the attempt.
        event.update(last_error=f"deferred: {type(e).__name__}: {e}"[:500])
        _save_event(db, event, now, calendar_status=None)
        raise
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:500]
        exhausted = attempts >= max_attempts
        event.update(
            status="failed" if exhausted else event["status"], attempts=attempts,
            last_error=error,
            next_attempt_at=None if exhausted else utc_iso(now + backoff(attempts)),
        )
        _save_event(db, event, now, calendar_status="failed" if exhausted else "detected")
        outcome.status = "failed" if exhausted else "retry"
        outcome.issues = [error]
        return outcome

    extracted = sorted(mk for mk, r in results.items() if r.status == "success")
    review = sorted(f"{mk}: {r.status}" for mk, r in results.items() if r.status in NEEDS_REVIEW)
    retryable = sorted(
        f"{mk}: {r.status}" for mk, r in results.items()
        if r.status not in RESOLVED and r.status not in NEEDS_REVIEW
    )
    if not retryable:
        status, next_at = "extracted", None
    elif attempts >= max_attempts:
        status, next_at = "failed", None
    else:
        status, next_at = "partial", utc_iso(now + backoff(attempts))
    issues = retryable + review
    event.update(
        status=status, attempts=attempts, last_error="; ".join(issues) or None,
        next_attempt_at=next_at,
        summary_json=json.dumps({"metrics_extracted": extracted, "issues": issues}),
    )
    _save_event(db, event, now, calendar_status=status)
    outcome.status = status
    outcome.metrics_extracted = extracted
    outcome.issues = issues
    return outcome


def _pause_llm(db: Database, error: LLMUsageLimitError, now: datetime) -> None:
    until = error.resets_at or (now + DEFAULT_PAUSE)
    settings.set("llm.paused_until", utc_iso(until), db=db, actor="watcher")


# ---- outputs and notifications ------------------------------------------------------

def regenerate_outputs(log: Callable[[str], None] = print) -> None:
    """Reconcile period types, then rebuild the workbook, charts and site.

    Reconcile MUST run before the exporters: XBRL-extracted rows land
    with `period_type=''` and the chart selectors filter by period_type,
    so without it a new period would be invisible to every chart.
    Each step is independent; a failure is logged and the rest still run.
    """
    try:
        from ..extract.reconcile import reconcile
        s = reconcile(write=True)
        log(f"  reconcile: derived={s.derived} conflicts={s.conflicts} "
            f"unresolved={s.unresolved}")
    except Exception as e:
        log(f"  reconcile error: {type(e).__name__}: {e}")
    try:
        from ..exporters.excel import export_workbook
        log(f"  workbook: {export_workbook().name}")
    except Exception as e:
        log(f"  workbook error: {type(e).__name__}: {e}")
    try:
        from ..exporters.charts import generate_all_metric_charts
        from ..exporters.dashboard_html import generate_dashboard_html
        from ..exporters.earnings_calendar_html import generate_earnings_calendar_html
        from ..exporters.interactive_chart import generate_all_interactive
        from ..exporters.treatments_html import generate_treatments_html
        generate_all_metric_charts()
        generate_all_interactive()
        generate_dashboard_html()
        generate_earnings_calendar_html()
        generate_treatments_html()
        log("  charts + site regenerated")
    except Exception as e:
        log(f"  chart/site error: {type(e).__name__}: {e}")


def notify_fresh(outcomes: list[EventOutcome], *, db: Database, today: date) -> dict | None:
    """Email subscribers about newly extracted filings, skipping old backlog."""
    if not settings.get("notify.enabled", db):
        return None
    max_age = settings.get("notify.max_age_days", db)
    fresh = [
        o for o in outcomes
        if o.status in ("extracted", "partial")
        and (today - date.fromisoformat(o.filing_date)).days <= max_age
    ]
    if not fresh:
        return None
    from ..notify import notify_subscribers
    return notify_subscribers([
        {"status": "success", "ticker": o.ticker, "period": o.period, "filed": o.filing_date,
         "metrics_extracted": o.metrics_extracted, "issues": o.issues}
        for o in fresh
    ], db=db)


# ---- one run ------------------------------------------------------------------------

def run_watcher(
    *,
    db: Database | None = None,
    backend: Any | None = None,
    dry_run: bool = False,
    since: str | None = None,
    tickers: set[str] | None = None,
    sweep: bool = False,
    event_ids: list[int] | None = None,
    today: date | None = None,
    now: datetime | None = None,
    log: Callable[[str], None] = print,
) -> RunSummary:
    """One watcher run (see the module docstring). Never raises for LLM
    problems: they end the run and are reported in the summary."""
    db = db or Database()
    today = today or today_eastern()
    now = now or utc_now()
    summary = RunSummary(dry_run=dry_run)
    cache: dict[str, Any] = {}

    if not dry_run:
        sync_watchlist(db)
        summary.stale = mark_stale_rows(db, today)
        if summary.stale:
            log(f"marked {summary.stale} calendar row(s) stale")

    rows = [] if event_ids is not None else select_due_rows(
        db, today=today, now=now, since=since, tickers=tickers,
    )
    summary.due = len(rows)
    results: list[tuple[dict[str, Any], PollResult]] = []
    for row in rows:
        result = poll_for_row(row["ticker"], row["form_type"], row["fiscal_date_ending"],
                              db=db, cache=cache)
        summary.polls[result.status] = summary.polls.get(result.status, 0) + 1
        results.append((row, result))
        what = (f"{row['ticker']} {row['form_type']} period {row['fiscal_date_ending']} "
                f"(reported {row['report_date']})")
        if result.status == HIT:
            summary.discovered.append(what)
            log(f"found   {what}: {result.filing['accessionNumber']} "
                f"filed {result.filing['filingDate']}")
        elif result.status == NOT_YET:
            log(f"waiting {what}: not on EDGAR yet")
        else:
            log(f"{result.status:7} {what}: {result.detail}")
    if dry_run:
        return summary
    if results:
        with db.mutating() as conn:
            for row, result in results:
                record_poll(conn, row, result, now)

    if sweep and event_ids is None:
        summary.swept = sweep_new_filings(db, today=today, cache=cache)
        for item in summary.swept:
            log(f"swept   {item}")

    limit = settings.get("llm.max_filings_per_run", db)
    events = due_events(db, now=now, limit=limit, event_ids=event_ids)
    for event in events:
        log(f"process {event['ticker']} {event['form_type']} {event['accession_number']} "
            f"(attempt {event['attempts'] + 1})")
        try:
            if backend is None:
                from ..adapters.cli_backend import CLIBackend
                backend = CLIBackend.from_settings(db=db)
            outcome = process_event(event, db=db, backend=backend, now=now)
        except LLMAuthError as e:
            summary.stop_kind, summary.stopped = "auth", str(e)
        except LLMUsageLimitError as e:
            _pause_llm(db, e, now)
            summary.stop_kind, summary.stopped = "deferred", str(e)
        except LLMBudgetError as e:
            summary.stop_kind, summary.stopped = "deferred", str(e)
        except LLMConfigError as e:
            summary.stop_kind, summary.stopped = "config", str(e)
        else:
            summary.outcomes.append(outcome)
            log(f"  → {outcome.status}: extracted {outcome.metrics_extracted or '-'}"
                + (f"; issues {outcome.issues}" if outcome.issues else ""))
            continue
        log(f"  stopped: {summary.stopped}")
        break

    if any(o.status in ("extracted", "partial") for o in summary.outcomes):
        log("regenerating outputs")
        regenerate_outputs(log)
        summary.outputs_regenerated = True
        summary.notify = notify_fresh(summary.outcomes, db=db, today=today)
    return summary
