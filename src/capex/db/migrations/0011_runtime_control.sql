-- Migration 0011: runtime control for the always-on server.
--
-- The admin panel edits these tables and the scheduler re-reads them on
-- every tick, so behaviour changes without a deploy. Additive only: older
-- code ignores the new tables, which keeps a code rollback safe.
-- Secrets never go here (they live in SSM Parameter Store).

-- Typed settings (registry and defaults: src/capex/settings.py).
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL
);

-- Who changed what, for settings, watchlist, schedules and subscribers.
CREATE TABLE IF NOT EXISTS settings_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    entity      TEXT NOT NULL,          -- 'setting' | 'watchlist' | 'schedule' | 'subscriber'
    entity_key  TEXT NOT NULL,
    old_json    TEXT,
    new_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_settings_audit_entity ON settings_audit(entity, entity_key);

-- Which companies the watcher follows, and which forms to expect.
CREATE TABLE IF NOT EXISTS watchlist (
    ticker          TEXT PRIMARY KEY REFERENCES companies(ticker),
    watch           INTEGER NOT NULL DEFAULT 1 CHECK(watch IN (0, 1)),
    quarterly_form  TEXT CHECK(quarterly_form IN ('10-Q', '6-K', 'HK-IR')),
    annual_form     TEXT CHECK(annual_form IN ('10-K', '20-F', 'HK-AR')),
    notes           TEXT,
    updated_at      TEXT NOT NULL
);

-- When each job runs (5-field cron in `tz`).
CREATE TABLE IF NOT EXISTS job_schedules (
    job          TEXT PRIMARY KEY,
    cron         TEXT NOT NULL,
    tz           TEXT NOT NULL DEFAULT 'Europe/London',
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    timeout_s    INTEGER NOT NULL DEFAULT 3600,
    params_json  TEXT NOT NULL DEFAULT '{}',
    next_run_at  TEXT,
    last_run_id  INTEGER,
    updated_at   TEXT NOT NULL
);

-- One row per job execution.
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job           TEXT NOT NULL,
    trigger_kind  TEXT NOT NULL CHECK(trigger_kind IN ('schedule', 'manual', 'startup', 'cli')),
    status        TEXT NOT NULL CHECK(status IN (
                      'running', 'success', 'partial', 'failed', 'deferred',
                      'skipped', 'timeout', 'cancelled')),
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    exit_code     INTEGER,
    summary_json  TEXT,
    log_path      TEXT,
    log_tail      TEXT,
    code_sha      TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_job_started ON runs(job, started_at);

-- "Run now" requests from the admin panel or CLI, claimed by the scheduler.
CREATE TABLE IF NOT EXISTS job_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job           TEXT NOT NULL,
    params_json   TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'queued'
                      CHECK(status IN ('queued', 'running', 'done', 'cancelled')),
    requested_by  TEXT NOT NULL,
    requested_at  TEXT NOT NULL,
    run_id        INTEGER REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_job_requests_status ON job_requests(status, requested_at);

-- Email subscribers (private: never in git).
CREATE TABLE IF NOT EXISTS subscribers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    tickers_json  TEXT NOT NULL DEFAULT '["*"]',
    metrics_json  TEXT NOT NULL DEFAULT '["*"]',
    enabled       INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- De-duplication for operator alerts.
CREATE TABLE IF NOT EXISTS alerts_sent (
    key           TEXT PRIMARY KEY,
    last_sent_at  TEXT NOT NULL,
    count         INTEGER NOT NULL DEFAULT 1
);

-- One row per LLM call: budget enforcement and usage visibility.
CREATE TABLE IF NOT EXISTS llm_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    run_id         INTEGER,
    backend        TEXT NOT NULL,
    model          TEXT,
    prompt_chars   INTEGER NOT NULL,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    duration_ms    INTEGER,
    ok             INTEGER NOT NULL CHECK(ok IN (0, 1)),
    error_kind     TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts);
