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
- [ ] PR merged with CI green

## Phase 2: AWS foundation and early smoke test (PR 2) — pause (b)
- [ ] 2.1 `deploy/aws/capex-stack.yaml` (site and backup buckets, CloudFront OAC, IAM role, key pair, security group, EC2)
- [ ] 2.2 `deploy/bootstrap.sh`
- [ ] 2.3 `capex-secrets.service` + `capex.server.secrets`
- [ ] 2.4 `deploy/aws/allow_my_ip.sh`, `scripts/ssh_config.example`
- [ ] 2.5 Stack deployed; `deploy/smoke_test.sh` passes (claude, SEC, Alpha Vantage, Gmail, S3/CloudFront, memory)

## Phase 3: Code/data split, central paths, DB concurrency (PR 3)
- [ ] 3.1 `capex/paths.py`
- [ ] 3.2 Path derivations routed through `paths`
- [ ] 3.3 Generated HTML moves to the site dir; `workbooks.html`
- [ ] 3.4 `Database`: busy timeout, journal mode from env, `connect_ro()`, dump opt-in
- [ ] 3.5 `audit/fixes.py` env merge

## Phase 4: LLM backend hardening and runtime-control schema (PR 4)
- [ ] 4.1 Migration 0011 (settings, watchlist, job_schedules, job_requests, runs, subscribers, alerts_sent, llm_calls)
- [ ] 4.2 `capex/settings.py`
- [ ] 4.3 `adapters/errors.py`
- [ ] 4.4 `cli_backend.py`: prompt on stdin, JSON output, no tools, empty cwd, error classes
- [ ] 4.5 Fatal LLM errors no longer swallowed
- [ ] 4.6 `capex llm ping`, `capex settings`

## Phase 5: Watcher correctness and pipeline refactor (PR 5)
- [ ] 5.1 Migration 0012 (filing_events, calendar v2)
- [ ] 5.2 IREN seeds → 10-K/10-Q
- [ ] 5.3 `monitor/watchlist.py`
- [ ] 5.4 `fetch/sec_http.py`
- [ ] 5.5 `find_filings` / `fetch_accession`; idempotent `record_source_document`
- [ ] 5.6 Calendar: key validation, expected forms, lookback
- [ ] 5.7 Watcher: poll results, no success-on-exception
- [ ] 5.8 `monitor/pipeline.py`
- [ ] 5.9 `run.py` thin CLI (git push and issue creation removed, real exit codes)
- [ ] 5.10 `capex calendar requeue`

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
