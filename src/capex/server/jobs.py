"""The scheduler's jobs, one process each:

    python -m capex.server.jobs <job> [--run-id N] [--params JSON]

Exit codes, mapped to the run status by the scheduler: 0 success,
3 partial, 4 skipped (not configured / nothing to do), 75 deferred (LLM
usage limit or budget, or the pipeline lock is busy), 77 Claude token
rejected, anything else failed. A job's summary lands in
runs.summary_json. Jobs that change the site queue a `publish`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import paths, settings
from ..db import Database
from . import schedules
from .locks import PIPELINE, LockBusyError, file_lock

EXIT_OK, EXIT_ERROR, EXIT_PARTIAL, EXIT_SKIPPED = 0, 1, 3, 4
EXIT_DEFERRED, EXIT_AUTH = 75, 77
PIPELINE_WAIT_S = 300
KEEP_REQUESTS_DAYS = 90
KEEP_LLM_CALLS_DAYS = 400


@dataclass
class JobContext:
    db: Database
    run_id: int | None
    params: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)

    def log(self, message: str) -> None:
        print(message, flush=True)


def _queue_publish(ctx: JobContext, by: str) -> None:
    request_id, created = schedules.queue_request(ctx.db, "publish", requested_by=f"job:{by}")
    if created:
        ctx.log(f"queued publish (request {request_id})")


# ---- jobs ------------------------------------------------------------------------

def job_watcher(ctx: JobContext, *, sweep: bool = False) -> int:
    from ..monitor import pipeline

    with file_lock(PIPELINE, wait_s=PIPELINE_WAIT_S):
        s = pipeline.run_watcher(db=ctx.db, sweep=sweep, since=ctx.params.get("since"),
                                 log=ctx.log)
    ctx.summary.update(
        due=s.due, polls=s.polls, discovered=s.discovered, swept=s.swept, stale=s.stale,
        outcomes=[{"ticker": o.ticker, "form": o.form_type, "period": o.period,
                   "status": o.status, "metrics": o.metrics_extracted, "issues": o.issues}
                  for o in s.outcomes],
        stopped=s.stopped, outputs_regenerated=s.outputs_regenerated, notify=s.notify,
    )
    if s.outputs_regenerated:
        _queue_publish(ctx, "filings_sweep" if sweep else "watcher")
    return s.exit_code()


def job_filings_sweep(ctx: JobContext) -> int:
    return job_watcher(ctx, sweep=True)


def job_calendar_sync(ctx: JobContext) -> int:
    from ..monitor.calendar import CalendarError, sync_earnings_calendar

    try:
        result = sync_earnings_calendar(api_key=os.environ.get("ALPHA_VANTAGE_API_KEY"),
                                        db=ctx.db)
    except CalendarError as e:
        ctx.log(f"calendar sync failed: {e}")
        ctx.summary["error"] = str(e)
        return EXIT_ERROR
    ctx.summary.update(result)
    ctx.log(f"calendar: {result.get('synced')} synced, {result.get('skipped')} skipped")
    return EXIT_PARTIAL if result.get("errors") else EXIT_OK


def job_regenerate_outputs(ctx: JobContext) -> int:
    from ..monitor.pipeline import regenerate_outputs

    workbook = True if ctx.params.get("workbook") else None
    with file_lock(PIPELINE, wait_s=PIPELINE_WAIT_S):
        result = regenerate_outputs(ctx.log, workbook=workbook)
    ctx.summary.update(result)
    _queue_publish(ctx, "regenerate_outputs")
    return EXIT_PARTIAL if result["errors"] else EXIT_OK


def job_publish(ctx: JobContext) -> int:
    from .publish import publish_site

    with file_lock(PIPELINE, wait_s=PIPELINE_WAIT_S):
        summary = publish_site(db=ctx.db, force=bool(ctx.params.get("force")), log=ctx.log)
    ctx.summary.update(summary)
    if summary["status"] == "skipped":
        ctx.log(f"publish skipped: {summary['reason']}")
        return EXIT_SKIPPED
    return EXIT_OK


def job_backup(ctx: JobContext) -> int:
    from .backup import backup_db

    ctx.summary.update(backup_db(db=ctx.db, log=ctx.log))
    return EXIT_OK


def job_backup_raw(ctx: JobContext) -> int:
    from .backup import sync_raw

    summary = sync_raw(db=ctx.db, log=ctx.log)
    ctx.summary.update(summary)
    return EXIT_SKIPPED if summary.get("status") == "skipped" else EXIT_OK


def job_health(ctx: JobContext) -> int:
    from . import health

    results = health.run_checks(ctx.db)
    for c in results:
        ctx.log(f"{c.level:8} {c.name:16} {c.detail}")
    ctx.summary["checks"] = {c.name: [c.level, c.detail] for c in results}
    ctx.summary["alerts_sent"] = health.alert_on(results, db=ctx.db, log=ctx.log)
    # Findings make the run 'partial', not 'failed': the checks above already
    # emailed about them, and 'failed' (which alerts again) means a crash.
    return EXIT_PARTIAL if health.exit_code(results) else EXIT_OK


def job_llm_check(ctx: JobContext) -> int:
    from ..adapters.cli_backend import CLIBackend
    from ..adapters.errors import LLMAuthError, LLMBudgetError, LLMError, LLMUsageLimitError

    backend = CLIBackend.from_settings(db=ctx.db)
    try:
        answer = backend.extract("", "Reply with the single word OK.")
    except LLMAuthError as e:
        ctx.log(f"auth failed: {e}")
        ctx.summary["error"] = str(e)
        return EXIT_AUTH
    except (LLMUsageLimitError, LLMBudgetError) as e:
        ctx.log(f"deferred: {e}")
        ctx.summary["deferred"] = str(e)
        return EXIT_DEFERRED
    except LLMError as e:
        ctx.log(f"{type(e).__name__}: {e}")
        ctx.summary["error"] = f"{type(e).__name__}: {e}"
        return EXIT_ERROR
    ctx.summary.update(model=backend.model, answer=answer.strip()[:40],
                       duration_ms=(backend.last_call or {}).get("duration_ms"))
    ctx.log(f"{backend.model}: {answer.strip()[:40]!r}")
    return EXIT_OK if "OK" in answer.upper() else EXIT_ERROR


def job_prune(ctx: JobContext) -> int:
    from ..exporters.excel import parse_workbook_name

    now = datetime.now(timezone.utc)
    removed: dict[str, int] = {}

    log_cutoff = now - timedelta(days=settings.get("prune.run_log_days", ctx.db))
    run_logs = paths.logs_dir() / "runs"
    removed["run_logs"] = 0
    if run_logs.exists():
        for path in run_logs.glob("*.log"):
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < log_cutoff:
                path.unlink()
                removed["run_logs"] += 1

    keep = settings.get("prune.keep_workbooks", ctx.db)
    ranked = sorted(((key, p) for p in paths.workbook_dir().glob("*.xlsx")
                     if (key := parse_workbook_name(p.name)) is not None), reverse=True)
    removed["workbooks"] = 0
    for _, path in ranked[max(keep, 1):]:  # the newest is never removed
        path.unlink()
        removed["workbooks"] += 1

    with ctx.db.ops_write() as conn:
        removed["requests"] = conn.execute(
            "DELETE FROM job_requests WHERE status IN ('done', 'cancelled') "
            "AND requested_at < ?",
            (schedules.utc_iso(now - timedelta(days=KEEP_REQUESTS_DAYS)),)).rowcount
        removed["llm_calls"] = conn.execute(
            "DELETE FROM llm_calls WHERE ts < ?",
            (schedules.utc_iso(now - timedelta(days=KEEP_LLM_CALLS_DAYS)),)).rowcount
    ctx.summary["removed"] = removed
    ctx.log(f"pruned: {removed}")
    if removed["workbooks"]:
        _queue_publish(ctx, "prune")  # drop them from the site too
    return EXIT_OK


RUNNERS: dict[str, Callable[[JobContext], int]] = {
    "watcher": job_watcher,
    "filings_sweep": job_filings_sweep,
    "calendar_sync": job_calendar_sync,
    "regenerate_outputs": job_regenerate_outputs,
    "publish": job_publish,
    "backup": job_backup,
    "backup_raw": job_backup_raw,
    "health": job_health,
    "llm_check": job_llm_check,
    "prune": job_prune,
}
assert set(RUNNERS) == set(schedules.JOBS), "every scheduled job needs a runner"


def run_job(job: str, *, db: Database | None = None, run_id: int | None = None,
            params: dict[str, Any] | None = None) -> int:
    """Run `job` in this process; record its summary on the run row."""
    ctx = JobContext(db or Database(), run_id, params or {})
    try:
        code = RUNNERS[job](ctx)
    except LockBusyError as e:
        ctx.log(f"deferred: {e}")
        ctx.summary["deferred"] = str(e)
        code = EXIT_DEFERRED
    except Exception as e:
        traceback.print_exc()
        ctx.summary["error"] = f"{type(e).__name__}: {e}"
        code = EXIT_ERROR
    if run_id is not None:
        try:
            schedules.set_run_summary(ctx.db, run_id, ctx.summary)
        except Exception as e:  # never let bookkeeping change the job's outcome
            print(f"could not record the run summary: {e}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m capex.server.jobs")
    parser.add_argument("job", choices=sorted(RUNNERS))
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--params", default="{}", help="JSON object of job parameters")
    args = parser.parse_args(argv)
    try:
        params = json.loads(args.params)
    except ValueError:
        parser.error("--params must be a JSON object")
    if not isinstance(params, dict):
        parser.error("--params must be a JSON object")
    return run_job(args.job, run_id=args.run_id, params=params)


if __name__ == "__main__":
    sys.exit(main())
