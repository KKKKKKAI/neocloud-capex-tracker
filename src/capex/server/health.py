"""Server health: `capex server health` and the hourly `health` job.

Each check is ok, warn or critical:
- the scheduler heartbeat;
- the Claude token's age (llm.token_created_at; renew around day 330);
- time since the last successful calendar sync, publish and backup;
- jobs whose latest run failed, an LLM pause or a nearly spent budget,
  filings that failed extraction;
- free disk, the claude binary, email and SEC contact configuration.

The job emails the operator about every check that isn't ok: once a day
per warning, every 6 hours per critical one (notify/ops.py).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .. import paths, settings
from ..db import Database
from . import schedules
from .scheduler import heartbeat_path

OK, WARN, CRITICAL = "ok", "warn", "critical"
TOKEN_WARN_DAYS, TOKEN_CRITICAL_DAYS = 330, 355
HEARTBEAT_WARN_S, HEARTBEAT_CRITICAL_S = 180, 900
# job: hours since its last success before (warn, critical)
FRESHNESS = {"calendar_sync": (36, 72), "publish": (3, 24), "backup": (36, 72)}
FAILED_FILINGS_DAYS = 14
DISK_WARN_GIB, DISK_CRITICAL_GIB = 2.0, 0.5


@dataclass
class Check:
    name: str
    level: str
    detail: str


def _age(now: datetime, iso: str) -> timedelta:
    return now - datetime.fromisoformat(iso)


def check_heartbeat(db: Database, now: datetime) -> Check:
    path = heartbeat_path()
    if not path.exists():
        return Check("heartbeat", CRITICAL,
                     "no scheduler heartbeat yet (is capex-scheduler running?)")
    age = now.timestamp() - path.stat().st_mtime
    level = CRITICAL if age > HEARTBEAT_CRITICAL_S else WARN if age > HEARTBEAT_WARN_S else OK
    return Check("heartbeat", level, f"scheduler last ticked {age:.0f}s ago")


def check_token(db: Database, now: datetime) -> Check:
    created = settings.get("llm.token_created_at", db)
    if not created:
        return Check("token", WARN, "llm.token_created_at is not set, so renewal can't be "
                                    "reminded (capex settings set llm.token_created_at DATE)")
    days = (now.date() - date.fromisoformat(created)).days
    level = CRITICAL if days >= TOKEN_CRITICAL_DAYS else WARN if days >= TOKEN_WARN_DAYS else OK
    hint = "" if level == OK else ": renew with `claude setup-token` (tokens last a year)"
    return Check("token", level, f"Claude token is {days} days old{hint}")


def check_freshness(db: Database, now: datetime) -> list[Check]:
    enabled = {s["job"] for s in schedules.get_schedules(db) if s["enabled"]}
    out = []
    for job, (warn_h, crit_h) in FRESHNESS.items():
        name = f"{job}_age"
        if job not in enabled:
            out.append(Check(name, OK, f"{job} is not scheduled"))
            continue
        if job == "publish" and not settings.get("publish.site_bucket", db):
            out.append(Check(name, OK, "publishing is not configured"))
            continue
        last = schedules.last_success(db, job)
        if last is None:
            out.append(Check(name, WARN, f"{job} has never succeeded"))
            continue
        hours = _age(now, last).total_seconds() / 3600
        level = CRITICAL if hours > crit_h else WARN if hours > warn_h else OK
        out.append(Check(name, level, f"{job} last succeeded {hours:.1f} h ago"))
    if "backup" in enabled and not settings.get("backup.bucket", db):
        out.append(Check("backup_target", WARN,
                         "backup.bucket is not set: DB backups stay on this disk only"))
    return out


def check_failed_jobs(db: Database, now: datetime) -> Check:
    failing = [f"{s['job']} ({s['last_status']})" for s in schedules.get_schedules(db)
               if s["last_status"] in ("failed", "timeout")]
    if failing:
        return Check("jobs", WARN, "latest run failed: " + ", ".join(failing))
    return Check("jobs", OK, "every job's latest run succeeded")


def check_llm(db: Database, now: datetime) -> Check:
    paused = settings.get("llm.paused_until", db)
    if paused and datetime.fromisoformat(paused) > now:
        return Check("llm", WARN, f"LLM calls paused until {paused} (usage limit)")
    budget = settings.get("llm.max_calls_per_day", db)
    with db.connect() as conn:
        used = conn.execute("SELECT COUNT(*) FROM llm_calls WHERE ts >= ?",
                            (now.strftime("%Y-%m-%dT00:00:00"),)).fetchone()[0]
    if budget and used >= 0.8 * budget:
        return Check("llm", WARN, f"{used} of {budget} LLM calls used today")
    return Check("llm", OK, f"{used} of {budget} LLM calls used today")


def check_failed_filings(db: Database, now: datetime) -> Check:
    since = schedules.utc_iso(now - timedelta(days=FAILED_FILINGS_DAYS))
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT ticker, form_type, period_of_report FROM filing_events "
            "WHERE status = 'failed' AND updated_at >= ? ORDER BY updated_at DESC",
            (since,),
        ).fetchall()
    if rows:
        listed = ", ".join(f"{r['ticker']} {r['form_type']} {r['period_of_report'] or ''}"
                           .strip() for r in rows[:5])
        return Check("filings", WARN, f"{len(rows)} filing(s) failed extraction in "
                                      f"{FAILED_FILINGS_DAYS} days: {listed}")
    return Check("filings", OK, "no failed filings")


def check_disk(db: Database, now: datetime) -> Check:
    home = paths.home()
    usage = shutil.disk_usage(home if home.exists() else paths.CODE_ROOT)
    free = usage.free / 2**30
    level = CRITICAL if free < DISK_CRITICAL_GIB else WARN if free < DISK_WARN_GIB else OK
    return Check("disk", level, f"{free:.1f} GiB free")


def check_claude(db: Database, now: datetime) -> Check:
    from ..adapters.cli_backend import claude_binary

    binary = claude_binary()
    if not binary:
        return Check("claude", CRITICAL, "claude CLI not found (CAPEX_CLAUDE_BIN)")
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True,
                             timeout=30, check=False)
        version = (out.stdout or out.stderr).strip().splitlines()[0] if out.returncode == 0 \
            else f"exit {out.returncode}"
    except (OSError, subprocess.SubprocessError, IndexError) as e:
        return Check("claude", CRITICAL, f"claude --version failed: {type(e).__name__}")
    return Check("claude", OK, version)


def check_email(db: Database, now: datetime) -> Check:
    missing = [k for k in ("GMAIL_USERNAME", "GMAIL_APP_PASSWORD") if not os.environ.get(k)]
    if missing:
        return Check("email", WARN, f"{' and '.join(missing)} not set: no subscriber or "
                                    "alert emails can be sent")
    if not settings.get("alerts.operator_emails", db):
        return Check("email", WARN, "alerts.operator_emails is empty: nobody gets alerts")
    return Check("email", OK, "SMTP credentials and operator emails configured")


def check_sec_contact(db: Database, now: datetime) -> Check:
    from ..fetch import get_user_agent

    agent = get_user_agent()
    if "@" not in agent:
        return Check("sec_contact", WARN, "SEC User-Agent has no contact email (CAPEX_FETCHER_UA)")
    return Check("sec_contact", OK, "SEC User-Agent carries a contact")


CHECKS: list[Callable[[Database, datetime], Check | list[Check]]] = [
    check_heartbeat, check_token, check_freshness, check_failed_jobs, check_llm,
    check_failed_filings, check_disk, check_claude, check_email, check_sec_contact,
]


def run_checks(db: Database | None = None, now: datetime | None = None) -> list[Check]:
    db = db or Database()
    now = now or datetime.now(timezone.utc)
    results: list[Check] = []
    for fn in CHECKS:
        try:
            out = fn(db, now)
        except Exception as e:  # a crashing check is a critical check
            out = Check(fn.__name__.removeprefix("check_"), CRITICAL,
                        f"check crashed: {type(e).__name__}: {e}")
        results.extend(out if isinstance(out, list) else [out])
    return results


def alert_on(results: list[Check], *, db: Database,
             log: Callable[[str], None] = print) -> int:
    """Email the operator about each non-ok check (de-duplicated). Returns emails sent."""
    from ..notify.ops import alert

    sent = 0
    for c in results:
        if c.level == OK:
            continue
        interval = timedelta(hours=6) if c.level == CRITICAL else timedelta(hours=24)
        if alert(f"health:{c.name}", f"{c.level}: {c.name}", f"{c.name}: {c.detail}",
                 db=db, min_interval=interval, log=log):
            sent += 1
    return sent


def exit_code(results: list[Check]) -> int:
    """1 if anything is critical, 3 (partial) for warnings, else 0."""
    levels = {c.level for c in results}
    return 1 if CRITICAL in levels else 3 if WARN in levels else 0
