"""`capex monitor`: run the watcher pipeline once (see pipeline.py).

    capex monitor --catch-up [--since YYYY-MM-DD] [--sweep] [--dry-run]
        every watched calendar row whose report date has passed
    capex monitor --all-today [--dry-run]
        only rows reporting today (US/Eastern)
    capex monitor TICKER [FORM]
        queue and process the newest FORM filing of one company now
        (FORM defaults to its watchlist quarterly form)

--dry-run polls SEC and prints what would happen, changing nothing.

Exit codes: 0 ok; 1 error; 3 some filings partial or failed; 75 deferred
(LLM usage limit, budget or pause); 77 LLM authentication failed.
Nothing here commits or pushes: the server publishes outputs itself.
"""
from __future__ import annotations

import sys

from ..db import Database
from . import pipeline
from .clock import today_eastern
from .watchlist import get_entry, sync_watchlist

USAGE = __doc__.split("\n\n")[1]


def _flag_value(argv: list[str], flag: str) -> str | None:
    if flag not in argv:
        return None
    i = argv.index(flag)
    if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
        raise SystemExit(f"{flag} needs a value")
    return argv[i + 1]


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else pipeline.EXIT_ERROR

    db = Database()
    dry_run = "--dry-run" in argv
    kwargs: dict = {"db": db, "dry_run": dry_run}

    if argv[0] == "--catch-up":
        kwargs.update(since=_flag_value(argv, "--since"), sweep="--sweep" in argv)
    elif argv[0] == "--all-today":
        kwargs.update(since=today_eastern().isoformat())
    elif not argv[0].startswith("-"):
        ticker = argv[0].upper()
        form = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else None
        if form is None:
            sync_watchlist(db)
            entry = get_entry(ticker, db) or {}
            form = entry.get("quarterly_form") or entry.get("annual_form")
        if not form:
            print(f"no form given and none on the watchlist for {ticker}", file=sys.stderr)
            return pipeline.EXIT_ERROR
        if dry_run:
            print(f"would queue and process the newest {form} filing of {ticker}")
            return pipeline.EXIT_OK
        try:
            event_id = pipeline.enqueue_latest(ticker, form, db=db)
        except Exception as e:
            print(f"cannot queue {ticker} {form}: {e}", file=sys.stderr)
            return pipeline.EXIT_ERROR
        kwargs.update(event_ids=[event_id])
    else:
        print(USAGE, file=sys.stderr)
        return pipeline.EXIT_ERROR

    summary = pipeline.run_watcher(**kwargs)
    _print_summary(summary)
    return summary.exit_code()


def _print_summary(s: pipeline.RunSummary) -> None:
    polls = ", ".join(f"{k}={v}" for k, v in sorted(s.polls.items())) or "none"
    print(f"\nsummary: {s.due} due row(s) polled ({polls}); "
          f"{len(s.discovered)} filing(s) found; {s.stale} marked stale"
          + (f"; {len(s.swept)} swept" if s.swept else ""))
    if s.dry_run:
        print("dry run: nothing was changed")
        return
    for o in s.outcomes:
        print(f"  {o.ticker} {o.form_type} {o.period or '?'}: {o.status}"
              + (f" ({', '.join(o.issues)})" if o.issues else ""))
    if s.stopped:
        print(f"stopped early ({s.stop_kind}): {s.stopped}")
    if s.notify is not None:
        print(f"notify: sent={s.notify.get('sent')} skipped={s.notify.get('skipped')} "
              f"errors={len(s.notify.get('errors', []))}")


if __name__ == "__main__":
    sys.exit(main())
