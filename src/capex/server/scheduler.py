"""The always-on scheduler: `capex server scheduler [--once]`.

Startup: take the scheduler lock, refuse an unmigrated DB, mark runs
orphaned by a previous crash as failed, add missing default schedules.

Then every 30 s:
- touch the heartbeat file (read by the health check);
- queue due schedules (skipped while `scheduler.paused`; Run-now
  requests still run);
- claim one request and run it as a child process,
  `python -m capex.server.jobs <job> --run-id N`, logging to
  logs/runs/<N>.log. A job that overruns its timeout gets SIGTERM, then
  SIGKILL 30 s later.

One job runs at a time. On SIGTERM the scheduler stops claiming work and
exits once the current job ends (run it under systemd KillMode=mixed).
A failed or timed-out run, or a rejected Claude token, emails the
operator (de-duplicated per job).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import paths, settings
from ..db import Database
from ..db.schema import current_version, latest_version
from . import schedules
from .locks import SCHEDULER, LockBusyError, file_lock

TICK_S = 30
KILL_GRACE_S = 30
LOG_TAIL_BYTES = 4000
EXIT_AUTH = 77

# Job exit code → runs.status (anything else is 'failed').
STATUS_BY_EXIT = {0: "success", 3: "partial", 4: "skipped", 75: "deferred"}

JobCommand = Callable[[str, int, str], list[str]]


def default_job_command(job: str, run_id: int, params_json: str) -> list[str]:
    return [sys.executable, "-m", "capex.server.jobs", job,
            "--run-id", str(run_id), "--params", params_json]


def heartbeat_path() -> Path:
    return paths.run_dir() / "scheduler.heartbeat"


class SchemaNotMigratedError(RuntimeError):
    pass


def check_schema(db: Database) -> int:
    """The DB's schema version; SchemaNotMigratedError if the code expects newer."""
    current, latest = current_version(db), latest_version()
    if current < latest:
        raise SchemaNotMigratedError(
            f"DB schema is v{current}, the code needs v{latest}: run `capex db migrate`")
    return current


class Scheduler:
    def __init__(
        self,
        db: Database | None = None,
        *,
        job_command: JobCommand = default_job_command,
        log: Callable[[str], None] | None = None,
        tick_s: float = TICK_S,
        kill_grace_s: float = KILL_GRACE_S,
    ) -> None:
        self.db = db or Database()
        self.job_command = job_command
        self.log = log or (lambda msg: print(msg, flush=True))
        self.tick_s = tick_s
        self.kill_grace_s = kill_grace_s
        self.stopping = False

    # ---- lifecycle -------------------------------------------------------------
    def startup(self, now: datetime | None = None) -> None:
        version = check_schema(self.db)
        orphans = schedules.mark_orphans(self.db, now)
        added = schedules.ensure_defaults(self.db, now)
        self.log(f"scheduler up: schema v{version}, pid {os.getpid()}"
                 + (f", {orphans} orphaned run(s) marked failed" if orphans else "")
                 + (f", default schedules added: {', '.join(added)}" if added else ""))

    def request_stop(self, *_: Any) -> None:
        if not self.stopping:
            self.log("stop requested: finishing the current job, claiming nothing new")
        self.stopping = True

    def serve(self) -> int:
        """Run until SIGTERM/SIGINT. Returns the process exit code."""
        try:
            with file_lock(SCHEDULER):
                signal.signal(signal.SIGTERM, self.request_stop)
                signal.signal(signal.SIGINT, self.request_stop)
                self.startup()
                while not self.stopping:
                    if not self.tick():
                        self._sleep(self.tick_s)
        except LockBusyError:
            self.log("another scheduler is already running")
            return 1
        except SchemaNotMigratedError as e:
            self.log(str(e))
            return 1
        self.log("scheduler stopped")
        return 0

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))

    # ---- one tick ----------------------------------------------------------------
    def tick(self, now: datetime | None = None) -> bool:
        """Heartbeat, queue due schedules, run at most one request.
        Returns True when a job ran."""
        now = now or schedules.utc_now()
        self.heartbeat(now)
        if not settings.get("scheduler.paused", self.db):  # paused: Run-now still runs
            for job in schedules.queue_due(self.db, now):
                self.log(f"queued {job} (schedule)")
        if self.stopping:
            return False
        request = schedules.claim_next(self.db, now)
        if request is None:
            return False
        self.run_request(request)
        return True

    def heartbeat(self, now: datetime) -> None:
        path = heartbeat_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{schedules.utc_iso(now)} pid={os.getpid()}\n", encoding="ascii")

    def run_request(self, request: dict[str, Any]) -> str:
        """Run one claimed request to completion; returns the run status."""
        job, run_id = request["job"], request["run_id"]
        timeout_s = self._timeout(job)
        log_dir = paths.logs_dir() / "runs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{run_id}.log"
        self.log(f"run {run_id}: {job} started ({request['requested_by']}, "
                 f"timeout {timeout_s}s)")
        started = time.monotonic()
        exit_code: int | None
        if job not in schedules.JOBS:
            log_path.write_text(f"unknown job {job!r}\n", encoding="utf-8")
            status, exit_code = "failed", None
        else:
            status, exit_code = self._run_child(
                self.job_command(job, run_id, request["params_json"] or "{}"),
                log_path, run_id, timeout_s)
        tail = _tail(log_path)
        schedules.finish_run(self.db, run_id, status=status, exit_code=exit_code,
                             log_path=str(log_path), log_tail=tail,
                             request_id=request["id"])
        self.log(f"run {run_id}: {job} {status} (exit {exit_code}) "
                 f"in {time.monotonic() - started:.0f}s")
        self._alert(job, run_id, status, exit_code, tail)
        return status

    def _timeout(self, job: str) -> int:
        with self.db.connect() as conn:
            row = conn.execute("SELECT timeout_s FROM job_schedules WHERE job = ?",
                               (job,)).fetchone()
        if row:
            return int(row[0])
        spec = schedules.JOBS.get(job)
        return spec.timeout_s if spec else 3600

    def _run_child(self, cmd: list[str], log_path: Path, run_id: int,
                   timeout_s: float) -> tuple[str, int | None]:
        env = dict(os.environ, CAPEX_RUN_ID=str(run_id), PYTHONUNBUFFERED="1")
        with open(log_path, "ab") as log_file:
            try:
                proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, env=env,
                                        start_new_session=True)
            except OSError as e:
                log_file.write(f"could not start: {e}\n".encode())
                return "failed", None
            try:
                code = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                log_file.write(f"\n[scheduler] timeout after {timeout_s}s: SIGTERM\n".encode())
                log_file.flush()
                _signal_group(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=self.kill_grace_s)
                except subprocess.TimeoutExpired:
                    _signal_group(proc, signal.SIGKILL)
                    proc.wait()
                return "timeout", proc.returncode
        if code == EXIT_AUTH:
            return "failed", code
        return STATUS_BY_EXIT.get(code, "failed"), code

    def _alert(self, job: str, run_id: int, status: str, exit_code: int | None,
               tail: str) -> None:
        from ..notify.ops import alert

        if exit_code == EXIT_AUTH:
            alert("llm-auth", "Claude rejected the token: extraction is stopped",
                  f"Run {run_id} ({job}) exited 77: the Claude token is missing, "
                  "invalid or expired.\n\nRenew it: `claude setup-token`, save it to "
                  "the SSM parameter /capex/CLAUDE_CODE_OAUTH_TOKEN, then "
                  "`sudo systemctl restart capex-secrets`.\n\nLast log lines:\n" + tail,
                  db=self.db, log=self.log)
        elif status in ("failed", "timeout"):
            alert(f"job:{job}", f"{job} {status} (run {run_id})",
                  f"Run {run_id} of {job} ended {status} (exit {exit_code}).\n\n"
                  f"Last log lines:\n{tail}", db=self.db, log=self.log)


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, AttributeError):
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass


def _tail(path: Path, limit: int = LOG_TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - limit))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="capex server scheduler")
    parser.add_argument("--once", action="store_true",
                        help="one tick: queue due schedules, run at most one request, exit")
    args = parser.parse_args(argv)
    scheduler = Scheduler()
    if not args.once:
        return scheduler.serve()
    try:
        with file_lock(SCHEDULER):
            scheduler.startup()
            ran = scheduler.tick()
    except (LockBusyError, SchemaNotMigratedError) as e:
        print(e, file=sys.stderr)
        return 1
    print(json.dumps({"ran": ran}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
