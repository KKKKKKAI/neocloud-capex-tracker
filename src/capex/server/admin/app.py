"""Routes, guards and page data for the admin panel (see the package doc)."""
from __future__ import annotations

import hmac
import json
import os
import secrets
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from ... import settings
from ...db import Database
from .. import schedules

ACTOR = "admin"
DEFAULT_PORT = int(os.environ.get("CAPEX_ADMIN_PORT", "8081"))
LOG_VIEW_BYTES = 200_000
DISPLAY_TZ = ZoneInfo(os.environ.get("CAPEX_TZ", "Europe/London"))

NAV = [("/", "Overview"), ("/companies", "Companies"), ("/calendar", "Calendar"),
       ("/schedule", "Schedule"), ("/runs", "Runs"), ("/notifications", "Notifications"),
       ("/settings", "Settings"), ("/audit", "Audit")]
CRON_PRESETS = [
    ("*/20 * * * *", "every 20 minutes"), ("0 * * * *", "hourly"),
    ("15 */6 * * *", "every 6 hours"), ("0 7 * * *", "daily at 07:00"),
    ("10 6,18 * * *", "06:10 and 18:10"), ("30 4 * * 0", "Sundays at 04:30"),
]
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data:; form-action 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ---- template helpers ---------------------------------------------------------------

def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def local_time(value: str | None) -> str:
    moment = _parse(value)
    return moment.astimezone(DISPLAY_TZ).strftime("%d %b %H:%M %Z") if moment else "–"


def ago(value: str | None) -> str:
    moment = _parse(value)
    if moment is None:
        return "–"
    seconds = (datetime.now(timezone.utc) - moment).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 90:
        text = f"{seconds:.0f} s"
    elif seconds < 5400:
        text = f"{seconds / 60:.0f} min"
    elif seconds < 172_800:
        text = f"{seconds / 3600:.0f} h"
    else:
        text = f"{seconds / 86_400:.0f} days"
    return f"in {text}" if future else f"{text} ago"


def pretty_json(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def took(started: str | None, finished: str | None) -> str:
    start, end = _parse(started), _parse(finished)
    if not (start and end):
        return ""
    seconds = (end - start).total_seconds()
    return f"{seconds:.0f} s" if seconds < 120 else f"{seconds / 60:.0f} min"


TEMPLATES.env.filters.update(local=local_time, ago=ago, pretty=pretty_json)
TEMPLATES.env.globals.update(took=took)


# ---- app and guards -----------------------------------------------------------------

def create_app(db_factory: Callable[[], Database] = Database, *,
               port: int = DEFAULT_PORT) -> FastAPI:
    app = FastAPI(title="capex admin", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db_factory = db_factory
    app.state.csrf = secrets.token_urlsafe(32)
    hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    origins = {f"http://{h}" for h in hosts}

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable) -> Response:
        if request.headers.get("host", "") not in hosts:
            return PlainTextResponse(
                f"Forbidden: open the panel through the SSH tunnel at http://localhost:{port}",
                status_code=403)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin is None and request.headers.get("referer"):
                origin = "/".join(request.headers["referer"].split("/")[:3])
            if origin not in origins:
                return PlainTextResponse("Forbidden: cross-site request", status_code=403)
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    _routes(app)
    return app


async def check_csrf(request: Request) -> None:
    form = await request.form()
    if not hmac.compare_digest(str(form.get("csrf", "")), request.app.state.csrf):
        raise HTTPException(403, "This form is stale (the panel restarted): reload the page.")


def get_db(request: Request) -> Database:
    return request.app.state.db_factory()


DB = Annotated[Database, Depends(get_db)]


def render(request: Request, db: Database, template: str, **context: Any) -> Response:
    token = request.app.state.csrf
    return TEMPLATES.TemplateResponse(request, template, {
        "nav": NAV, "path": request.url.path,
        "csrf_field": Markup(f'<input type="hidden" name="csrf" value="{token}">'),
        "msg": request.query_params.get("msg"), "err": request.query_params.get("err"),
        "public_url": settings.get("publish.public_base_url", db),
        **context,
    })


def back(path: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    if not path.startswith("/") or path.startswith("//"):
        path = "/"
    query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(path + (f"?{query}" if query else ""), status_code=303)


def _queue(db: Database, job: str, params: dict[str, Any] | None = None) -> str:
    request_id, created = schedules.queue_request(db, job, params=params, requested_by=ACTOR)
    if created:
        return f"Queued {job} (request {request_id}); the scheduler starts it within a minute."
    return f"{job} is already queued (request {request_id})."


# ---- page data --------------------------------------------------------------------------

def _rows(db: Database, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def _heartbeat() -> str | None:
    from ..scheduler import heartbeat_path

    path = heartbeat_path()
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _latest_run(db: Database, job: str) -> dict[str, Any] | None:
    rows = _rows(db, "SELECT * FROM runs WHERE job = ? ORDER BY id DESC LIMIT 1", (job,))
    if not rows:
        return None
    run = rows[0]
    run["summary"] = json.loads(run["summary_json"]) if run["summary_json"] else {}
    return run


def _overview(db: Database) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    token_date = settings.get("llm.token_created_at", db)
    token_days = (now.date() - date.fromisoformat(token_date)).days if token_date else None
    backlog = {r["status"]: r["n"] for r in _rows(
        db, "SELECT status, COUNT(*) AS n FROM filing_events WHERE status IN "
            "('discovered', 'fetched', 'partial', 'failed') GROUP BY status")}
    due = _rows(db, "SELECT COUNT(*) AS n FROM fiscal_calendar WHERE status = 'upcoming' "
                    "AND report_date <= ?", (now.date().isoformat(),))[0]["n"]
    calls = _rows(db, "SELECT COUNT(*) AS n, SUM(ok = 0) AS failed FROM llm_calls "
                      "WHERE ts >= ?", (now.strftime("%Y-%m-%dT00:00:00"),))[0]
    upcoming = sorted((s for s in schedules.get_schedules(db) if s["enabled"]
                       and s["next_run_at"]), key=lambda s: s["next_run_at"])[:6]
    return {
        "heartbeat": _heartbeat(),
        "scheduler_paused": settings.get("scheduler.paused", db),
        "llm_paused_until": settings.get("llm.paused_until", db),
        "token_date": token_date, "token_days": token_days,
        "backlog": backlog, "due": due,
        "calls": calls["n"], "calls_failed": calls["failed"] or 0,
        "budget": settings.get("llm.max_calls_per_day", db),
        "upcoming": upcoming,
        "recent": _rows(db, "SELECT * FROM runs ORDER BY id DESC LIMIT 10"),
        "health": _latest_run(db, "health"),
        "publish": _latest_run(db, "publish"),
        "queued": _rows(db, "SELECT * FROM job_requests WHERE status IN ('queued', 'running') "
                            "ORDER BY id"),
    }


def _companies(db: Database) -> list[dict[str, Any]]:
    return _rows(db, """
        SELECT w.*, c.name, c.preferred_source, c.fiscal_year_end_month AS fye,
          (SELECT MAX(period_of_report) FROM source_documents s
            WHERE s.ticker = w.ticker AND s.raw_path NOT LIKE '%://%') AS last_period,
          (SELECT report_date || ' · ' || status FROM fiscal_calendar f
            WHERE f.ticker = w.ticker AND f.status NOT IN ('extracted', 'skipped')
            ORDER BY report_date DESC LIMIT 1) AS next_row
        FROM watchlist w JOIN companies c ON c.ticker = w.ticker ORDER BY w.ticker
    """)


# ---- routes -------------------------------------------------------------------------------

def _csv(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()] or ["*"]


def _routes(app: FastAPI) -> None:
    from ...monitor import calendar as cal
    from ...monitor import pipeline
    from ...monitor.watchlist import ANNUAL_FORMS, QUARTERLY_FORMS, update_entry
    from ...notify import subscribers as subs

    guarded = [Depends(check_csrf)]

    # -- overview + generic actions
    @app.get("/")
    def overview(db: DB, request: Request) -> Response:
        return render(request, db, "overview.html", title="Overview", o=_overview(db),
                      jobs=list(schedules.JOBS))

    @app.post("/jobs/{job}/run", dependencies=guarded)
    def run_job(db: DB, job: str, next: str = Form("/"), params: str = Form("{}")) -> Response:
        try:
            decoded = json.loads(params or "{}")
            if not isinstance(decoded, dict):
                raise ValueError
            return back(next, msg=_queue(db, job, decoded))
        except (ValueError, schedules.ScheduleError) as e:
            return back(next, err=str(e) or "params must be a JSON object")

    @app.post("/scheduler/{action}", dependencies=guarded)
    def pause_scheduler(db: DB, action: str, next: str = Form("/")) -> Response:
        if action not in ("pause", "resume"):
            raise HTTPException(404)
        settings.set("scheduler.paused", action == "pause", db=db, actor=ACTOR)
        return back(next, msg="Schedules paused (Run now still works)." if action == "pause"
                    else "Schedules resumed.")

    # -- companies
    @app.get("/companies")
    def companies(db: DB, request: Request) -> Response:
        return render(request, db, "companies.html", title="Companies", rows=_companies(db),
                      quarterly_forms=QUARTERLY_FORMS, annual_forms=ANNUAL_FORMS)

    @app.post("/companies/{ticker}", dependencies=guarded)
    def save_company(db: DB, ticker: str, watch: str | None = Form(None),
                     quarterly_form: str = Form(""), annual_form: str = Form(""),
                     notes: str = Form("")) -> Response:
        try:
            update_entry(ticker, watch=watch == "on", quarterly_form=quarterly_form or None,
                         annual_form=annual_form or None, notes=notes, db=db, actor=ACTOR)
        except (LookupError, ValueError) as e:
            return back("/companies", err=str(e))
        return back("/companies", msg=f"{ticker} saved.")

    @app.post("/companies/{ticker}/check", dependencies=guarded)
    def check_company(db: DB, ticker: str) -> Response:
        return back("/companies", msg=_queue(db, "watcher", {"tickers": [ticker]}))

    @app.post("/companies/{ticker}/fetch", dependencies=guarded)
    def fetch_company(db: DB, ticker: str, form: str = Form(...)) -> Response:
        return back("/companies", msg=_queue(db, "watcher", {"ticker": ticker, "form": form}))

    # -- calendar and filings
    @app.get("/calendar")
    def calendar_page(db: DB, request: Request) -> Response:
        today = date.today()
        rows = _rows(db, "SELECT * FROM fiscal_calendar WHERE report_date BETWEEN ? AND ? "
                         "ORDER BY report_date DESC, ticker",
                     ((today - timedelta(days=150)).isoformat(),
                      (today + timedelta(days=100)).isoformat()))
        events = _rows(db, "SELECT * FROM filing_events WHERE status != 'ignored' "
                           "OR updated_at >= ? ORDER BY id DESC LIMIT 60",
                       ((today - timedelta(days=30)).isoformat(),))
        return render(request, db, "calendar.html", title="Calendar", rows=rows,
                      events=events, forms=cal.CALENDAR_FORMS, retryable=cal.RETRYABLE,
                      tickers=[c["ticker"] for c in _companies(db)])

    @app.post("/calendar/add", dependencies=guarded)
    def calendar_add(db: DB, ticker: str = Form(...), report_date: str = Form(...),
                     fiscal_date_ending: str = Form(...), form_type: str = Form("")) -> Response:
        try:
            cal.save_manual_entry(ticker, report_date, fiscal_date_ending, form_type or None,
                                  db=db, actor=ACTOR)
        except (LookupError, ValueError) as e:
            return back("/calendar", err=str(e))
        return back("/calendar", msg=f"{ticker.upper()} {fiscal_date_ending} saved and queued.")

    @app.post("/calendar/{row_id}/{action}", dependencies=guarded)
    def calendar_action(db: DB, row_id: int, action: str, reason: str = Form("")) -> Response:
        try:
            if action == "retry":
                cal.retry_row(row_id, db=db, actor=ACTOR)
            elif action == "skip":
                cal.skip_row(row_id, db=db, actor=ACTOR, reason=reason)
            else:
                raise HTTPException(404)
        except (LookupError, ValueError) as e:
            return back("/calendar", err=str(e))
        return back("/calendar", msg=f"Calendar row {row_id}: {action} done.")

    @app.post("/filings/ingest", dependencies=guarded)
    def filings_ingest(db: DB, ticker: str = Form(...), accession: str = Form(...)) -> Response:
        try:
            event_id = pipeline.ingest_accession(ticker, accession, db=db, actor=ACTOR)
        except (LookupError, ValueError) as e:
            return back("/calendar", err=str(e))
        except Exception as e:  # SEC unreachable and similar
            return back("/calendar", err=f"{type(e).__name__}: {e}")
        return back("/calendar", msg=f"Filing queued as event {event_id}. "
                    + _queue(db, "watcher", {"event_ids": [event_id]}))

    @app.post("/filings/{event_id}/{action}", dependencies=guarded)
    def filing_action(db: DB, event_id: int, action: str, reason: str = Form("")) -> Response:
        try:
            if action == "retry":
                pipeline.retry_event(event_id, db=db, actor=ACTOR)
                note = " " + _queue(db, "watcher", {"event_ids": [event_id]})
            elif action == "ignore":
                pipeline.ignore_event(event_id, db=db, actor=ACTOR, reason=reason)
                note = ""
            else:
                raise HTTPException(404)
        except LookupError as e:
            return back("/calendar", err=str(e))
        return back("/calendar", msg=f"Filing event {event_id}: {action} done.{note}")

    # -- schedules
    @app.get("/schedule")
    def schedule_page(db: DB, request: Request) -> Response:
        rows = schedules.get_schedules(db)
        for row in rows:
            spec = schedules.JOBS.get(row["job"])
            row["help"] = spec.help if spec else "(no longer a job)"
        return render(request, db, "schedule.html", title="Schedule", rows=rows,
                      presets=CRON_PRESETS, paused=settings.get("scheduler.paused", db))

    @app.post("/schedule/{job}", dependencies=guarded)
    def schedule_save(db: DB, job: str, cron: str = Form(""), preset: str = Form(""),
                      enabled: str | None = Form(None), timeout_s: str = Form("")) -> Response:
        try:
            timeout = int(timeout_s) if timeout_s.strip() else None
            row = schedules.update_schedule(db, job, cron=(preset or cron.strip() or None),
                                            enabled=enabled == "on", timeout_s=timeout,
                                            actor=ACTOR)
        except (schedules.ScheduleError, ValueError) as e:
            return back("/schedule", err=f"{job}: {e}")
        state = "enabled" if row["enabled"] else "disabled"
        return back("/schedule", msg=f"{job}: {row['cron']} ({state}), next run "
                    f"{local_time(row['next_run_at'])}.")

    # -- runs
    @app.get("/runs")
    def runs_page(db: DB, request: Request, job: str = "", status: str = "") -> Response:
        where, params = [], []
        if job:
            where.append("job = ?")
            params.append(job)
        if status:
            where.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM runs" + (" WHERE " + " AND ".join(where) if where else "")
        rows = _rows(db, sql + " ORDER BY id DESC LIMIT 150", params)
        return render(request, db, "runs.html", title="Runs", rows=rows, job=job,
                      status=status, jobs=list(schedules.JOBS))

    @app.get("/runs/{run_id}")
    def run_page(db: DB, run_id: int, request: Request) -> Response:
        rows = _rows(db, "SELECT * FROM runs WHERE id = ?", (run_id,))
        if not rows:
            raise HTTPException(404, "no such run")
        run = rows[0]
        log = run["log_tail"] or ""
        if run["log_path"] and Path(run["log_path"]).exists():
            with open(run["log_path"], "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - LOG_VIEW_BYTES))
                log = f.read().decode("utf-8", "replace")
        return render(request, db, "run.html", title=f"Run {run_id}", run=run, log=log)

    # -- notifications
    @app.get("/notifications")
    def notifications_page(db: DB, request: Request) -> Response:
        return render(
            request, db, "notifications.html", title="Notifications",
            subscribers=subs.load_subscribers(db=db),
            alerts_enabled=settings.get("alerts.enabled", db),
            operator_emails=", ".join(settings.get("alerts.operator_emails", db)),
            notify_enabled=settings.get("notify.enabled", db),
            max_age=settings.get("notify.max_age_days", db),
            sent=_rows(db, "SELECT * FROM alerts_sent ORDER BY last_sent_at DESC LIMIT 20"))

    @app.post("/notifications/subscribers", dependencies=guarded)
    def subscriber_save(db: DB, email: str = Form(...), tickers: str = Form("*"),
                        metrics: str = Form("*")) -> Response:
        try:
            sub = subs.add_subscriber(email, tickers=[t.upper() for t in _csv(tickers)],
                                      metrics=_csv(metrics), db=db, actor=ACTOR)
        except ValueError as e:
            return back("/notifications", err=str(e))
        return back("/notifications", msg=f"{sub.email} saved.")

    @app.post("/notifications/subscribers/{action}", dependencies=guarded)
    def subscriber_action(db: DB, action: str, email: str = Form(...)) -> Response:
        current = {s.email.lower(): s for s in subs.load_subscribers(db=db)}
        sub = current.get(email.strip().lower())
        if sub is None:
            return back("/notifications", err=f"{email} is not a subscriber")
        if action == "toggle":
            subs.set_enabled(sub.email, not sub.enabled, db=db, actor=ACTOR)
            return back("/notifications", msg=f"{sub.email} "
                        f"{'disabled' if sub.enabled else 'enabled'}.")
        if action == "delete":
            subs.remove_subscriber(sub.email, db=db, actor=ACTOR)
            return back("/notifications", msg=f"{sub.email} removed.")
        raise HTTPException(404)

    @app.post("/notifications/settings", dependencies=guarded)
    def notification_settings(db: DB, alerts_enabled: str | None = Form(None),
                              operator_emails: str = Form(""),
                              notify_enabled: str | None = Form(None),
                              max_age: str = Form("14")) -> Response:
        try:
            for key, value in (("alerts.enabled", alerts_enabled == "on"),
                               ("alerts.operator_emails", operator_emails),
                               ("notify.enabled", notify_enabled == "on"),
                               ("notify.max_age_days", max_age)):
                if settings.coerce(key, value) != settings.get(key, db):
                    settings.set(key, value, db=db, actor=ACTOR)
        except settings.SettingError as e:
            return back("/notifications", err=str(e))
        return back("/notifications", msg="Notification settings saved.")

    @app.post("/notifications/test", dependencies=guarded)
    def notification_test(db: DB, kind: str = Form(...), email: str = Form("")) -> Response:
        if kind == "alert":
            from ...notify.ops import alert
            stamp = schedules.utc_iso(schedules.utc_now())
            sent = alert(f"test:{stamp}", "test alert from the admin panel",
                         "If you can read this, operator alerts work.", db=db,
                         min_interval=timedelta(0))
            return back("/notifications", msg="Test alert sent to the operator emails."
                        if sent else None,
                        err=None if sent else "Not sent: check alerts.enabled, the operator "
                        "emails and the Gmail credentials (see the Runs log / health).")
        return back("/notifications", **_send_sample(db, email))

    # -- settings
    @app.get("/settings")
    def settings_page(db: DB, request: Request) -> Response:
        groups: dict[str, list] = {}
        for setting, value, is_default in settings.all_settings(db):
            shown = value if setting.type is str else json.dumps(value)
            groups.setdefault(setting.key.split(".")[0], []).append(
                {"key": setting.key, "help": setting.help, "value": shown,
                 "is_default": is_default, "type": setting.type.__name__})
        return render(request, db, "settings.html", title="Settings", groups=groups,
                      paused_until=settings.get("llm.paused_until", db))

    @app.post("/settings/{key}", dependencies=guarded)
    def setting_save(db: DB, key: str, value: str = Form(""),
                     reset: str | None = Form(None)) -> Response:
        try:
            if reset:
                settings.reset(key, db=db, actor=ACTOR)
                return back("/settings", msg=f"{key} reset to its default.")
            settings.set(key, value, db=db, actor=ACTOR)
        except settings.SettingError as e:
            return back("/settings", err=str(e))
        return back("/settings", msg=f"{key} saved.")

    @app.post("/llm/pause", dependencies=guarded)
    def llm_pause(db: DB, hours: str = Form("6")) -> Response:
        try:
            until = schedules.utc_now() + timedelta(hours=float(hours))
        except ValueError:
            return back("/settings", err="hours must be a number")
        settings.set("llm.paused_until", schedules.utc_iso(until), db=db, actor=ACTOR)
        return back("/settings", msg=f"LLM calls paused until {local_time(until.isoformat())}.")

    @app.post("/llm/resume", dependencies=guarded)
    def llm_resume(db: DB) -> Response:
        settings.set("llm.paused_until", "", db=db, actor=ACTOR)
        return back("/settings", msg="LLM calls resumed.")

    # -- audit
    @app.get("/audit")
    def audit_page(db: DB, request: Request, entity: str = "") -> Response:
        sql = "SELECT * FROM settings_audit" + (" WHERE entity = ?" if entity else "")
        rows = _rows(db, sql + " ORDER BY id DESC LIMIT 300", (entity,) if entity else ())
        return render(request, db, "audit.html", title="Audit", rows=rows, entity=entity,
                      entities=["setting", "watchlist", "schedule", "subscriber", "calendar",
                                "filing"])


def _send_sample(db: Database, email: str) -> dict[str, str]:
    """One sample filing email to `email` (the subscriber list is untouched)."""
    from ...notify import notify_subscribers
    from ...notify.subscribers import Subscriber

    if "@" not in email:
        return {"err": "Enter the address to send the sample to."}
    rows = _rows(db, """
        SELECT sd.id, sd.ticker, sd.period_of_report, sd.filing_date FROM source_documents sd
        WHERE sd.raw_path NOT LIKE '%://%' AND EXISTS (SELECT 1 FROM extractions e
          WHERE e.source_document_id = sd.id AND e.period_type IN ('FY','Q1','Q2','Q3','Q4'))
        ORDER BY sd.filing_date DESC, sd.id DESC LIMIT 1""")
    if not rows:
        return {"err": "No extracted filing to build a sample from yet."}
    row = rows[0]
    summary = notify_subscribers(
        [{"status": "success", "ticker": row["ticker"], "period": row["period_of_report"],
          "filed": row["filing_date"], "source_document_id": row["id"]}],
        db=db, subscribers=[Subscriber(email=email.strip())])
    if summary["sent"]:
        return {"msg": f"Sample email ({row['ticker']} {row['period_of_report']}) sent to "
                       f"{email.strip()}."}
    errors = "; ".join(str(e.get("error")) for e in summary["errors"]) or "nothing to send"
    return {"err": f"Not sent: {errors}"}


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="capex server admin")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    uvicorn.run(create_app(port=args.port), host="127.0.0.1", port=args.port,
                proxy_headers=False, server_header=False, log_level="info")
    return 0
