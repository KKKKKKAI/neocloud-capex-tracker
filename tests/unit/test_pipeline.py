"""The watcher pipeline's state machine, with SEC, fetch and extraction faked."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from capex import settings
from capex.adapters.errors import LLMAuthError, LLMUsageLimitError
from capex.monitor import pipeline
from capex.monitor.watcher import ERROR, HIT, KNOWN, NOT_YET, UNSUPPORTED, PollResult

TODAY = date(2026, 10, 30)
NOW = datetime(2026, 10, 30, 22, 0, tzinfo=timezone.utc)


def _filing(accession, report_date, filed="2026-10-29"):
    return {"accessionNumber": accession, "filingDate": filed, "reportDate": report_date,
            "primaryDocument": f"{accession}.htm", "form": "10-Q"}


@pytest.fixture
def world(capex_db, monkeypatch):
    """Scripted SEC answers, a fetch that records a real source_documents
    row, and extraction results chosen per test."""
    polls: dict[str, PollResult] = {}
    extraction: dict = {"results": {"revenue": "success", "capital_expenditures": "success"}}
    calls = {"fetch": [], "extract": [], "regen": 0, "notify": []}

    monkeypatch.setattr(pipeline, "poll_for_row",
                        lambda ticker, form, fde, db, cache, report_date:
                        polls.get(ticker, PollResult(NOT_YET)))

    def fake_fetch(ticker, form_type, db, filing):
        calls["fetch"].append((ticker, filing["accessionNumber"]))
        with db.mutating() as conn:
            cur = conn.execute(
                "INSERT INTO source_documents (ticker, form_type, filing_date, period_of_report, "
                "fiscal_year, period_token, sha256, raw_path, source, source_url, "
                "accession_number, fetched_at, fetcher_version, protocol_version) VALUES "
                "(?, ?, ?, ?, 2027, 'Q1', ?, 'x', 'sec_edgar', 'u', ?, 'now', 't', 'p')",
                (ticker, form_type, filing["filingDate"], filing["reportDate"],
                 "sha-" + filing["accessionNumber"], filing["accessionNumber"]),
            )
        return {"id": cur.lastrowid, "period_of_report": filing["reportDate"]}

    def fake_extract(ticker, form_type, period, write, backend, db):
        calls["extract"].append((ticker, period))
        if isinstance(extraction["results"], Exception):
            raise extraction["results"]
        return {mk: SimpleNamespace(status=st) for mk, st in extraction["results"].items()}

    monkeypatch.setattr(pipeline, "fetch_and_record", fake_fetch)
    monkeypatch.setattr("capex.extract.router.extract_filing", fake_extract)
    monkeypatch.setattr(pipeline, "regenerate_outputs",
                        lambda log=print: calls.__setitem__("regen", calls["regen"] + 1))
    monkeypatch.setattr(pipeline, "notify_fresh",
                        lambda outcomes, db, today: calls["notify"].append(outcomes) or None)
    return SimpleNamespace(db=capex_db, polls=polls, extraction=extraction, calls=calls)


def _add_row(db, ticker, fde, report_date, form="10-Q", **extra):
    cols = {"ticker": ticker, "report_date": report_date, "fiscal_date_ending": fde,
            "form_type": form, "status": "upcoming", "source": "alpha_vantage",
            "updated_at": "x", **extra}
    with db.mutating() as conn:
        conn.execute(
            f"INSERT INTO fiscal_calendar ({', '.join(cols)}) VALUES "
            f"({', '.join('?' * len(cols))})",
            list(cols.values()),
        )


def _row(db, ticker):
    with db.connect() as conn:
        return dict(conn.execute("SELECT * FROM fiscal_calendar WHERE ticker = ?",
                                 (ticker,)).fetchone())


def _events(db):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM filing_events ORDER BY id")]


def run(world, **kw):
    return pipeline.run_watcher(db=world.db, backend=object(), today=TODAY, now=NOW,
                                log=lambda _: None, **kw)


def test_found_and_fully_extracted(world):
    _add_row(world.db, "MSFT", "2026-09-30", "2026-10-28")
    world.polls["MSFT"] = PollResult(HIT, filing=_filing("acc-1", "2026-09-30"))

    summary = run(world)

    assert summary.exit_code() == pipeline.EXIT_OK
    (event,) = _events(world.db)
    assert (event["status"], event["attempts"], event["discovered_by"]) == ("extracted", 1, "calendar")
    row = _row(world.db, "MSFT")
    assert (row["status"], row["filing_event_id"]) == ("extracted", event["id"])
    assert world.calls["fetch"] == [("MSFT", "acc-1")]
    assert world.calls["regen"] == 1
    assert len(world.calls["notify"]) == 1


def test_retryable_gap_is_partial_with_backoff(world):
    _add_row(world.db, "MSFT", "2026-09-30", "2026-10-28")
    world.polls["MSFT"] = PollResult(HIT, filing=_filing("acc-1", "2026-09-30"))
    world.extraction["results"] = {"revenue": "success", "cloud_segment_revenue": "needs_interactive"}

    summary = run(world)

    assert summary.exit_code() == pipeline.EXIT_PARTIAL
    (event,) = _events(world.db)
    assert event["status"] == "partial"
    assert event["next_attempt_at"] == (NOW + timedelta(minutes=15)).isoformat(timespec="seconds")
    assert "cloud_segment_revenue: needs_interactive" in event["last_error"]
    assert _row(world.db, "MSFT")["status"] == "partial"


def test_needs_verification_is_flagged_not_retried(world):
    _add_row(world.db, "MSFT", "2026-09-30", "2026-10-28")
    world.polls["MSFT"] = PollResult(HIT, filing=_filing("acc-1", "2026-09-30"))
    world.extraction["results"] = {"revenue": "success", "cloud_segment_revenue": "needs_verification"}
    run(world)
    (event,) = _events(world.db)
    assert event["status"] == "extracted"
    assert "needs_verification" in event["last_error"]


def test_errors_back_off_then_fail_after_max_attempts(world):
    settings.set("watcher.max_attempts", 2, db=world.db)
    _add_row(world.db, "MSFT", "2026-09-30", "2026-10-28")
    world.polls["MSFT"] = PollResult(HIT, filing=_filing("acc-1", "2026-09-30"))
    world.extraction["results"] = ValueError("parser exploded")

    first = run(world)
    (event,) = _events(world.db)
    assert (event["status"], event["attempts"]) == ("fetched", 1)
    assert first.outcomes[0].status == "retry"

    later = NOW + timedelta(hours=1)
    pipeline.run_watcher(db=world.db, backend=object(), today=TODAY, now=later, log=lambda _: None)
    (event,) = _events(world.db)
    assert (event["status"], event["attempts"]) == ("failed", 2)
    assert "parser exploded" in event["last_error"]
    assert _row(world.db, "MSFT")["status"] == "failed"
    assert len(world.calls["fetch"]) == 1  # fetched once, reused on retry


def test_usage_limit_stops_the_run_and_pauses_llm(world):
    resets = datetime(2026, 10, 31, 3, 0, tzinfo=timezone.utc)
    _add_row(world.db, "MSFT", "2026-09-30", "2026-10-28")
    world.polls["MSFT"] = PollResult(HIT, filing=_filing("acc-1", "2026-09-30"))
    world.extraction["results"] = LLMUsageLimitError("limit", resets_at=resets)

    summary = run(world)

    assert summary.exit_code() == pipeline.EXIT_DEFERRED
    (event,) = _events(world.db)
    assert (event["status"], event["attempts"]) == ("fetched", 0)   # not held against the filing
    assert settings.get("llm.paused_until", world.db) == resets.isoformat(timespec="seconds")
    assert world.calls["regen"] == 0


def test_auth_failure_exits_77(world):
    _add_row(world.db, "MSFT", "2026-09-30", "2026-10-28")
    world.polls["MSFT"] = PollResult(HIT, filing=_filing("acc-1", "2026-09-30"))
    world.extraction["results"] = LLMAuthError("token expired")
    assert run(world).exit_code() == pipeline.EXIT_AUTH


def test_poll_outcomes_are_recorded(world):
    _add_row(world.db, "MSFT", "2026-09-30", "2026-10-28")
    _add_row(world.db, "ORCL", "2026-08-31", "2026-10-20")
    _add_row(world.db, "BIDU", "2026-09-30", "2026-10-25", form="HK-IR")
    world.polls["ORCL"] = PollResult(ERROR, detail="sec_edgar: gave up")
    world.polls["BIDU"] = PollResult(UNSUPPORTED, detail="no automated fetcher for HK-IR yet")

    summary = run(world)

    assert summary.polls == {"not_yet": 1, "error": 1, "unsupported": 1}
    msft, orcl, bidu = (_row(world.db, t) for t in ("MSFT", "ORCL", "BIDU"))
    assert (msft["status"], msft["next_attempt_at"]) == ("upcoming", None)
    assert orcl["next_attempt_at"] == (NOW + timedelta(hours=1)).isoformat(timespec="seconds")
    assert orcl["attempts"] == 0 and "gave up" in orcl["last_error"]
    assert bidu["next_attempt_at"] == (NOW + timedelta(days=1)).isoformat(timespec="seconds")
    assert _events(world.db) == []


def test_ruled_out_6ks_are_remembered(world):
    _add_row(world.db, "BIDU", "2026-09-30", "2026-10-25", form="6-K")
    buyback = {"accessionNumber": "acc-bb", "filingDate": "2026-10-28", "reportDate": "2026-10-28",
               "primaryDocument": "bb.htm", "form": "6-K"}
    world.polls["BIDU"] = PollResult(NOT_YET, ignored=[(buyback, "not an earnings release (score -6)")])

    run(world)

    (event,) = _events(world.db)
    assert (event["accession_number"], event["status"], event["calendar_id"]) == ("acc-bb", "ignored", None)
    assert event["period_of_report"] is None            # a 6-K's reportDate is no fiscal period
    assert "score -6" in event["last_error"]
    assert _row(world.db, "BIDU")["status"] == "upcoming"
    assert world.calls["fetch"] == []


def test_ruling_out_never_downgrades_an_event_in_progress(world):
    filing = _filing("acc-1", "2026-06-30", filed="2026-08-18")
    with world.db.mutating() as conn:
        pipeline._insert_event(conn, "BIDU", "6-K", filing, discovered_by="manual",
                               calendar_id=None, stamp="x")
        conn.execute("UPDATE filing_events SET status = 'extracted'")
    _add_row(world.db, "BIDU", "2026-09-30", "2026-10-25", form="6-K")
    world.polls["BIDU"] = PollResult(NOT_YET, ignored=[(filing, "not an earnings release (score 0)")])
    run(world)
    assert [e["status"] for e in _events(world.db)] == ["extracted"]


def test_known_release_skips_the_calendar_row(world):
    _add_row(world.db, "BIDU", "2026-09-30", "2026-10-25", form="6-K")
    world.polls["BIDU"] = PollResult(KNOWN, filing=_filing("acc-1", "2026-09-30"),
                                     detail="period already recorded (source_documents id 7)")
    summary = run(world)
    row = _row(world.db, "BIDU")
    assert (row["status"], row["last_error"]) == (
        "skipped", "period already recorded (source_documents id 7)")
    assert summary.polls == {"known": 1} and _events(world.db) == []


def test_stale_rows_and_row_selection(world):
    _add_row(world.db, "MSFT", "2026-06-30", "2026-07-29",                 # polled after its
             last_attempt_at="2026-08-20T01:00:00+00:00")                   # 21-day window: stale
    _add_row(world.db, "AMZN", "2026-06-30", "2026-07-30")                 # never polled: one more look
    _add_row(world.db, "CRWV", "2026-03-31", "2026-04-20")                 # before the lookback: stale
    _add_row(world.db, "ORCL", "2026-08-31", "2026-10-20")                 # due
    _add_row(world.db, "GOOGL", "2026-09-30", "2026-11-05")                # not reported yet
    _add_row(world.db, "META", "2026-09-30", "2026-10-29",
             next_attempt_at=(NOW + timedelta(hours=2)).isoformat())       # backing off
    _add_row(world.db, "0700", "2026-06-30", "2026-10-25", form="HK-IR")   # unwatched

    summary = run(world)

    assert summary.stale == 2
    assert (_row(world.db, "MSFT")["status"], _row(world.db, "CRWV")["status"]) == ("stale", "stale")
    assert summary.due == 2   # AMZN and ORCL
    assert _row(world.db, "ORCL")["last_attempt_at"] is not None

    pipeline.run_watcher(db=world.db, backend=object(), today=TODAY + timedelta(days=1),
                         now=NOW + timedelta(days=1), log=lambda _: None)
    assert _row(world.db, "AMZN")["status"] == "stale"      # looked at, still nothing


def test_max_filings_per_run(world):
    settings.set("llm.max_filings_per_run", 1, db=world.db)
    for ticker, acc in (("MSFT", "acc-1"), ("ORCL", "acc-2")):
        _add_row(world.db, ticker, "2026-09-30", "2026-10-28")
        world.polls[ticker] = PollResult(HIT, filing=_filing(acc, "2026-09-30"))
    summary = run(world)
    assert len(summary.outcomes) == 1
    assert sorted(e["status"] for e in _events(world.db)) == ["discovered", "extracted"]


def test_dry_run_changes_nothing(world):
    _add_row(world.db, "MSFT", "2026-09-30", "2026-10-28")
    world.polls["MSFT"] = PollResult(HIT, filing=_filing("acc-1", "2026-09-30"))
    before = _row(world.db, "MSFT")
    summary = run(world, dry_run=True)
    assert summary.discovered and summary.exit_code() == pipeline.EXIT_OK
    assert _row(world.db, "MSFT") == before
    assert _events(world.db) == []
    assert world.calls["fetch"] == []


def test_sweep_queues_unknown_recent_filings(world, monkeypatch):
    submissions = {"filings": {"recent": {
        "accessionNumber": ["iren-q", "iren-old"],
        "filingDate": ["2026-10-20", "2026-05-10"],
        "reportDate": ["2026-09-30", "2026-03-31"],
        "primaryDocument": ["q.htm", "old.htm"],
        "form": ["10-Q", "10-Q"],
    }}}
    monkeypatch.setattr(pipeline, "submissions_for",
                        lambda ticker, db, cache: submissions if ticker == "IREN" else {})
    _add_row(world.db, "IREN", "2026-09-30", "2026-11-06", status="stale")

    summary = run(world, sweep=True)

    assert summary.swept == ["IREN 10-Q 2026-09-30"]           # old one outside sweep_days
    (event,) = _events(world.db)
    assert event["discovered_by"] == "sweep"
    assert _row(world.db, "IREN")["filing_event_id"] == event["id"]


def test_notify_skips_backlog(capex_db, monkeypatch):
    sent = []
    monkeypatch.setattr("capex.notify.notify_subscribers",
                        lambda results, db: sent.extend(results) or {"sent": len(results)})
    fresh = pipeline.EventOutcome(1, "MSFT", "10-Q", "2026-09-30", "2026-10-28", "extracted")
    old = pipeline.EventOutcome(2, "ORCL", "10-Q", "2026-05-31", "2026-06-20", "extracted")
    pipeline.notify_fresh([fresh, old], db=capex_db, today=TODAY)
    assert [r["ticker"] for r in sent] == ["MSFT"]


def test_backoff_schedule():
    assert [pipeline.backoff(n) for n in (1, 2, 3)] == [
        timedelta(minutes=15), timedelta(minutes=30), timedelta(hours=1)]
    assert pipeline.backoff(20) == timedelta(hours=12)
