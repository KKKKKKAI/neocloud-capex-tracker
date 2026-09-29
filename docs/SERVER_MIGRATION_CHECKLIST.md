# Server migration checklist

Execution log for moving the tracker from a laptop-only WSL setup to an
always-on AWS server. One line per step; tick it with the date and the
PR or commit that did it.

**Target:** EC2 (Ubuntu 24.04, `eu-north-1`) runs the scheduler and
pipeline. S3 + CloudFront serve the public dashboard and workbook
downloads. The admin panel is reachable only over an SSH tunnel. Secrets
live in SSM Parameter Store under `/capex/`. GitHub holds code only;
the server deploys commits whose `lint-and-test` CI run passed.

**Pauses for the maintainer:**
- (a) secrets and accounts;
- (b) anything that creates or increases AWS cost;
- (c) the go-live commit that stops tracking data;
- (d) spot-checks of newly extracted numbers;
- (e) anything destructive.

## Phase 0: Day-0 setup and baseline

### 0A. Maintainer setup
- [x] AWS account; CLI v2 in WSL; default region `eu-north-1` — 2026-09-28
- [x] SSH key `~/.ssh/capex_ed25519` in WSL — 2026-09-28
- [x] `gh` logged in from WSL — 2026-09-28
- [x] Operator and subscriber email agreed (same address, all companies) — 2026-09-28
- [x] `/capex/CLAUDE_CODE_OAUTH_TOKEN` and `/capex/ALPHA_VANTAGE_API_KEY` exist in `eu-north-1` — 2026-09-28
- [x] Re-save both as **SecureString** — 2026-09-28 (see Phase 2)
- [ ] Rotate the Claude token (`claude setup-token`) and re-save `/capex/CLAUDE_CODE_OAUTH_TOKEN`
- [x] Create `/capex/GMAIL_APP_PASSWORD` (SecureString) and `/capex/GMAIL_USERNAME` (String) in `eu-north-1` — 2026-09-28 (see Phase 2). The unused top-level `GMAIL_*` parameters can be deleted.
- [ ] Create a non-root admin identity for the CLI (the CLI currently uses the root user).

### 0B. Baseline (2026-09-28)
- [x] 0.1 No crontab, no monitor process. WSL git clean at `279bc70`; Windows Git shows 41 `D` + 41 `??` workbook entries.
- [x] 0.2 DB snapshot `data/db/capex.db.pre-server.bak`: integrity ok, schema v10, 3,336 extractions, calendar 14 extracted / 4 failed / 5 upcoming.
- [x] 0.3 uv plus a Python 3.12 venv at `~/.venvs/capex`.
- [x] 0.4 ruff: 1 error (E501, `cli/main.py:1010`). pytest: 191 passed. Found that the test suite overwrote the tracked `data/db/dump.sql`; fixed in PR 1.
- [x] 0.5 This checklist.

## Phase 1: Windows/WSL fixes and green CI (PR 1)
- [x] 1.1 Workbook names `[YYYY.MM.DD - HHhMM] financials sourcebook.xlsx` in `CAPEX_TZ`, plus `parse_workbook_name()` / `latest_workbook()`
- [x] 1.2 CLI help line wrapped (the E501)
- [x] 1.3 `scripts/migrate_workbook_names.py`; 41 workbooks renamed with `git mv`; README link updated
- [x] 1.4 `.gitattributes` pins LF; renormalised (no content changes)
- [x] 1.5 `scripts/*.sh` executable in git
- [x] 1.6 Tests: naming, repo hygiene, dump path, chart HTML on a fixture DB
- [x] 1.7 `charts` extra; `tzdata` on Windows
- [x] 1.8 CI: Python 3.12, all extras, lint `src tests scripts`, read-only token, job id `lint-and-test`
- [x] 1.9 Removed `organize-sources.yml`, `watcher.yml`, `install_cron.sh`; untracked `.claude/settings.local.json`
- [x] 1.10 CLAUDE.md: Rule 0 and a Windows/WSL environment section
- [x] PR merged with CI green — 2026-09-28, #2 (`908c15a`), first green CI since 2026-04-21

## Phase 2: AWS foundation and early smoke test (PR 3) — pause (b)
- [x] 2.1 `deploy/aws/capex-stack.yaml`: site and backup buckets, CloudFront OAC, least-privilege role, key pair, SSH-only security group, EC2 plus a persistent data volume and Elastic IP. cfn-lint clean; AWS `validate-template` OK.
- [x] 2.2 `deploy/bootstrap.sh` (idempotent; pins uv 0.12.19 and Claude Code 2.1.232)
- [x] 2.3 `capex-secrets.service` + `capex.server.secrets` (fetch / check); `capex.server.doctor` for the smoke test and later health checks
- [x] 2.4 `deploy/aws/deploy_stack.sh`, `deploy/aws/allow_my_ip.sh`, `scripts/ssh_config.example`, `deploy/README.md`
- [x] `secrets check` against the real account (names and types only): two `String` params, the Gmail pair missing
- [x] PR merged — 2026-09-28, #3 (`ecab9b2`)
- [x] Parameters fixed: Claude token + Alpha Vantage key re-saved as SecureString — 2026-09-28
- [x] 2.5 Stack `capex` deployed in `eu-north-1` (maintainer approved cost) — 2026-09-28
- [x] 2.5 Smoke test on the host: 8/9 PASS
  - claude answered via the SSM token in ~1 s
  - SEC HTTP 200 from the AWS IP
  - Alpha Vantage returned 4,597 upcoming rows
  - S3 → CloudFront round trip OK; placeholder page live
  - RAM 909 MiB + 2 GiB swap
  - data volume mounted, 14.8 GiB free
- [x] Gmail: parameters re-created under `/capex/`; after `capex-secrets` restarted, the gmail check logs in to SMTP as the operator address. Smoke test is now **9/9** — 2026-09-28
- [x] The SSH key is passphrase-protected, so agent-side host commands go through SSM Run Command (`deploy/aws/run_on_host.py`)

## Phase 3: Code/data split, central paths, DB concurrency (PR 4)
- [x] 3.1 `capex/paths.py`: code/config under the checkout, runtime data under `$CAPEX_HOME`; `raw_path_key()` / `resolve_raw_path()` (303 file rows + 167 virtual rows need no rewrite)
- [x] 3.2 Every `parents[3]` data path and direct DB path routed through `paths`: fetchers, extractors, exporters, audit, CLI, monitor, subscribers, scripts
- [x] 3.3 Generated HTML goes to `site/` (dashboard thumbnails in `site/charts/`); `docs/` is frozen until go-live. `workbooks.html` moves to Phase 7 (publish).
- [x] 3.4 `Database`: 30 s busy timeout, `CAPEX_DB_JOURNAL_MODE` (WAL on the server), `connect_ro()`. Dumps stay on in a plain checkout and are off under `CAPEX_HOME` (`CAPEX_DUMP_SQL` overrides).
- [x] 3.5 `audit/fixes.py` inherits the environment and reports failures instead of swallowing them
- [x] Verified: outputs regenerated under a scratch `CAPEX_HOME` match the committed `docs/*.html` (6/7 identical; `calendar.html` differs only in date-relative text); `capex extract MSFT` resolves raw files there; 250 tests pass
- [x] PR merged — 2026-09-28, #4 (`9229c2a`)

## Phase 4: LLM backend hardening and runtime-control schema (PR 5)
- [x] 4.1 Migration 0011 (additive): settings, settings_audit, watchlist, job_schedules, job_requests, runs, subscribers, alerts_sent, llm_calls
- [x] 4.2 `capex/settings.py`: typed registry with validation, env fallbacks for stack values, and audited set/reset. No free-form keys, so secrets can't be stored.
- [x] 4.3 `adapters/errors.py`: `LLMAuthError`, `LLMUsageLimitError` (with reset time), `LLMBudgetError`, `LLMConfigError` (all fatal), `LLMTransientError`, `LLMOutputError`; shared classifier
- [x] 4.4 `cli_backend.py`:
  - prompt on stdin; `--output-format json`; `--tools ""`
  - empty working dir with `--setting-sources user`; API-key env vars stripped
  - daily budget, pause, and `llm_calls` telemetry
- [x] 4.5 Fatal LLM errors re-raised by the multi-metric extractor, router and watcher instead of reading as "found nothing" / "success"
- [x] 4.6 `capex llm ping|usage`, `capex settings list|help|get|set|reset`; `doctor` now pings through the production backend
- [x] Verified locally with the real CLI (2.1.232 accepted every flag): an expired login and a bogus token both give `LLMAuthError` (exit 77) and are logged in `llm_calls`. 299 tests pass.
- [x] PR merged — 2026-09-29, #5 (`afd3e51`)
- [x] Verified on the server: `capex llm ping` → `claude-opus-4-8: 'OK' in 2732 ms`, exit 0, with the SSM token, and no DB file created
- [ ] Extraction regression (same values as stored) → happens with the Phase 10 backlog run and its spot-check

## Phase 5: Watcher correctness and pipeline refactor (PR 6)
- [x] 5.1 Migration 0012: `fiscal_calendar` rebuilt with statuses partial/stale/skipped plus attempts, last_error, last/next attempt and filing_event_id; new `filing_events` (unique accession); index on `source_documents(accession_number)`
- [x] 5.2 IREN seeds → 10-K/10-Q (it left 20-F filing in FY2025); convention `three_month_column`
- [x] 5.3 `monitor/watchlist.py`: `sync_watchlist()` (never overwrites edits; 20-F filers → 6-K quarters; HKEX starts unwatched) and `expected_form()`
- [x] 5.4 `fetch/sec_http.py`: one SEC client (contact UA, gzip, ≤ 5 req/s, retries 429/5xx honouring Retry-After, clean errors). Used by the fetcher, watcher and both XBRL readers.
- [x] 5.5 `sec.list_filings()` / `fetch_accession()` (the exact matched filing, not "latest"); `dispatcher.record_source_document()` idempotent by sha256, accession or period
- [x] 5.6 Calendar sync refuses missing/placeholder keys and AV error bodies (the old workflow's silent zero-row runs). Forms come from the watchlist, one transaction per sync, and manual or in-flight rows are never overwritten.
- [x] 5.7 `watcher.poll_for_row()` → hit / not_yet / error / unsupported, matching within ±10 days and skipping amendments
- [x] 5.8 `monitor/pipeline.py`:
  - stale marking; due-row selection (watched, lookback, backoff)
  - filing events; fetch-then-extract
  - extracted / partial (retry with backoff 15 min → 12 h) / failed after `watcher.max_attempts`
  - fatal LLM errors stop the run without costing the filing an attempt (usage limit → `llm.paused_until`)
  - optional sweep for filings with no calendar row
  - outputs regenerated only when something was extracted; backlog not emailed
- [x] 5.9 `run.py` is now a thin CLI with no git push or gh issue. Exit codes 0 / 1 / 3 partial / 75 deferred / 77 auth; `--dry-run`, `--sweep`, `TICKER [FORM]`.
- [x] 5.10 `capex calendar requeue [--status] [--since] [--ticker] [--refresh-forms]`
- [x] Verified on a scratch DB copy against live EDGAR: migrated to v12, 13 watchlist rows correct, requeue fixed IREN's forms, and the dry-run catch-up found IREN 10-Q (2026-03-31), IREN 10-K (2026-06-30) and ORCL 10-Q (2026-08-31). The six 6-K rows show as unsupported until Phase 6. 350 tests pass.
- [x] PR merged — 2026-09-29, #6 (`9cc29f9`)

## Phase 6: 6-K quarterly results for NBIS, GDS, BIDU, BABA (PR 7)
- [x] 6.1 `fetch/sec_6k.py` finds the earnings release among a foreign filer's 6-Ks. EDGAR gives a 6-K no period (its reportDate is the filing date), and most 6-Ks are buyback returns, AGM notices or circulars. The search:
  - looks only at 6-Ks filed 7–120 days after the quarter end, the announced day first
  - takes the first EX-99 .htm exhibit ≥ 40 KB, judged from the filing index, so small 6-Ks are never downloaded
  - scores the text: results title, "financial results for the", condensed statements, "quarter ended"; notices are rejected by their title
  - reads the period from "three months ended <date>" phrases and accepts it within ±7 days of the calendar row
  - stores certain rejections as `filing_events` status `ignored`, never fetched again; borderline ones are looked at again
- [x] 6.2 The release plus up to 3 more EX-99 exhibits are saved as one `[filed][T][Qn][6-K].htm`; `source_url` is the release exhibit
- [x] 6.3 Watcher: 6-K rows are polled (found / waiting / already recorded → `skipped`). `capex monitor BIDU 6-K` queues the newest release, not the newest 6-K.
- [x] 6.4 Fixes:
  - a fiscal-Q4 6-K was tokenised `Q3`; now `Q4` (namer + dispatcher)
  - 6-K extraction goes straight to the LLM: companyfacts hold only 20-F facts, and the `6k_press` regex labelled RMB as USD
  - long releases send both the highlights and the financial statements
  - BABA's March quarter still comes from the 20-F (`coverage.yaml` `fiscal_q4: annual`)
- [x] 6.5 Stale rule (Phase 5 bug): a real run marked every row older than its 14–45-day window stale *before* polling it, so a requeued backlog row was dropped unseen. Now a row goes stale only after a poll on or after its deadline, or once it falls out of the lookback; `requeue` clears `last_attempt_at`.
- [x] 6.6 Pure-play cloud revenue (found in the GDS spot-check). For whole-company companies (GDS, CRWV, APLD, IREN, NBIS), cloud revenue came only from a one-off backfill script. The watcher asked the LLM for a cloud segment instead, got "not found" from GDS's release, and counted it as success, so the cloud chart would never get new quarters. `extract_filing()` now copies the filing's revenue rows as `whole-company-copy@0.1.0` rows, only for that filing, so history is untouched.
- [x] 6.7 `llm_calls.input_tokens` now counts cache writes and reads. It read 1 for every call, because the CLI caches its prompts.
- [x] Verified against live EDGAR on a scratch copy of the real DB: after `requeue --refresh-forms`, the dry-run catch-up finds all six backlog quarters (NBIS Q1, GDS Q1 + Q2, BIDU Q1 + Q2, BABA June quarter, each with score 9) plus ORCL's 10-Q. 388 tests pass; the opt-in live test finds GDS Q2.
- [x] GDS Q2 2026 run end to end on the scratch copy with the real Claude login. `capex monitor GDS 6-K` fetched `0001104659-26-095498` and extracted all six metrics on the first attempt, then regenerated the workbook, charts and site. Reconcile conflicts are unchanged at 9, all pre-existing.
  - Q2, RMB m: revenue 3,087.950, capex 1,249.766, OCF 1,416.260, D&A 851.428, PP&E 38,734.730, cloud revenue 3,087.950
  - Each value matches the release's financial statements to the thousand.
- [x] PR merged — 2026-09-29, #7 (`fd9bbef`)
- [x] Pause (d): maintainer confirmed the GDS Q2 values against the release — 2026-09-29

## Phase 7: Scheduler, jobs, publish, backups, health, alerts, subscribers (PR 8)
- [x] 7.1 `server/jobs.py`: watcher, filings_sweep, calendar_sync, regenerate_outputs, publish, backup, backup_raw (new: weekly raw sync), health, llm_check, prune.
  - Each runs as `python -m capex.server.jobs JOB --run-id N` and records its summary in `runs`.
  - Exit codes: 0 / 3 partial / 4 skipped / 75 deferred / 77 auth.
  - Jobs that change the site queue a `publish`.
- [x] 7.2 `server/scheduler.py` (`capex server scheduler [--once]`):
  - startup: lock, schema check, orphaned runs → failed, default schedules
  - every 30 s: heartbeat file, due schedules queued (croniter in the schedule's time zone; missed runs collapse), one request claimed atomically (`UPDATE … RETURNING`)
  - each job runs as a child process logging to `logs/runs/<id>.log`; on timeout SIGTERM, then SIGKILL
  - SIGTERM drains the current job. `server/locks.py` holds a `pipeline` lock that manual `capex monitor` runs share.
- [x] 7.3 Default schedules (Europe/London, editable with `capex server schedule`, later the panel): watcher `*/20`, filings_sweep 06:10 and 18:10, calendar_sync 07:00, regenerate 05:30, publish hourly at :15 and after every regenerate, backup 03:15, backup_raw Sundays 03:45, health hourly at :05, llm_check 08:00, prune Sundays 04:30.
- [x] 7.4 `server/publish.py`:
  - changed files only: MD5 against the ETag, plus a header signature
  - `max-age=300` for pages and `download/latest.xlsx`; `immutable` for `workbooks/YYYYMMDD-HHMM[-vN].xlsx`, whose Content-Disposition carries the real name
  - generated `workbooks.html`; one `/*` invalidation per change
  - refuses to delete more than half the bucket unless forced
- [x] 7.5 `server/backup.py`:
  - nightly: SQLite online backup, `integrity_check`, gzip plus `dump.sql.gz`, uploaded to `db/`, with `backup.keep_daily` local copies
  - weekly: raw filings (missing keys only) to `raw/`
  - `capex server backup|backups|restore`. Restore verifies the copy and clears stale WAL files; replacing the live DB needs the services stopped.
- [x] 7.6 `server/health.py`: heartbeat, token age (warn 330 days, critical 355), last calendar sync / publish / backup, failed jobs and filings, LLM pause and budget, disk, the claude binary, email and SEC contact configuration. `capex server health [--alert]`.
- [x] 7.7 `notify/ops.py`: operator alerts via Gmail, de-duplicated per key in `alerts_sent` (6 h default). Failed or timed-out runs alert; exit 77 sends the renew-token alert. The health job alerts per check and records findings as `partial`, so it doesn't send a second "failed" email.
- [x] 7.8 Subscribers in the DB:
  - `capex notify` add/remove/enable/disable are audited; `import-yaml` imports an old YAML file
  - YAML is used only with an explicit path or `NOTIFY_SUBSCRIBERS_PATH`
  - emails resolve the filing by `source_document_id`, link to `publish.public_base_url` (`/download/latest.xlsx`), and the footer now says "reply to unsubscribe"
- [x] 7.9 `server` extra adds croniter; dev adds `moto[s3,cloudfront]`. New settings: `backup.bucket`, `prune.keep_workbooks`, `prune.run_log_days`. The daily regenerate exports a workbook only when the data changed.
- [x] Verified:
  - 440 tests pass: scheduler loop with real child processes (exit codes, timeout kill, alerts, pause, orphans), publish and backup against moto S3, jobs, health, alerts, subscribers
  - smoke run on a scratch DB copy: `server init`, `run health --now`, and `run regenerate_outputs` through `scheduler --once`. It wrote the workbook and queued a publish, which was skipped with no bucket; a local verified backup; cron validation.
- [x] PR merged — 2026-09-29, #8 (`1a08b0b`)
- [x] On the server via SSM: code at `1a08b0b`, venv reinstalled (croniter). In a throwaway home, migrate + `server init` + `server jobs` work, and `server publish --dry-run` lists the real site bucket through the instance role: would upload index.html and workbooks.html, delete nothing. The first real publish and backup run with the real data in Phase 10.

## Phase 8: Admin panel over the SSH tunnel (PR 9)
- [x] 8.1 `server/admin/`: FastAPI + Jinja2, no JavaScript, on 127.0.0.1:8081 (`capex server admin`).
  - **Guards:**
    - the Host must be localhost:8081 or 127.0.0.1:8081 (blocks DNS rebinding)
    - every POST needs the form token and a same-origin Origin/Referer
    - `no-store`; CSP with no scripts and no framing
  - **Pages:**
    - Overview: heartbeat and pause, Claude budget and token age, backlog, publish status, health, queue, coming up, recent runs
    - Companies: watch, forms, notes, Check, Newest FORM
    - Calendar: manual dates, retry/skip rows, ingest an accession, retry/ignore filings
    - Schedule: cron with presets, on/off, timeout, Run now
    - Runs, with the log and summary
    - Notifications: subscribers, alert and filing-email settings, test email, test alert
    - Settings: every registry key, plus Claude pause/resume
    - Audit
  - The panel only edits control tables (audited as `admin`) and queues `job_requests`.
  - **New audited operations:**
    - `watchlist.update_entry`; `calendar.save_manual_entry` / `retry_row` / `skip_row`
    - `pipeline.retry_event` / `ignore_event` / `ingest_accession` (a 6-K goes through `sec_6k.release_from_filing`)
    - watcher job params `tickers`, `event_ids`, `ticker` + `form`
- [x] 8.2 `scripts/admin_tunnel.sh` (WSL) and `scripts/admin_tunnel.ps1` (Windows OpenSSH)
- [x] 8.3 A "⬇ Latest Excel" pill in every page's nav (`download/latest.xlsx`) and a Workbooks card on the dashboard (`workbooks.html`), both served by the publisher
- [x] Verified:
  - 463 tests pass, 23 of them for the panel: guards, every page, every action, audit rows
  - the panel ran on the scratch DB copy in a browser: Overview and Schedule render, and a Companies save round-trips (token, origin, form association) into the audit log
- [x] PR merged — 2026-09-29, #9 (`cf58084`)
- [ ] On the server: `capex-admin` unit (Phase 9), then the tunnel from WSL

## Phase 9: Deploy pipeline and runbooks (PR 10)
- [x] 9.1 `uv.lock` committed (73 packages). CI now installs from it (`uv lock --check`, then `uv sync --frozen --all-extras` via setup-uv), so CI tests exactly what the server runs.
- [x] 9.2 systemd units:
  - `capex-scheduler`: KillMode=mixed drain, 15 min stop timeout, Restart=always, ProtectSystem=strict
  - `capex-admin`
  - `capex-deploy.service` + `.timer` (every 10 min)
  - `capex-alert@`: OnFailure emails through `capex server alert`
  - `capex-secrets` now runs from the live release
- [x] 9.3 `capex-deploy` (`deploy/capex-deploy.sh`) with `deploy/ci_gate.py`:
  - fetch, then the CI gate (`lint-and-test` passed for that exact commit)
  - build `releases/<sha>` as `capex-deploy` (git worktree + `uv sync --frozen`), then smoke-import
  - drain the scheduler, local backup, then `db migrate` / `sync-all` / `server init` (once a DB exists)
  - install units, switch `current` atomically, restart what was running, health check
  - roll back and alert on failure; keep 3 releases
  - flags: `--status`, `--pin`, `--unpin`, `--rollback`, `--force-gate`
  - scheduler and admin are enabled only at go-live
- [x] 9.4 `scripts/dev_setup.sh` (the dev venv from the lock), `scripts/pull_server_snapshot.sh` (newest S3 DB backup into a local CAPEX_HOME; `--raw` adds the filings), and the `/usr/local/bin/capex` wrapper on the host
- [x] 9.5 `docs/SERVER_OPERATIONS.md`: everyday use, deploys, token renewal, restore, IP change, key rotation, full disk, adding a company, maintenance scripts, go-live, rebuild
- [x] 9.6 shellcheck + cfn-lint in CI (since Phase 2)
- [x] Verified locally: 473 tests pass (gate verdicts; units call real CLI commands), and `dev_setup.sh` rebuilt the venv from the lock
- [ ] On the server: bootstrap with this branch's commit pinned to get the first release; after merge, unpin to deploy main, then `--rollback` and back
- [ ] PR merged

## Phase 10: Migrate data and go live
- [ ] 10.1 First deploy + `capex server doctor`
- [ ] 10.2–10.3 Data copied and imported
- [ ] 10.4 Settings pre-filled
- [ ] 10.4b Calendar forms refreshed: `capex calendar requeue --status upcoming,failed,stale --since 2026-03-01 --refresh-forms`. This fixes IREN's rows and the foreign filers' 2026 rows, which still say `20-F`.
- [ ] 10.5 Backlog caught up — pause (d) spot-check

## Phase 11: Go-live repo cleanup (PR 10) — pause (c)
- [ ] 11.1 Data untracked + `.gitignore`
- [ ] 11.2 Local wrappers removed
- [ ] 11.3 README links, diagram, status table
- [ ] 11.4 Docs
- [ ] 11.5 GitHub Pages redirect/disable; branch protection on `main`

## Phase 12: After launch
- [ ] First calendar sync observed
- [ ] Late-October earnings wave end to end
- [ ] Monthly restore drill
- [ ] Token renewal (~day 330, i.e. late August 2027)
