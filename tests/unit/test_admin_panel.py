"""The admin panel: guards, pages and every action (FastAPI TestClient)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from capex import paths, settings
from capex.server import schedules
from capex.server.admin import create_app

BASE = "http://localhost:8081"
PAGES = ["/", "/companies", "/calendar", "/schedule", "/runs", "/notifications", "/settings",
         "/audit"]


@pytest.fixture
def panel(capex_db):
    schedules.ensure_defaults(capex_db)
    app = create_app(lambda: capex_db, port=8081)
    client = TestClient(app, base_url=BASE, follow_redirects=False)
    return SimpleNamespace(client=client, db=capex_db, csrf=app.state.csrf, app=app)


def post(panel, path, headers=None, **data):
    return panel.client.post(path, data={"csrf": panel.csrf, **data},
                             headers=headers if headers is not None else {"Origin": BASE})


def _audit(db, entity):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM settings_audit WHERE entity = ? ORDER BY id", (entity,))]


def _requests(db):
    with db.connect() as conn:
        return [(r["job"], json.loads(r["params_json"]), r["requested_by"])
                for r in conn.execute("SELECT * FROM job_requests ORDER BY id")]


# ---- guards --------------------------------------------------------------------------

@pytest.mark.parametrize("base", ["http://evil.example:8081", "http://localhost",
                                  "http://localhost:9999"])
def test_foreign_hosts_are_refused(panel, base):
    response = TestClient(panel.app, base_url=base).get("/")
    assert response.status_code == 403 and "SSH tunnel" in response.text


def test_cross_site_and_tokenless_posts_are_refused(panel):
    assert post(panel, "/scheduler/pause", headers={"Origin": "http://evil.example"}
                ).status_code == 403
    assert post(panel, "/scheduler/pause", headers={}).status_code == 403   # no Origin/Referer
    bad_token = panel.client.post("/scheduler/pause", data={"csrf": "guess"},
                                  headers={"Origin": BASE})
    assert bad_token.status_code == 403
    assert settings.get("scheduler.paused", panel.db) is False
    ok = post(panel, "/scheduler/pause", headers={"Referer": f"{BASE}/schedule"})
    assert ok.status_code == 303 and settings.get("scheduler.paused", panel.db) is True


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_with_security_headers(panel, path):
    response = panel.client.get(path)
    assert response.status_code == 200, response.text
    assert "capex admin" in response.text and panel.csrf in response.text or path in (
        "/runs", "/audit")
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "<script" not in response.text


def test_redirects_stay_on_the_panel(panel):
    response = post(panel, "/jobs/publish/run", next="//evil.example/steal")
    assert response.headers["location"].startswith("/?msg=")


# ---- companies, jobs, schedules --------------------------------------------------------

def test_company_edits_are_validated_and_audited(panel):
    from capex.monitor.watchlist import get_entry

    response = post(panel, "/companies/MSFT", quarterly_form="10-Q", annual_form="10-K",
                    notes="watch capex guidance")                    # watch box unticked
    assert response.status_code == 303 and "msg=" in response.headers["location"]
    entry = get_entry("MSFT", panel.db)
    assert (entry["watch"], entry["notes"]) == (0, "watch capex guidance")
    (row,) = _audit(panel.db, "watchlist")
    assert (row["actor"], row["entity_key"]) == ("admin", "MSFT")
    bad = post(panel, "/companies/MSFT", watch="on", quarterly_form="10-X", annual_form="")
    assert "err=" in bad.headers["location"] and get_entry("MSFT", panel.db)["watch"] == 0


def test_run_now_buttons_queue_requests(panel):
    post(panel, "/jobs/publish/run")
    post(panel, "/companies/MSFT/check")
    post(panel, "/companies/BIDU/fetch", form="6-K")
    post(panel, "/jobs/publish/run")                                 # already queued
    assert _requests(panel.db) == [
        ("publish", {}, "admin"),
        ("watcher", {"tickers": ["MSFT"]}, "admin"),
        ("watcher", {"form": "6-K", "ticker": "BIDU"}, "admin"),
    ]
    assert "err=" in post(panel, "/jobs/nonsense/run").headers["location"]


def test_schedule_edits(panel):
    post(panel, "/schedule/watcher", cron="*/30 * * * *", enabled="on", timeout_s="3600")
    post(panel, "/schedule/backup", cron="15 3 * * *", preset="0 7 * * *", enabled="on")
    post(panel, "/schedule/prune", cron="30 4 * * 0")                # box unticked: off
    rows = {s["job"]: s for s in schedules.get_schedules(panel.db)}
    assert (rows["watcher"]["cron"], rows["watcher"]["timeout_s"]) == ("*/30 * * * *", 3600)
    assert rows["backup"]["cron"] == "0 7 * * *"                     # the preset wins
    assert (rows["prune"]["enabled"], rows["prune"]["next_run_at"]) == (0, None)
    bad = post(panel, "/schedule/watcher", cron="every minute", enabled="on")
    assert "err=" in bad.headers["location"]
    assert len(_audit(panel.db, "schedule")) == 3


# ---- settings and notifications --------------------------------------------------------

def test_settings_edits(panel):
    post(panel, "/settings/llm.max_calls_per_day", value="80")
    assert settings.get("llm.max_calls_per_day", panel.db) == 80
    assert "err=" in post(panel, "/settings/llm.max_calls_per_day",
                          value="lots").headers["location"]
    post(panel, "/settings/alerts.operator_emails", value='["ops@example.com"]')
    assert settings.get("alerts.operator_emails", panel.db) == ["ops@example.com"]
    post(panel, "/settings/llm.max_calls_per_day", value="80", reset="1")
    assert settings.get("llm.max_calls_per_day", panel.db) == 150
    post(panel, "/llm/pause", hours="2")
    assert settings.get("llm.paused_until", panel.db)
    post(panel, "/llm/resume")
    assert settings.get("llm.paused_until", panel.db) == ""


def test_subscribers_through_the_panel(panel, monkeypatch):
    from capex.notify.subscribers import load_subscribers

    monkeypatch.delenv("NOTIFY_SUBSCRIBERS_PATH", raising=False)
    post(panel, "/notifications/subscribers", email="ann@example.com", tickers="msft, googl",
         metrics="*")
    (sub,) = load_subscribers(db=panel.db)
    assert (sub.tickers, sub.enabled) == (["MSFT", "GOOGL"], True)
    post(panel, "/notifications/subscribers/toggle", email="ANN@example.com")
    assert load_subscribers(db=panel.db)[0].enabled is False
    post(panel, "/notifications/subscribers/delete", email="ann@example.com")
    assert load_subscribers(db=panel.db) == []
    assert [a["actor"] for a in _audit(panel.db, "subscriber")] == ["admin"] * 3
    assert "err=" in post(panel, "/notifications/subscribers/toggle",
                          email="nobody@example.com").headers["location"]


def test_notification_settings_and_test_alert(panel, monkeypatch):
    sent = []
    monkeypatch.setattr("capex.notify.ops.send_email", lambda **kw: sent.append(kw))
    post(panel, "/notifications/settings", alerts_enabled="on",
         operator_emails="a@example.com, b@example.com", max_age="30")  # filing emails off
    assert settings.get("alerts.operator_emails", panel.db) == ["a@example.com",
                                                               "b@example.com"]
    assert settings.get("notify.enabled", panel.db) is False
    assert settings.get("notify.max_age_days", panel.db) == 30
    response = post(panel, "/notifications/test", kind="alert")
    assert "msg=" in response.headers["location"] and len(sent) == 2
    assert "err=" in post(panel, "/notifications/test", kind="sample",
                          email="x@example.com").headers["location"]    # no filings yet


# ---- calendar and filings -------------------------------------------------------------------

def _insert_event(db, status="failed", calendar_id=None, accession="0000000000-26-000001"):
    with db.mutating() as conn:
        return conn.execute(
            "INSERT INTO filing_events (ticker, form_type, accession_number, filing_date, "
            "period_of_report, status, attempts, calendar_id, discovered_by, discovered_at, "
            "updated_at) VALUES ('MSFT', '10-Q', ?, '2026-10-28', '2026-09-30', ?, 6, ?, "
            "'calendar', 'x', 'x')", (accession, status, calendar_id)).lastrowid


def _calendar(db, ticker, fde):
    with db.connect() as conn:
        return dict(conn.execute("SELECT * FROM fiscal_calendar WHERE ticker = ? AND "
                                 "fiscal_date_ending = ?", (ticker, fde)).fetchone())


def test_calendar_rows(panel):
    post(panel, "/calendar/add", ticker="msft", report_date="2026-10-28",
         fiscal_date_ending="2026-09-30", form_type="")
    row = _calendar(panel.db, "MSFT", "2026-09-30")
    assert (row["form_type"], row["source"], row["status"]) == ("10-Q", "manual", "upcoming")
    post(panel, f"/calendar/{row['id']}/skip", reason="date moved")
    assert _calendar(panel.db, "MSFT", "2026-09-30")["status"] == "skipped"
    post(panel, f"/calendar/{row['id']}/retry")
    assert _calendar(panel.db, "MSFT", "2026-09-30")["status"] == "upcoming"
    assert "err=" in post(panel, "/calendar/add", ticker="NOPE", report_date="2026-10-28",
                          fiscal_date_ending="2026-09-30").headers["location"]
    assert len(_audit(panel.db, "calendar")) == 3


def test_filing_retry_and_ignore(panel):
    post(panel, "/calendar/add", ticker="MSFT", report_date="2026-10-28",
         fiscal_date_ending="2026-09-30", form_type="10-Q")
    cal_id = _calendar(panel.db, "MSFT", "2026-09-30")["id"]
    event_id = _insert_event(panel.db, calendar_id=cal_id)
    post(panel, f"/filings/{event_id}/retry")
    with panel.db.connect() as conn:
        event = dict(conn.execute("SELECT * FROM filing_events WHERE id = ?",
                                  (event_id,)).fetchone())
    assert (event["status"], event["attempts"]) == ("discovered", 0)
    assert _calendar(panel.db, "MSFT", "2026-09-30")["status"] == "detected"
    assert ("watcher", {"event_ids": [event_id]}, "admin") in _requests(panel.db)
    post(panel, f"/filings/{event_id}/ignore", reason="wrong filing")
    assert _calendar(panel.db, "MSFT", "2026-09-30")["status"] == "upcoming"
    assert [a["entity_key"] for a in _audit(panel.db, "filing")] == [
        "0000000000-26-000001"] * 2


def test_ingest_an_accession(panel, monkeypatch):
    submissions = {"filings": {"recent": {
        "accessionNumber": ["0000950170-26-000099", "0000950170-26-000011"],
        "filingDate": ["2026-10-29", "2026-07-30"],
        "reportDate": ["2026-09-30", "2026-06-30"],
        "primaryDocument": ["msft-q1.htm", "msft-10k.htm"],
        "form": ["10-Q", "10-K"],
    }}}
    monkeypatch.setattr("capex.monitor.pipeline.submissions_for",
                        lambda ticker, db, cache: submissions)
    response = post(panel, "/filings/ingest", ticker="MSFT", accession="0000950170-26-000099")
    assert "msg=" in response.headers["location"]
    with panel.db.connect() as conn:
        event = dict(conn.execute("SELECT * FROM filing_events").fetchone())
    assert (event["form_type"], event["period_of_report"], event["discovered_by"]) == (
        "10-Q", "2026-09-30", "manual")
    assert ("watcher", {"event_ids": [event["id"]]}, "admin") in _requests(panel.db)
    for bad in ("not-an-accession", "0000950170-26-999999"):
        assert "err=" in post(panel, "/filings/ingest", ticker="MSFT",
                              accession=bad).headers["location"]


# ---- runs --------------------------------------------------------------------------------

def test_run_detail_shows_the_log(panel):
    with panel.db.ops_write() as conn:
        run_id = schedules.open_run(conn, "health", "manual", schedules.utc_now())
    log = paths.logs_dir() / "runs" / f"{run_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("checking disk\nall good\n")
    schedules.finish_run(panel.db, run_id, status="success", exit_code=0, log_path=str(log))
    schedules.set_run_summary(panel.db, run_id, {"checks": {"disk": ["ok", "fine"]}})
    page = panel.client.get(f"/runs/{run_id}")
    assert page.status_code == 200 and "all good" in page.text and "disk" in page.text
    assert panel.client.get("/runs/999999").status_code == 404
    listing = panel.client.get("/runs?job=health&status=success")
    assert f"/runs/{run_id}" in listing.text
