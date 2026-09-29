"""Job runners, health checks and operator alerts."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from capex import paths, settings
from capex.notify import ops
from capex.server import health, jobs, schedules

UTC = timezone.utc
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _requests(db):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT job, requested_by FROM job_requests")]


# ---- jobs ----------------------------------------------------------------------------

def _fake_watch_summary(**kw):
    base = dict(due=1, polls={"hit": 1}, discovered=["MSFT 10-Q"], swept=[], stale=0,
                outcomes=[], stopped=None, outputs_regenerated=True, notify=None)
    base.update(kw)
    return SimpleNamespace(**base, exit_code=lambda: 0)


def test_watcher_job_queues_a_publish_after_new_outputs(capex_db, monkeypatch):
    calls = []
    monkeypatch.setattr("capex.monitor.pipeline.run_watcher",
                        lambda **kw: calls.append(kw) or _fake_watch_summary())
    with capex_db.ops_write() as conn:
        run_id = schedules.open_run(conn, "watcher", "schedule", NOW)
    assert jobs.run_job("watcher", db=capex_db, run_id=run_id) == 0
    assert calls[0]["sweep"] is False
    assert _requests(capex_db) == [{"job": "publish", "requested_by": "job:watcher"}]
    with capex_db.connect() as conn:
        summary = json.loads(conn.execute("SELECT summary_json FROM runs WHERE id = ?",
                                          (run_id,)).fetchone()[0])
    assert summary["discovered"] == ["MSFT 10-Q"]


def test_sweep_job_is_a_watcher_run_with_the_sweep(capex_db, monkeypatch):
    calls = []
    monkeypatch.setattr("capex.monitor.pipeline.run_watcher",
                        lambda **kw: calls.append(kw) or _fake_watch_summary(
                            outputs_regenerated=False))
    assert jobs.run_job("filings_sweep", db=capex_db) == 0
    assert calls[0]["sweep"] is True and _requests(capex_db) == []


def test_a_busy_pipeline_defers_the_job(capex_db, monkeypatch):
    monkeypatch.setattr(jobs, "PIPELINE_WAIT_S", 0)
    monkeypatch.setattr("capex.monitor.pipeline.run_watcher",
                        lambda **kw: pytest.fail("must not run"))
    import subprocess
    import sys
    holder = subprocess.Popen(  # another process holds the pipeline lock
        [sys.executable, "-c",
         "import time; from capex.server.locks import file_lock, PIPELINE\n"
         "with file_lock(PIPELINE):\n    print('held', flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE, env=dict(os.environ, PYTHONPATH="src"), text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert jobs.run_job("watcher", db=capex_db) == jobs.EXIT_DEFERRED
    finally:
        holder.kill()
        holder.wait()


def test_crashing_jobs_exit_1_and_record_the_error(capex_db, monkeypatch):
    def boom(**kw):
        raise ValueError("parser exploded")
    monkeypatch.setattr("capex.monitor.pipeline.run_watcher", boom)
    with capex_db.ops_write() as conn:
        run_id = schedules.open_run(conn, "watcher", "schedule", NOW)
    assert jobs.run_job("watcher", db=capex_db, run_id=run_id) == jobs.EXIT_ERROR
    with capex_db.connect() as conn:
        summary = json.loads(conn.execute("SELECT summary_json FROM runs WHERE id = ?",
                                          (run_id,)).fetchone()[0])
    assert "parser exploded" in summary["error"]


def test_regenerate_exports_a_workbook_only_when_data_changed(capex_db, monkeypatch):
    from capex.monitor import pipeline

    exported = []
    monkeypatch.setattr("capex.exporters.excel.export_workbook",
                        lambda: exported.append(1) or paths.workbook_dir() / "wb.xlsx")
    monkeypatch.setattr("capex.extract.reconcile.reconcile",
                        lambda write: SimpleNamespace(derived=0, conflicts=0, unresolved=0))
    for name in ("charts.generate_all_metric_charts", "interactive_chart.generate_all_interactive",
                 "dashboard_html.generate_dashboard_html",
                 "earnings_calendar_html.generate_earnings_calendar_html",
                 "treatments_html.generate_treatments_html"):
        monkeypatch.setattr(f"capex.exporters.{name}", lambda: None)
    assert pipeline.regenerate_outputs(lambda _: None)["workbook"] == "wb.xlsx"
    assert pipeline.regenerate_outputs(lambda _: None)["workbook"] is None     # quiet day
    assert jobs.run_job("regenerate_outputs", db=capex_db,
                        params={"workbook": True}) == jobs.EXIT_OK
    assert len(exported) == 2
    assert {"job": "publish", "requested_by": "job:regenerate_outputs"} in _requests(capex_db)


def test_publish_job_skips_until_configured(capex_db):
    assert jobs.run_job("publish", db=capex_db) == jobs.EXIT_SKIPPED


def test_llm_check_job(capex_db, fake_claude, monkeypatch):
    assert jobs.run_job("llm_check", db=capex_db) == jobs.EXIT_OK
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "auth")
    assert jobs.run_job("llm_check", db=capex_db) == jobs.EXIT_AUTH


def test_prune_job(capex_db):
    settings.set("prune.keep_workbooks", 2, db=capex_db)
    books = paths.workbook_dir()
    books.mkdir(parents=True)
    names = [f"[2026.10.{d} - 07h00] financials sourcebook.xlsx" for d in (27, 28, 29)]
    for name in names:
        (books / name).write_bytes(b"x")
    logs = paths.logs_dir() / "runs"
    logs.mkdir(parents=True)
    old_log = logs / "1.log"
    old_log.write_text("old")
    stamp = (NOW - timedelta(days=90)).timestamp()
    os.utime(old_log, (stamp, stamp))
    (logs / "2.log").write_text("recent")
    assert jobs.run_job("prune", db=capex_db) == jobs.EXIT_OK
    assert sorted(p.name for p in books.iterdir()) == names[1:]
    assert [p.name for p in logs.iterdir()] == ["2.log"]
    assert {"job": "publish", "requested_by": "job:prune"} in _requests(capex_db)


def test_every_scheduled_job_has_a_runner():
    assert set(jobs.RUNNERS) == set(schedules.JOBS)


# ---- health ---------------------------------------------------------------------------

def _levels(results):
    return {c.name: c.level for c in results}


def test_token_age_levels(capex_db):
    assert health.check_token(capex_db, NOW).level == health.WARN            # unknown date
    for days, level in ((10, "ok"), (331, "warn"), (356, "critical")):
        settings.set("llm.token_created_at", (NOW - timedelta(days=days)).date().isoformat(),
                     db=capex_db)
        assert health.check_token(capex_db, NOW).level == level


def test_freshness_from_runs(capex_db):
    schedules.ensure_defaults(capex_db, NOW)
    settings.set("publish.site_bucket", "b", db=capex_db)
    with capex_db.ops_write() as conn:
        for job, hours in (("calendar_sync", 30), ("publish", 30)):
            run_id = schedules.open_run(conn, job, "schedule", NOW - timedelta(hours=hours))
            conn.execute("UPDATE runs SET status = 'success', finished_at = ? WHERE id = ?",
                         (schedules.utc_iso(NOW - timedelta(hours=hours)), run_id))
    levels = _levels(health.check_freshness(capex_db, NOW))
    assert levels == {"calendar_sync_age": "ok", "publish_age": "critical",
                      "backup_age": "warn", "backup_target": "warn"}


def test_failed_jobs_and_filings(capex_db):
    schedules.ensure_defaults(capex_db, NOW)
    with capex_db.ops_write() as conn:
        run_id = schedules.open_run(conn, "watcher", "schedule", NOW)
        conn.execute("UPDATE runs SET status = 'failed' WHERE id = ?", (run_id,))
        conn.execute(
            "INSERT INTO filing_events (ticker, form_type, accession_number, filing_date, "
            "period_of_report, status, discovered_by, discovered_at, updated_at) VALUES "
            "('GDS', '6-K', 'acc', '2026-10-20', '2026-09-30', 'failed', 'calendar', 'x', ?)",
            (schedules.utc_iso(NOW),))
    assert "watcher (failed)" in health.check_failed_jobs(capex_db, NOW).detail
    filings = health.check_failed_filings(capex_db, NOW)
    assert filings.level == "warn" and "GDS 6-K 2026-09-30" in filings.detail


def test_heartbeat_levels(capex_db):
    from capex.server.scheduler import heartbeat_path

    assert health.check_heartbeat(capex_db, NOW).level == health.CRITICAL
    path = heartbeat_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    now = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    assert health.check_heartbeat(capex_db, now + timedelta(seconds=30)).level == "ok"
    assert health.check_heartbeat(capex_db, now + timedelta(minutes=30)).level == "critical"


def test_health_job_alerts_and_exit_codes(capex_db, monkeypatch):
    sent = []
    monkeypatch.setattr("capex.notify.ops.send_email", lambda **kw: sent.append(kw))
    settings.set("alerts.operator_emails", ["ops@example.com"], db=capex_db)
    monkeypatch.setattr(health, "CHECKS", [
        lambda db, now: health.Check("disk", health.CRITICAL, "0.1 GiB free"),
        lambda db, now: health.Check("token", health.OK, "fine"),
    ])
    assert jobs.run_job("health", db=capex_db) == jobs.EXIT_PARTIAL   # findings, not a crash
    assert len(sent) == 1 and "disk" in sent[0]["subject"]
    assert jobs.run_job("health", db=capex_db) == jobs.EXIT_PARTIAL
    assert len(sent) == 1                                   # de-duplicated for 6 hours
    assert health.exit_code(health.run_checks(capex_db, NOW)) == 1    # the CLI still says 1


def test_a_crashing_check_is_critical(capex_db, monkeypatch):
    def broken(db, now):
        raise RuntimeError("nope")
    monkeypatch.setattr(health, "CHECKS", [broken])
    (result,) = health.run_checks(capex_db, NOW)
    assert result.level == health.CRITICAL and "nope" in result.detail


# ---- operator alerts -----------------------------------------------------------------

def test_alerts_are_deduplicated_and_never_raise(capex_db):
    sent = []

    def send(**kw):
        sent.append(kw)

    kw = dict(db=capex_db, send_fn=send, log=lambda _: None)
    assert ops.alert("job:watcher", "watcher failed", "details", now=NOW, **kw) is False
    settings.set("alerts.operator_emails", ["a@example.com", "b@example.com"], db=capex_db)
    assert ops.alert("job:watcher", "watcher failed", "details", now=NOW, **kw) is True
    assert [m["to_email"] for m in sent] == ["a@example.com", "b@example.com"]
    assert sent[0]["subject"] == "[capex] watcher failed"
    assert ops.alert("job:watcher", "again", "x", now=NOW + timedelta(hours=1), **kw) is False
    assert ops.alert("job:watcher", "again", "x", now=NOW + timedelta(hours=7), **kw) is True
    with capex_db.connect() as conn:
        assert conn.execute("SELECT count FROM alerts_sent").fetchone()[0] == 3

    def down(**kw):
        raise OSError("smtp down")
    assert ops.alert("health:disk", "disk", "x", db=capex_db, send_fn=down, now=NOW,
                     log=lambda _: None) is False
    settings.set("alerts.enabled", False, db=capex_db)
    assert ops.alert("health:x", "s", "b", now=NOW, **kw) is False
