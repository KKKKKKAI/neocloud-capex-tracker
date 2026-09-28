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
- [ ] Re-save both as **SecureString**; they are type `String` now. Rotate the Claude token while doing it (`claude setup-token`).
- [ ] Create `/capex/GMAIL_APP_PASSWORD` (SecureString) and `/capex/GMAIL_USERNAME` (String) in `eu-north-1`. They don't exist in any region yet.
- [ ] Create a non-root admin identity for the CLI (the CLI currently uses the root user). Needed before Phase 2.

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

## Phase 6: 6-K quarterly results for NBIS, GDS, BIDU, BABA (PR 6)
- [ ] 6.1 `fetch/sec_6k.py`
- [ ] 6.2 namer / dispatcher / sections / router / coverage fixes
- [ ] GDS Q2 2026 extracted — pause (d) spot-check

## Phase 7: Scheduler, jobs, publish, backups, health, alerts, subscribers (PR 7)
- [ ] 7.1 Jobs
- [ ] 7.2 Scheduler
- [ ] 7.3 Default schedules
- [ ] 7.4 Publish to S3 + CloudFront invalidation
- [ ] 7.5 Backups to S3
- [ ] 7.6 Health
- [ ] 7.7 Operator alerts
- [ ] 7.8 Subscribers in DB
- [ ] 7.9 `server` extra

## Phase 8: Admin panel over the SSH tunnel (PR 8)
- [ ] 8.1 FastAPI admin (Host check, CSRF, all pages)
- [ ] 8.2 `scripts/admin_tunnel.sh` / `.ps1`
- [ ] 8.3 Download-latest and workbooks links on the site

## Phase 9: Deploy pipeline and runbooks (PR 9)
- [ ] 9.1 `uv.lock`
- [ ] 9.2 systemd units
- [ ] 9.3 CI-gated pull deploy with rollback
- [ ] 9.4 `dev_setup.sh`, `pull_server_snapshot.sh`
- [ ] 9.5 `docs/SERVER_OPERATIONS.md`
- [ ] 9.6 shellcheck + cfn-lint in CI

## Phase 10: Migrate data and go live
- [ ] 10.1 First deploy + `capex server doctor`
- [ ] 10.2–10.3 Data copied and imported
- [ ] 10.4 Settings pre-filled
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
