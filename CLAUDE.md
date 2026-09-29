# CLAUDE.md — Agent Reference for neocloud-capex-tracker

This is the master reference for any Claude agent working on this
codebase. Read this FIRST before making any changes.

## Environment: Windows checkout, WSL workflow

The checkout lives on the Windows filesystem
(`C:\Users\FKaiY\Desktop\AI Agent Repos\Claude\neocloud-capex-tracker`)
but is developed and run from WSL Ubuntu
(`/mnt/c/Users/FKaiY/Desktop/AI Agent Repos/Claude/neocloud-capex-tracker`).

- Run git, Python, tests and scripts **from WSL**. Never `git add -A`,
  stash, checkout or clean from Windows Git: it runs with
  `core.autocrlf=true` and sees NTFS-mangled filenames differently.
- Dev environment: uv-managed Python 3.12 venv at `~/.venvs/capex` (on
  the Linux filesystem; WSL's system Python is 3.10, below the
  project's 3.11 floor), built from `uv.lock` by `scripts/dev_setup.sh`.
  `source ~/.venvs/capex/bin/activate`. After changing dependencies in
  `pyproject.toml`, run `uv lock` and commit `uv.lock`: CI and the
  server install exactly the locked versions (CI fails on a stale lock).
- `.gitattributes` pins LF (`*.sh` must stay LF or bash breaks), and
  `tests/unit/test_repo_hygiene.py` fails CI on any tracked path that
  Windows can't check out (`<>:"|?*`, reserved names, trailing dot or
  space, case-only collisions).
- The move to an always-on AWS server is tracked step by step in
  `docs/SERVER_MIGRATION_CHECKLIST.md`. Server infrastructure lives in
  `deploy/` (see `deploy/README.md`); `deploy/aws/run_on_host.py` runs a
  command on the host via SSM without SSH. Merging to `main` deploys to
  the server once CI passes (`capex-deploy`); operations are in
  `docs/SERVER_OPERATIONS.md`.
- `CAPEX_HOME` moves all runtime data (see `src/capex/paths.py`).
  **GitHub holds code only**: the DB, workbooks, charts and site live on
  the server (the only writer) and in S3. To work on real data locally:
  `scripts/pull_server_snapshot.sh [--raw]`, then
  `CAPEX_HOME=~/capex-snapshot capex …`. Local changes to data are never
  sent back; data fixes run on the server (docs/SERVER_OPERATIONS.md).

## CLI Commands

```bash
capex db migrate              # apply pending DB migrations
capex db sync-all             # migrate + sync companies + metrics from YAML
capex fetch <TICKER> <FORM>   # download latest filing from SEC/HKEX → _raw/ (6-K: latest earnings release)
capex extract <TICKER>        # dry-run: show sections + metrics for extraction
capex export [-o PATH]        # generate Excel workbook from DB
capex chart [-o PATH]         # regenerate PNG chart (YoY auto-recalculated)
capex monitor --catch-up [--dry-run] [--sweep]   # one watcher run (see monitor/pipeline.py)
capex calendar requeue --since DATE --refresh-forms
capex llm ping                # one real call through the production LLM backend
capex settings list|set K V   # runtime settings (audited; see `capex settings help`)
capex server jobs|runs|log N  # schedules, recent runs, one run's log
capex server run JOB [--now]  # queue a job for the scheduler (--now: run it here)
capex server scheduler        # the always-on scheduler (systemd on the server)
capex server admin            # admin panel on 127.0.0.1:8081 (scripts/admin_tunnel.sh from WSL)
capex server publish --dry-run | backup | restore | health | doctor
```

## Key Modules

| Module | Purpose |
|---|---|
| `src/capex/paths.py` | **Every filesystem location.** Code/config (seeds, prompts) under the checkout; runtime data (DB, raw filings, workbooks, charts, `site/`, reports) under `$CAPEX_HOME` (default: the checkout). `resolve_raw_path()` maps DB `raw_path` values to files. Never build `Path(__file__).parents[...]` data paths. |
| `src/capex/adapters/cli_backend.py` | The only LLM entry point: `claude -p` with the prompt on stdin, JSON output, no tools, an empty cwd, budget/pause/telemetry. Build it with `CLIBackend.from_settings()`. |
| `src/capex/adapters/errors.py` | Typed LLM errors. `FATAL_LLM_ERRORS` (auth, usage limit, budget, config) must never be swallowed — re-raise them. |
| `src/capex/settings.py` | Runtime settings registry (`settings` table, audited). Add new knobs here, never as ad-hoc env vars. |
| `src/capex/server/secrets.py` | Server: SSM Parameter Store `/capex/*` → `/run/capex/capex.env` at boot (`check` shows names/types only) |
| `src/capex/server/doctor.py` | Server health checks (claude, SEC, Alpha Vantage, Gmail, S3→CloudFront, memory, disk, data volume) |
| `src/capex/server/scheduler.py` | The always-on scheduler: every 30 s a heartbeat, due schedules queued (cron in Europe/London, missed runs collapse to one), one request claimed and run as `python -m capex.server.jobs JOB` with a timeout; failures email the operator |
| `src/capex/server/schedules.py` | `job_schedules` / `job_requests` / `runs` helpers and the default schedule of every job (`JOBS`). Operational writes use `Database.ops_write()` (no dump.sql churn). |
| `src/capex/server/jobs.py` | The jobs: watcher, filings_sweep, calendar_sync, regenerate_outputs, publish, backup, backup_raw, health, llm_check, prune. Exit codes 0 / 3 partial / 4 skipped / 75 deferred / 77 auth. |
| `src/capex/server/locks.py` | File locks under `run/`: `pipeline` (every writer of filings, extractions or the site, including `capex monitor`) and `scheduler` |
| `src/capex/server/publish.py` | Site + workbooks → S3 (changed files only, safe workbook keys served under their real names, `download/latest.xlsx`, `workbooks.html`) → one CloudFront invalidation |
| `src/capex/server/backup.py` | Nightly verified DB backups (gz + SQL dump) to S3 with local rotation, weekly raw-filing sync, verified restore |
| `src/capex/server/health.py` | Heartbeat, token age, job freshness, failed jobs/filings, LLM pause/budget, disk, claude, email — alerts via `notify/ops.py` |
| `src/capex/notify/ops.py` | Operator alert emails, de-duplicated per key through `alerts_sent` |
| `src/capex/server/admin/` | The admin panel (FastAPI + Jinja2 templates, no JS): watchlist, calendar and filings, schedules, runs, subscribers, settings, audit. Localhost-only via SSH tunnel; Host check + form token + same-origin POSTs. It edits control tables (audited as `admin`) and queues `job_requests` — never runs jobs itself. |
| `src/capex/monitor/pipeline.py` | The watcher state machine: calendar row → `filing_events` → fetch the exact accession → extract → outputs. Retries with backoff, stale rows, fatal-LLM stop. `run.py` is its CLI. |
| `src/capex/monitor/watchlist.py` | Runtime watch list (which companies, which forms); `expected_form()` |
| `src/capex/fetch/sec_http.py` | The only way to call SEC: contact UA, ≤ 5 req/s, retries honouring Retry-After |
| `src/capex/fetch/sec.py` | SEC EDGAR fetcher — `list_filings()` / `fetch_accession()` / `fetch_latest()` with canonical names |
| `src/capex/fetch/sec_6k.py` | 6-K earnings releases: `find_earnings_release()` picks the release out of a foreign filer's 6-Ks (EDGAR gives a 6-K no period; most are buyback returns or notices) and derives its period from the text; `fetch_release()` saves the EX-99 exhibits as one `[filed][T][Qn][6-K].htm` |
| `src/capex/fetch/hkex.py` | HKEXnews fetcher — downloads HKEX annual/interim reports |
| `src/capex/fetch/dispatcher.py` | Routes fetch requests by form_type + source |
| `src/capex/fetch/sidecar.py` | JSON sidecar writer/reader for raw archive |
| `src/capex/read/text.py` | Extract text from HTML (SEC) or PDF (HKEX via pdfplumber) |
| `src/capex/read/sections.py` | Parse text into named sections (Items 7, 8, Notes) |
| `src/capex/extract/writer.py` | Adapter-agnostic DB writer — validates + writes extractions |
| `src/capex/extract/segment.py` | Generalized segment revenue extractor with table scoring |
| `src/capex/extract/extractors/llm_headless.py` | Per-metric dual-agent extractor — used by PEL re-extract, audit re-verify, restatement sweep, and as the fallback for the multi-metric path |
| `src/capex/extract/extractors/llm_headless_filing.py` | Per-filing dual-agent extractor — one Agent A call covers all 6 metrics, then one Agent B call per metric. Used by `monitor/watcher.py` via `router.extract_filing()` |
| `src/capex/extract/router.py` | `extract_metric()` (per-metric, retained for PEL/audit) and `extract_filing()` (per-filing bulk path used by the auto-update watcher) |
| `src/capex/notify/orchestrator.py` | `notify_subscribers(results)` — called by the watcher pipeline for fresh filings. Builds one email per (subscriber, filing), resolving the filing by `source_document_id`; links come from `publish.public_base_url`. |
| `src/capex/notify/subscribers.py` | Subscribers in the DB `subscribers` table (private, audited); YAML only with an explicit path or `NOTIFY_SUBSCRIBERS_PATH` (`capex notify import-yaml` moves it into the DB) |
| `src/capex/notify/performance.py` | QoQ + YoY comparisons for the email's metric table |
| `src/capex/notify/formatter.py` | HTML + plain-text body builder, subject-line generator |
| `src/capex/notify/email_sender.py` | Gmail SMTP via stdlib `smtplib.SMTP_SSL`, reads `GMAIL_USERNAME` / `GMAIL_APP_PASSWORD` from env |
| `src/capex/xbrl/timeseries.py` | XBRL companyfacts API — pulls full quarterly history |
| `src/capex/fx/rates.py` | FX rate lookups via frankfurter.app (ECB data) |
| `src/capex/db/schema.py` | SQLite Database wrapper + migrator |
| `src/capex/db/sync.py` | YAML → DB sync (companies + metric_definitions) |
| `src/capex/exporters/excel.py` | Excel workbook generator (all values in USD) |
| `src/capex/exporters/charts.py` | Chart generator (YoY always recalculated from DB) |
| `src/capex/exporters/citations.py` | Source citation formatter for Excel cell comments |
| `src/capex/query/line_items.py` | User-facing metric lookup with cache |
| `src/capex/organize/namer.py` | Canonical filename grammar + period derivation (KEPT) |
| `src/capex/organize/walker.py` | DEPRECATED — organize step removed, naming at fetch time |

## Key Data Files

| File | Purpose |
|---|---|
| `data/_sources/_identity.yaml` | Company registry — ticker, CIK, FYE, currency |
| `data/seeds/coverage.yaml` | Coverage treatments — per-company adjustments, derivations |
| `data/seeds/metric_definitions.yaml` | Metric registry with XBRL concepts + aliases |
| `$CAPEX_HOME/data/db/capex.db` | SQLite database. The system of record is the server's (`/var/lib/capex`); any local copy is untracked |
| `dump.sql` | Optional SQL dump next to the DB (`CAPEX_DUMP_SQL=1`); the server's nightly backups include one |

## Skills

| Skill | When to use |
|---|---|
| `fetch-company-report` | Download a filing from SEC/HKEX |
| `read-and-extract` | Extract metrics from a downloaded filing (v1: Claude Code interactive) |
| `query-line-item` | Look up an extracted metric with provenance |
| `organize-sources` | DEPRECATED — naming now happens at fetch time |

## Critical Rules

0. **Excel workbook filenames.** Every exported workbook under
   `workbook/` is named `[YYYY.MM.DD - HHhMM] financials sourcebook.xlsx`
   (minute precision, `h` between hour and minute, clock = `CAPEX_TZ`,
   default Europe/London). If two exports land in the same minute,
   append ` v2`, ` v3`, ... — do NOT revert to the old
   `capex_tracker_vN.xlsx` scheme, and never put `:` in a filename: it
   is illegal on Windows, and WSL stores it as U+F03A, which Windows Git
   reports as deleted + untracked. `capex export` auto-generates the
   name via `default_workbook_path()` in `src/capex/exporters/excel.py`;
   use `latest_workbook()` to find the newest one (a plain sort ranks
   ` v2` wrongly). Manually writing a workbook? Follow the same format.

1. **ALWAYS fetch before extracting.** Use `capex fetch` to download
   reports to `data/_sources/<TICKER>/_raw/` BEFORE extracting data.
   NEVER download to temp files that get deleted.

2. **All Excel values in USD.** Non-USD companies are FX-converted.
   Local currency is in the cell comment only (for audit).

3. **Citations use EXTERNAL URLs only.** SEC EDGAR or HKEXnews links
   that an analyst can copy-paste into a browser. NEVER reference
   local file paths, GitHub repo URLs, or our codebase.

4. **YoY growth is always recalculated.** Never cache or filter YoY
   values. Call `capex chart` after any data change.

5. **BABA + BIDU XBRL values are in USD.** SEC XBRL for 20-F filers
   returns USD convenience translations. Do NOT treat them as CNY.
   GDS XBRL IS in CNY (correct).

## Common Workflows

**Add a new company:**
1. Add entry to `data/_sources/_identity.yaml`
2. Add entry to `data/seeds/coverage.yaml` (treatments + adjustments)
3. Run `capex db sync-all`
4. Run `capex fetch <TICKER> <FORM>`
5. Extract metrics (via Claude Code or headless adapter)
6. Run `capex export` + `capex chart`

**Extract a new metric from existing filings:**
1. Add metric to `data/seeds/metric_definitions.yaml`
2. Run `capex db sync-metrics`
3. Read the filing from `data/_sources/<TICKER>/_raw/`
4. Extract via Claude Code or `segment.py`
5. Write results via `writer.py`

**Regenerate outputs after data changes:**
```bash
capex export          # auto-named per Rule 0
capex chart
```
Outputs land under `$CAPEX_HOME` (the checkout by default): `workbook/`,
`charts/`, and the HTML pages in `site/`, all gitignored. The server
regenerates and publishes them itself (S3 + CloudFront,
https://d1pdb32k3hz8st.cloudfront.net); `docs/*.html` only redirect
there. Never commit generated outputs or data.

**Update README architecture diagram and status table when adding features:**

The README contains a Mermaid architecture diagram and a development
status table. Both MUST be updated when a feature is added, a module
is renamed, or a phase ships. The diagram is located between
`<!-- ARCHITECTURE_START -->` and `<!-- ARCHITECTURE_END -->` markers.

How to update the architecture diagram:

1. **Add a module** — add a node line inside the correct subgraph,
   add edge(s), and append the node ID to the matching `class` line:
   ```
   # Inside the subgraph:
   MYMOD["mymodule.py\nshort description"]
   # Edge:
   PREV_NODE --> MYMOD --> NEXT_NODE
   # Color class (append ID):
   class ...,MYMOD process
   ```

2. **Add an extraction strategy** — add the node inside `subgraph
   Extractors`, wire `SECT --> NEW_EXT` and `NEW_EXT --> WRITER`.

3. **Add an external source** — add the node inside `subgraph Sources`,
   create a fetch node in `subgraph L1`, wire `SOURCE --> FETCH --> DISP`.

4. **Add an export format** — add the node inside `subgraph L6`,
   wire `DB --> EXPORTER --> OUTPUT`, add the output node in `subgraph Out`.

5. **Rename a module** — change the label text inside quotes. Keep the
   node ID unchanged so existing edges still work.

6. **Remove a module** — delete the node line, all edges referencing it,
   and its ID from the `class` line.

Subgraph-to-layer mapping (must match `docs/SYSTEM_DESIGN.md`):
- `Sources` → External data providers (SEC, HKEX, XBRL, ECB)
- `L1` → 1 — Fetch
- `L2` → 2 — Raw Archive
- `L4` → 3 — Read + Extract (nested `Extractors` subgraph)
- `L3` → 4 — Storage Trunk
- `L6` → 5 — Export
- `Out` → Outputs

Color classes (append new node IDs to the correct line):
- `source` (blue) — external data providers
- `store` (orange) — data stores (DB, archive, dump.sql)
- `process` (purple) — internal processing modules
- `output` (green) — deliverable outputs

How to update the development status table:

- Phase ships: swap emoji `📋` → `🚧` → `✅`
- New feature planned: add a row with `📋`
- Update the "Current data" summary line at the bottom if counts change
