"""Schedules, requests and the scheduler loop (jobs faked as tiny processes)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from capex import settings
from capex.server import schedules
from capex.server.scheduler import Scheduler, SchemaNotMigratedError, check_schema

UTC = timezone.utc
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _schedule(db, job):
    with db.connect() as conn:
        return dict(conn.execute("SELECT * FROM job_schedules WHERE job = ?", (job,)).fetchone())


def _requests(db):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM job_requests ORDER BY id")]


def _runs(db):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")]


# ---- cron and time zones ---------------------------------------------------------------

def test_cron_validation():
    assert schedules.validate_cron("*/20 * * * *") is None
    assert "5 fields" in schedules.validate_cron("0 7 * *")
    assert "not a valid" in schedules.validate_cron("61 7 * * *")


@pytest.mark.parametrize("after, expected", [
    # 07:00 London is 06:00 UTC in summer time...
    (datetime(2026, 3, 28, 12, tzinfo=UTC), datetime(2026, 3, 29, 6, tzinfo=UTC)),
    # ...and 07:00 UTC after the clocks go back.
    (datetime(2026, 10, 25, 3, tzinfo=UTC), datetime(2026, 10, 25, 7, tzinfo=UTC)),
])
def test_next_run_keeps_local_wall_time_across_dst(after, expected):
    assert schedules.next_run("0 7 * * *", "Europe/London", after) == expected


# ---- schedules and requests -------------------------------------------------------------

def test_default_schedules_are_added_once(capex_db):
    added = schedules.ensure_defaults(capex_db, NOW)
    assert set(added) == set(schedules.JOBS)
    watcher = _schedule(capex_db, "watcher")
    assert (watcher["cron"], watcher["tz"], watcher["enabled"]) == (
        "*/20 * * * *", "Europe/London", 1)
    assert watcher["next_run_at"] == "2026-10-30T12:20:00+00:00"
    schedules.update_schedule(capex_db, "watcher", cron="*/30 * * * *", now=NOW)
    assert schedules.ensure_defaults(capex_db, NOW) == []           # edits survive
    assert _schedule(capex_db, "watcher")["cron"] == "*/30 * * * *"


def test_missed_runs_collapse_into_one(capex_db):
    schedules.ensure_defaults(capex_db, NOW - timedelta(days=3))    # down for three days
    queued = schedules.queue_due(capex_db, NOW)
    assert "watcher" in queued and queued.count("watcher") == 1
    assert [r["job"] for r in _requests(capex_db)].count("watcher") == 1
    assert _schedule(capex_db, "watcher")["next_run_at"] == "2026-10-30T12:20:00+00:00"
    assert schedules.queue_due(capex_db, NOW) == []                 # nothing due twice


def test_a_job_never_has_two_identical_requests_waiting(capex_db):
    first = schedules.queue_request(capex_db, "publish", requested_by="job:watcher")
    again = schedules.queue_request(capex_db, "publish", requested_by="cli")
    other = schedules.queue_request(capex_db, "publish", params={"force": True},
                                    requested_by="cli")
    assert first == (first[0], True) and again == (first[0], False) and other[1]
    with pytest.raises(schedules.ScheduleError):
        schedules.queue_request(capex_db, "nope", requested_by="cli")


def test_claiming_is_atomic_and_opens_a_run(capex_db):
    schedules.ensure_defaults(capex_db, NOW)
    schedules.queue_request(capex_db, "health", requested_by="schedule")
    first = schedules.claim_next(capex_db, NOW)
    assert first["job"] == "health"
    assert schedules.claim_next(capex_db, NOW) is None               # already taken
    (run,) = _runs(capex_db)
    assert (run["id"], run["status"], run["trigger_kind"]) == (first["run_id"], "running",
                                                               "schedule")
    assert _schedule(capex_db, "health")["last_run_id"] == run["id"]


def test_update_schedule_validates_and_audits(capex_db):
    schedules.ensure_defaults(capex_db, NOW)
    with pytest.raises(schedules.ScheduleError):
        schedules.update_schedule(capex_db, "backup", cron="every day")
    with pytest.raises(schedules.ScheduleError):
        schedules.update_schedule(capex_db, "backup", tz="Mars/Olympus")
    with pytest.raises(schedules.ScheduleError):
        schedules.update_schedule(capex_db, "backup", timeout_s=5)
    row = schedules.update_schedule(capex_db, "backup", enabled=False, actor="kai", now=NOW)
    assert row["next_run_at"] is None
    with capex_db.connect() as conn:
        audit = conn.execute("SELECT actor, entity, entity_key, new_json FROM settings_audit "
                             "WHERE entity = 'schedule'").fetchone()
    assert (audit["actor"], audit["entity_key"]) == ("kai", "backup")
    assert json.loads(audit["new_json"])["enabled"] == 0


def test_trigger_kinds():
    assert [schedules.trigger_kind(b) for b in ("schedule", "job:watcher", "cli", "admin:kai",
                                                "startup")] == [
        "schedule", "schedule", "manual", "manual", "startup"]


# ---- the scheduler loop ----------------------------------------------------------------

def _python(code: str):
    """A job command that runs `code` instead of a real job."""
    return lambda job, run_id, params: [sys.executable, "-c", code]


@pytest.fixture
def alerts(monkeypatch, capex_db):
    sent = []
    monkeypatch.setattr("capex.notify.ops.send_email", lambda **kw: sent.append(kw))
    settings.set("alerts.operator_emails", ["ops@example.com"], db=capex_db)
    return sent


def _scheduler(db, code, **kw):
    return Scheduler(db, job_command=_python(code), log=lambda _: None, **kw)


@pytest.mark.parametrize("code, status", [
    ("print('fine')", "success"),
    ("import sys; sys.exit(3)", "partial"),
    ("import sys; sys.exit(4)", "skipped"),
    ("import sys; sys.exit(75)", "deferred"),
    ("raise SystemExit('boom')", "failed"),
])
def test_exit_codes_become_run_statuses(capex_db, alerts, code, status):
    sched = _scheduler(capex_db, code)
    sched.startup(NOW)
    schedules.queue_request(capex_db, "health", requested_by="cli")
    assert sched.tick(NOW) is True
    (run,) = _runs(capex_db)
    assert run["status"] == status and run["finished_at"]
    assert _requests(capex_db)[-1]["status"] == "done"
    assert bool(alerts) == (status == "failed")          # only failures email the operator


def test_run_log_and_tail_are_kept(capex_db, alerts):
    sched = _scheduler(capex_db, "print('line one'); print('line two')")
    sched.startup(NOW)
    schedules.queue_request(capex_db, "health", requested_by="cli")
    sched.tick(NOW)
    (run,) = _runs(capex_db)
    assert "line two" in run["log_tail"] and run["log_path"].endswith(f"{run['id']}.log")


def test_a_rejected_token_sends_the_auth_alert(capex_db, alerts):
    sched = _scheduler(capex_db, "import sys; sys.exit(77)")
    sched.startup(NOW)
    schedules.queue_request(capex_db, "watcher", requested_by="cli")
    sched.tick(NOW)
    assert _runs(capex_db)[0]["status"] == "failed"
    (mail,) = alerts
    assert "token" in mail["subject"] and "setup-token" in mail["text_body"]


def test_overrunning_jobs_are_killed(capex_db, alerts):
    sched = _scheduler(capex_db, "import time; time.sleep(60)", kill_grace_s=2)
    sched.startup(NOW)
    with capex_db.ops_write() as conn:
        conn.execute("UPDATE job_schedules SET timeout_s = 1 WHERE job = 'prune'")
    schedules.queue_request(capex_db, "prune", requested_by="cli")
    sched.tick(NOW)
    (run,) = _runs(capex_db)
    assert run["status"] == "timeout"
    assert "timeout after 1s" in run["log_tail"]
    assert alerts and "timeout" in alerts[0]["subject"]


def test_schedules_are_queued_by_ticks_unless_paused(capex_db, alerts):
    sched = _scheduler(capex_db, "print('ok')")
    sched.startup(NOW - timedelta(hours=2))
    settings.set("scheduler.paused", True, db=capex_db)
    assert sched.tick(NOW) is False and _requests(capex_db) == []
    schedules.queue_request(capex_db, "backup", requested_by="cli")   # Run now still works
    assert sched.tick(NOW) is True
    settings.set("scheduler.paused", False, db=capex_db)
    sched.tick(NOW)
    assert {r["job"] for r in _requests(capex_db)} >= {"backup", "watcher", "health"}


def test_startup_marks_orphans_and_refuses_an_old_schema(capex_db):
    with capex_db.ops_write() as conn:
        schedules.open_run(conn, "watcher", "schedule", NOW)
        schedules.open_run(conn, "backup", "cli", NOW)           # someone's inline run
    _scheduler(capex_db, "").startup(NOW)
    statuses = {r["job"]: r["status"] for r in _runs(capex_db)}
    assert statuses == {"watcher": "failed", "backup": "running"}
    with capex_db.ops_write() as conn:
        conn.execute("DELETE FROM schema_version WHERE version = (SELECT MAX(version) "
                     "FROM schema_version)")
    with pytest.raises(SchemaNotMigratedError):
        check_schema(capex_db)


def test_unknown_jobs_fail_without_running(capex_db, alerts):
    sched = _scheduler(capex_db, "raise SystemExit('should not run')")
    sched.startup(NOW)
    with capex_db.ops_write() as conn:
        conn.execute("INSERT INTO job_requests (job, requested_by, requested_at) "
                     "VALUES ('retired_job', 'cli', 'x')")
    sched.tick(NOW)
    (run,) = _runs(capex_db)
    assert (run["job"], run["status"], run["exit_code"]) == ("retired_job", "failed", None)


def test_heartbeat_is_written_every_tick(capex_db):
    from capex.server.scheduler import heartbeat_path

    _scheduler(capex_db, "").tick(NOW)
    assert heartbeat_path().read_text(encoding="ascii").startswith("2026-10-30T12:00:00")
