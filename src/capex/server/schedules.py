"""Job schedules, run requests and run records (migration 0011 tables).

Every job has one schedule: a 5-field cron evaluated in a time zone
(Europe/London by default), so "07:00" stays 07:00 local across DST.
When a schedule falls due the scheduler queues a request and moves the
schedule to its next time after *now*, so runs missed while the server
was down or paused collapse into one. The admin panel and `capex server
run` queue requests directly. A job never has two identical requests
waiting: queueing again returns the one already there.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from .. import paths
from ..db import Database

DEFAULT_TZ = "Europe/London"


@dataclass(frozen=True)
class JobSpec:
    cron: str
    timeout_s: int
    help: str


JOBS: dict[str, JobSpec] = {
    "watcher": JobSpec(
        "*/20 * * * *", 5400,
        "Poll SEC for due calendar rows; fetch and extract new filings."),
    "filings_sweep": JobSpec(
        "10 6,18 * * *", 5400,
        "A watcher run that also queues recent filings no calendar row pointed at."),
    "calendar_sync": JobSpec(
        "0 7 * * *", 600, "Refresh earnings dates from Alpha Vantage."),
    "regenerate_outputs": JobSpec(
        "30 5 * * *", 1800,
        "Rebuild charts and site, and the workbook when the data changed."),
    "publish": JobSpec(
        "15 * * * *", 900,
        "Upload the site and workbooks to S3 and invalidate CloudFront "
        "(also queued after every regenerate)."),
    "backup": JobSpec(
        "15 3 * * *", 1800, "Back up the DB to S3, keeping local copies too."),
    "backup_raw": JobSpec(
        "45 3 * * 0", 7200, "Copy new raw filings to the backup bucket."),
    "health": JobSpec(
        "5 * * * *", 300, "Health checks; email the operator about problems."),
    "llm_check": JobSpec(
        "0 8 * * *", 600, "One real Claude call, to catch a rejected token early."),
    "prune": JobSpec(
        "30 4 * * 0", 900, "Delete old run logs, finished requests and workbooks."),
}

MIN_TIMEOUT_S, MAX_TIMEOUT_S = 30, 86_400


class ScheduleError(ValueError):
    """Unknown job, or an invalid cron / time zone / timeout."""


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_cron(expr: str) -> str | None:
    """None if `expr` is a usable 5-field cron, else the problem."""
    if len(expr.split()) != 5:
        return "cron needs 5 fields: minute hour day-of-month month day-of-week"
    if not croniter.is_valid(expr):
        return f"not a valid cron expression: {expr!r}"
    return None


def next_run(expr: str, tz: str, after: datetime) -> datetime:
    """The first time strictly after `after` that `expr` fires in `tz` (UTC)."""
    local = after.astimezone(ZoneInfo(tz))
    return croniter(expr, local).get_next(datetime).astimezone(timezone.utc)


@lru_cache(maxsize=1)
def code_sha() -> str | None:
    """The deployed commit: the release marker's content, else git HEAD."""
    marker = paths.CODE_ROOT / paths.RELEASE_MARKER
    if marker.exists():
        return marker.read_text(encoding="utf-8").strip()[:40] or None
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=paths.CODE_ROOT,
                             capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


# ---- schedules ------------------------------------------------------------------

def ensure_defaults(db: Database, now: datetime | None = None) -> list[str]:
    """Add a schedule for every job that has none (never touches edits)."""
    now = now or utc_now()
    added = []
    with db.ops_write() as conn:
        existing = {r[0] for r in conn.execute("SELECT job FROM job_schedules")}
        for job, spec in JOBS.items():
            if job in existing:
                continue
            conn.execute(
                "INSERT INTO job_schedules (job, cron, tz, enabled, timeout_s, params_json, "
                "next_run_at, updated_at) VALUES (?, ?, ?, 1, ?, '{}', ?, ?)",
                (job, spec.cron, DEFAULT_TZ, spec.timeout_s,
                 utc_iso(next_run(spec.cron, DEFAULT_TZ, now)), utc_iso(now)),
            )
            added.append(job)
    return added


def get_schedules(db: Database) -> list[dict[str, Any]]:
    """Every schedule with its last run's status and times."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT s.*, r.status AS last_status, r.started_at AS last_started_at, "
            "r.finished_at AS last_finished_at FROM job_schedules s "
            "LEFT JOIN runs r ON r.id = s.last_run_id ORDER BY s.job"
        ).fetchall()
    return [dict(r) for r in rows]


def update_schedule(
    db: Database, job: str, *, cron: str | None = None, tz: str | None = None,
    enabled: bool | None = None, timeout_s: int | None = None, actor: str = "cli",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Change a schedule (validated, audited); returns the new row."""
    now = now or utc_now()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM job_schedules WHERE job = ?", (job,)).fetchone()
    if row is None:
        raise ScheduleError(f"no schedule for job {job!r} (known: {', '.join(JOBS)})")
    old = dict(row)
    new = dict(old)
    if cron is not None:
        if problem := validate_cron(cron):
            raise ScheduleError(problem)
        new["cron"] = cron
    if tz is not None:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ScheduleError(f"unknown time zone {tz!r}") from None
        new["tz"] = tz
    if enabled is not None:
        new["enabled"] = int(enabled)
    if timeout_s is not None:
        if not MIN_TIMEOUT_S <= timeout_s <= MAX_TIMEOUT_S:
            raise ScheduleError(f"timeout must be {MIN_TIMEOUT_S}-{MAX_TIMEOUT_S} seconds")
        new["timeout_s"] = timeout_s
    new["next_run_at"] = (utc_iso(next_run(new["cron"], new["tz"], now))
                          if new["enabled"] else None)
    new["updated_at"] = utc_iso(now)
    with db.ops_write() as conn:
        conn.execute(
            "UPDATE job_schedules SET cron = ?, tz = ?, enabled = ?, timeout_s = ?, "
            "next_run_at = ?, updated_at = ? WHERE job = ?",
            (new["cron"], new["tz"], new["enabled"], new["timeout_s"], new["next_run_at"],
             new["updated_at"], job),
        )
        audit = ("cron", "tz", "enabled", "timeout_s")
        conn.execute(
            "INSERT INTO settings_audit (ts, actor, entity, entity_key, old_json, new_json) "
            "VALUES (?, ?, 'schedule', ?, ?, ?)",
            (utc_iso(now), actor, job, json.dumps({k: old[k] for k in audit}),
             json.dumps({k: new[k] for k in audit})),
        )
    return new


# ---- requests and runs ------------------------------------------------------------

def trigger_kind(requested_by: str) -> str:
    """runs.trigger_kind for a request: automatic, startup or by a person."""
    if requested_by == "schedule" or requested_by.startswith("job:"):
        return "schedule"
    if requested_by == "startup":
        return "startup"
    return "manual"


def queue_request(
    db: Database, job: str, *, params: dict[str, Any] | None = None,
    requested_by: str, now: datetime | None = None,
) -> tuple[int, bool]:
    """Queue `job`; returns (request id, newly created)."""
    if job not in JOBS:
        raise ScheduleError(f"unknown job {job!r} (known: {', '.join(JOBS)})")
    params_json = json.dumps(params or {}, sort_keys=True)
    with db.ops_write() as conn:
        row = conn.execute(
            "SELECT id FROM job_requests WHERE job = ? AND status = 'queued' "
            "AND params_json = ? ORDER BY id LIMIT 1",
            (job, params_json),
        ).fetchone()
        if row:
            return row[0], False
        cur = conn.execute(
            "INSERT INTO job_requests (job, params_json, status, requested_by, requested_at) "
            "VALUES (?, ?, 'queued', ?, ?)",
            (job, params_json, requested_by, utc_iso(now or utc_now())),
        )
        return cur.lastrowid, True


def queue_due(db: Database, now: datetime | None = None) -> list[str]:
    """Queue every enabled schedule that is due; move each past `now`."""
    now = now or utc_now()
    with db.connect() as conn:
        due = conn.execute(
            "SELECT job, cron, tz, params_json, next_run_at FROM job_schedules "
            "WHERE enabled = 1 AND (next_run_at IS NULL OR next_run_at <= ?)",
            (utc_iso(now),),
        ).fetchall()
    queued = []
    for row in due:
        if row["job"] not in JOBS:
            continue  # a schedule left behind by a removed job
        if row["next_run_at"] is not None:  # NULL: just enabled, only compute the time
            queue_request(db, row["job"], params=json.loads(row["params_json"] or "{}"),
                          requested_by="schedule", now=now)
            queued.append(row["job"])
        with db.ops_write() as conn:
            conn.execute("UPDATE job_schedules SET next_run_at = ? WHERE job = ?",
                         (utc_iso(next_run(row["cron"], row["tz"], now)), row["job"]))
    return queued


def claim_next(db: Database, now: datetime | None = None) -> dict[str, Any] | None:
    """Atomically take the oldest queued request and open its run row."""
    now = now or utc_now()
    with db.ops_write() as conn:
        row = conn.execute(
            "UPDATE job_requests SET status = 'running' WHERE id = ("
            "SELECT id FROM job_requests WHERE status = 'queued' ORDER BY id LIMIT 1) "
            "RETURNING id, job, params_json, requested_by"
        ).fetchone()
        if row is None:
            return None
        request = dict(row)
        request["run_id"] = open_run(conn, request["job"], trigger_kind(request["requested_by"]),
                                     now)
        conn.execute("UPDATE job_requests SET run_id = ? WHERE id = ?",
                     (request["run_id"], request["id"]))
    return request


def open_run(conn: Any, job: str, trigger: str, now: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO runs (job, trigger_kind, status, started_at, code_sha) "
        "VALUES (?, ?, 'running', ?, ?)",
        (job, trigger, utc_iso(now), code_sha()),
    )
    conn.execute("UPDATE job_schedules SET last_run_id = ? WHERE job = ?", (cur.lastrowid, job))
    return cur.lastrowid


def finish_run(
    db: Database, run_id: int, *, status: str, exit_code: int | None,
    log_path: str | None = None, log_tail: str | None = None,
    request_id: int | None = None, now: datetime | None = None,
) -> None:
    with db.ops_write() as conn:
        conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, exit_code = ?, log_path = ?, "
            "log_tail = ? WHERE id = ?",
            (status, utc_iso(now or utc_now()), exit_code, log_path, log_tail, run_id),
        )
        if request_id is not None:
            conn.execute("UPDATE job_requests SET status = 'done' WHERE id = ?", (request_id,))


def mark_orphans(db: Database, now: datetime | None = None) -> int:
    """Runs left 'running' by a scheduler that died → failed. Returns count.

    Only called by a scheduler that holds the scheduler lock, so nothing
    it started is still alive (systemd kills the whole unit). Inline CLI
    runs (`capex server run --now`) belong to their own process: skipped.
    """
    stamp = utc_iso(now or utc_now())
    with db.ops_write() as conn:
        cur = conn.execute(
            "UPDATE runs SET status = 'failed', finished_at = ?, "
            "summary_json = COALESCE(summary_json, ?) "
            "WHERE status = 'running' AND trigger_kind != 'cli'",
            (stamp, json.dumps({"error": "orphaned: the scheduler stopped mid-run"})),
        )
        conn.execute("UPDATE job_requests SET status = 'done' WHERE status = 'running'")
        return cur.rowcount


def set_run_summary(db: Database, run_id: int, summary: dict[str, Any]) -> None:
    with db.ops_write() as conn:
        conn.execute("UPDATE runs SET summary_json = ? WHERE id = ?",
                     (json.dumps(summary, default=str, sort_keys=True), run_id))


def last_success(db: Database, job: str) -> str | None:
    """finished_at of `job`'s latest successful run, or None."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT finished_at FROM runs WHERE job = ? AND status IN ('success', 'partial') "
            "ORDER BY id DESC LIMIT 1", (job,),
        ).fetchone()
    return row[0] if row else None
