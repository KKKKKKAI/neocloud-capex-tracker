"""`capex server ...`: commands for the always-on host.

    capex server scheduler [--once]            the long-running scheduler
    capex server admin [--port 8081]            the admin panel (127.0.0.1; SSH tunnel)
    capex server init                           default schedules + watchlist rows
    capex server jobs                           schedules with next and last runs
    capex server schedule JOB [--cron EXPR] [--tz TZ] [--enable|--disable] [--timeout S]
    capex server run JOB [--params JSON] [--now]   queue a run (--now: run it here)
    capex server runs [--job JOB] [--limit N]   recent runs
    capex server log RUN_ID                     one run's log
    capex server publish [--dry-run] [--force]
    capex server backup [--raw] [--no-upload]
    capex server backups                        DB backups in the bucket
    capex server restore KEY|FILE --to PATH [--force]
    capex server health [--alert]
    capex server alert KEY SUBJECT BODY         email the operator (de-duplicated per KEY)
    capex server doctor [...]                   smoke test (python -m capex.server.doctor)
    capex server secrets fetch|check            SSM secrets (python -m capex.server.secrets)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def server_command(argv: list[str]) -> int:
    if argv and argv[0] == "doctor":
        from ..server.doctor import main as doctor_main
        return doctor_main(argv[1:])
    if argv and argv[0] == "secrets":
        from ..server.secrets import main as secrets_main
        return secrets_main(argv[1:])
    if argv and argv[0] == "scheduler":
        from ..server.scheduler import main as scheduler_main
        return scheduler_main(argv[1:])
    if argv and argv[0] == "admin":
        from ..server.admin.app import main as admin_main
        return admin_main(argv[1:])

    parser = argparse.ArgumentParser(prog="capex server", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="add default schedules and watchlist rows")
    sub.add_parser("jobs", help="schedules with next and last runs")
    p = sub.add_parser("schedule", help="change a job's schedule")
    p.add_argument("job")
    p.add_argument("--cron")
    p.add_argument("--tz")
    p.add_argument("--timeout", type=int)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--enable", action="store_true")
    g.add_argument("--disable", action="store_true")
    p = sub.add_parser("run", help="queue a job (or run it here with --now)")
    p.add_argument("job")
    p.add_argument("--params", default="{}")
    p.add_argument("--now", action="store_true")
    p = sub.add_parser("runs", help="recent runs")
    p.add_argument("--job")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("log", help="one run's log")
    p.add_argument("run_id", type=int)
    p = sub.add_parser("publish", help="publish the site now")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("backup", help="back up the DB (and raw filings)")
    p.add_argument("--raw", action="store_true", help="raw filings instead of the DB")
    p.add_argument("--no-upload", action="store_true")
    sub.add_parser("backups", help="DB backups in the bucket")
    p = sub.add_parser("restore", help="restore a DB backup to a path")
    p.add_argument("source", help="bucket key (db/capex-....db.gz) or local .db.gz")
    p.add_argument("--to", required=True, type=Path)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("health", help="health checks")
    p.add_argument("--alert", action="store_true", help="email the operator about problems")
    p = sub.add_parser("alert", help="email the operator (for scripts and systemd)")
    p.add_argument("key", help="de-duplication key, e.g. unit:capex-scheduler.service")
    p.add_argument("subject")
    p.add_argument("body")
    p.add_argument("--every-hours", type=float, default=6.0,
                   help="send the same key at most this often (default 6)")
    args = parser.parse_args(argv)
    return COMMANDS[args.cmd](args)


def _db():
    from ..db import Database
    return Database()


def _local(iso: str | None, tz: str = "Europe/London") -> str:
    if not iso:
        return "-"
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")


def cmd_init(args: argparse.Namespace) -> int:
    from ..monitor.watchlist import sync_watchlist
    from ..server import schedules

    db = _db()
    added = schedules.ensure_defaults(db)
    watched = sync_watchlist(db)
    print(f"schedules added: {', '.join(added) or 'none'}; watchlist rows added: {watched}")
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    from ..server import schedules

    rows = schedules.get_schedules(_db())
    if not rows:
        print("no schedules yet: run `capex server init`")
        return 0
    print(f"{'job':19} {'on':3} {'cron':15} {'next run':24} last run")
    for r in rows:
        last = (f"{r['last_status']} ({_local(r['last_started_at'], r['tz'])})"
                if r["last_status"] else "-")
        print(f"{r['job']:19} {'yes' if r['enabled'] else 'no':3} {r['cron']:15} "
              f"{_local(r['next_run_at'], r['tz']):24} {last}")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    from ..server import schedules

    enabled = True if args.enable else False if args.disable else None
    try:
        row = schedules.update_schedule(_db(), args.job, cron=args.cron, tz=args.tz,
                                        enabled=enabled, timeout_s=args.timeout)
    except schedules.ScheduleError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"{args.job}: cron {row['cron']} ({row['tz']}), "
          f"{'enabled' if row['enabled'] else 'disabled'}, timeout {row['timeout_s']}s, "
          f"next run {_local(row['next_run_at'], row['tz'])}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from ..server import schedules
    from ..server.jobs import run_job
    from ..server.scheduler import STATUS_BY_EXIT

    try:
        params = json.loads(args.params)
        assert isinstance(params, dict)
    except (ValueError, AssertionError):
        print("--params must be a JSON object", file=sys.stderr)
        return 2
    if args.job not in schedules.JOBS:
        print(f"unknown job {args.job!r} (known: {', '.join(schedules.JOBS)})", file=sys.stderr)
        return 2
    db = _db()
    if not args.now:
        request_id, created = schedules.queue_request(db, args.job, params=params,
                                                      requested_by="cli")
        print(f"{'queued' if created else 'already queued'}: {args.job} "
              f"(request {request_id}); the scheduler runs it within a minute")
        return 0
    now = schedules.utc_now()
    with db.ops_write() as conn:
        run_id = schedules.open_run(conn, args.job, "cli", now)
    code = run_job(args.job, db=db, run_id=run_id, params=params)
    status = "failed" if code == 77 else STATUS_BY_EXIT.get(code, "failed")
    schedules.finish_run(db, run_id, status=status, exit_code=code)
    print(f"run {run_id}: {args.job} {status} (exit {code})")
    return code


def cmd_runs(args: argparse.Namespace) -> int:
    sql = ("SELECT id, job, trigger_kind, status, started_at, finished_at, exit_code "
           "FROM runs")
    params: list = []
    if args.job:
        sql += " WHERE job = ?"
        params.append(args.job)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit)
    with _db().connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    for r in rows:
        took = ""
        if r["finished_at"]:
            secs = (datetime.fromisoformat(r["finished_at"])
                    - datetime.fromisoformat(r["started_at"])).total_seconds()
            took = f"{secs:.0f}s"
        print(f"{r['id']:6} {r['job']:19} {r['status']:9} {_local(r['started_at']):22} "
              f"{took:>6} exit={r['exit_code']} ({r['trigger_kind']})")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    with _db().connect() as conn:
        row = conn.execute("SELECT log_path, log_tail FROM runs WHERE id = ?",
                           (args.run_id,)).fetchone()
    if row is None:
        print(f"no run {args.run_id}", file=sys.stderr)
        return 1
    if row["log_path"] and Path(row["log_path"]).exists():
        print(Path(row["log_path"]).read_text(encoding="utf-8", errors="replace"), end="")
    else:
        print(row["log_tail"] or "(no log)")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    from ..server.locks import PIPELINE, LockBusyError, file_lock
    from ..server.publish import PublishError, publish_site

    try:
        with file_lock(PIPELINE, wait_s=0):
            summary = publish_site(dry_run=args.dry_run, force=args.force)
    except (LockBusyError, PublishError) as e:
        print(e, file=sys.stderr)
        return 1
    if summary["status"] == "skipped":
        print(f"skipped: {summary['reason']}")
        return 0
    if args.dry_run:
        print(f"would upload {len(summary['uploaded'])}, delete {len(summary['deleted'])}, "
              f"leave {summary['unchanged']} unchanged")
        for key in summary["uploaded"]:
            print(f"  put    {key}")
        for key in summary["deleted"]:
            print(f"  delete {key}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from ..server.backup import backup_db, sync_raw

    summary = sync_raw() if args.raw else backup_db(upload=not args.no_upload)
    print(json.dumps(summary, indent=2, default=str))
    return 0


def cmd_backups(args: argparse.Namespace) -> int:
    from ..server.backup import BackupError, list_backups

    try:
        rows = list_backups()
    except BackupError as e:
        print(e, file=sys.stderr)
        return 1
    for r in rows:
        print(f"{r['key']:48} {r['size'] / 2**20:8.1f} MiB  {r['last_modified']}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from ..server.backup import BackupError, restore_db

    try:
        restore_db(args.source, to=args.to, force=args.force)
    except BackupError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    from ..server import health

    db = _db()
    results = health.run_checks(db)
    for c in results:
        print(f"{c.level:8} {c.name:16} {c.detail}")
    if args.alert:
        health.alert_on(results, db=db)
    return health.exit_code(results)


def cmd_alert(args: argparse.Namespace) -> int:
    from datetime import timedelta

    from ..notify.ops import alert

    sent = alert(args.key, args.subject, args.body, db=_db(),
                 min_interval=timedelta(hours=args.every_hours))
    print("sent" if sent else "not sent (repeat within the interval, alerts off, "
                              "or no operator emails)")
    return 0


COMMANDS = {
    "init": cmd_init, "jobs": cmd_jobs, "schedule": cmd_schedule, "run": cmd_run,
    "runs": cmd_runs, "log": cmd_log, "publish": cmd_publish, "backup": cmd_backup,
    "backups": cmd_backups, "restore": cmd_restore, "health": cmd_health,
    "alert": cmd_alert,
}
