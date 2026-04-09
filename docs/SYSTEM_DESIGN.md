# System Design — neocloud-capex-tracker

**Status:** v0.6 — DB-first, skills-anchored. Phase 1 (foundation) landed 2026-04-09.
**This document is authoritative.** When it disagrees with any older doc
(including `docs/neocloud_capex_tracker_design_memo.pdf`), this wins. The
PDF is kept as a historical record of the v0.5 design and its rejected
alternatives, but its nine-layer architecture has been superseded.
**Last updated:** 2026-04-09

Any architectural change must be reflected here before it lands in code.
Work-in-progress architectural changes live in `RESTRUCTURING_PLAN.md`;
once executed, they fold back into this doc and the plan is archived.

---

## 1. Purpose

A collaborative project that automatically tracks, extracts, and evaluates
AI-related capital expenditure disclosures from major hyperscalers and neocloud
providers. Runs on a recurring schedule, ingests quarterly and annual filings
as they are released, routes work across multiple LLM providers under a strict
interchange protocol, and stores validated results in a SQLite database that
can be queried ad-hoc or exported into Excel, CSV, JSON, or any other format
as needed.

## 2. Goals and non-goals

### Goals

- Track AI-attributable capex across major hyperscalers and pure-play neoclouds, quarterly cadence.
- Quantify both **volume** (raw spend) and **efficiency** (capex normalized to revenue, capacity, or output).
- Operate continuously and autonomously, triggered by the publication of new filings.
- Minimize token cost via model-agnostic routing under a universal interchange protocol.
- Single system of record (SQLite DB) that exports cleanly into multiple formats.
- Maintain a fully auditable trail: every extracted figure traceable to a specific quote in a specific source document, plus an append-only audit log of every DB-mutating action.
- Friendly collaboration via GitHub: code, data dump, source archive, and history all in one repository.

### Non-goals (v1)

- Real-time / intra-day tracking.
- Investment recommendations or financial advice.
- Coverage of private companies without quarterly disclosures.
- Hosted web dashboard.
- Multi-writer concurrency on the database.

## 3. Guiding principles

- **Provenance over plausibility.** Every value carries the quote and page it came from.
- **Mechanical checks before model checks.** Substring verification, range rules, and SQL constraints run before any eval model is consulted.
- **DB is the trunk, everything else is a branch.** The SQLite database is the single source of truth. Excel, CSV, and future web dashboards are read-only exports derived from it.
- **Model-agnostic interchange.** Swapping models is a config change, not a refactor.
- **Per-report context isolation.** Extraction runs one PDF per subagent invocation so context never overflows.
- **Cheap watcher, expensive worker.** LLM inference runs only when a new filing is detected.
- **Skills as the front door.** Every significant operation has a skill that Claude can invoke. The skill is the contract; the Python in `src/capex/` is the implementation.
- **Defer features that aren't justified yet.**

## 4. Architecture — six layers

| # | Layer | Lives in | Skills that touch it |
|---|---|---|---|
| 1 | **Source acquisition** | `src/capex/fetch/` + `data/_sources/<TICKER>/_raw/` | `fetch-company-report` |
| 2 | **Canonicalization** | `src/capex/organize/` + `data/_sources/<TICKER>/<YYYY>/` | `organize-sources` |
| 3 | **Storage trunk** | `src/capex/db/` + `data/db/capex.db` + `data/db/dump.sql` | every mutating skill |
| 4 | **Read + extract** | `src/capex/read/` + `src/capex/extract/` + `src/capex/adapters/` | `read-and-extract` (worker) |
| 5 | **Query / lookup** | `src/capex/query/` | `query-line-item` (user front door) |
| 6 | **Export** | `src/capex/exporters/` (excel, csv, json) | `export-workbook` |

**Cross-cutting modules** that don't sit in any one layer:

- `src/capex/protocol/` — versioned Pydantic schemas for the interchange contract.
- `src/capex/validation/` — provenance substring match, consistency rules (formerly in Excel formulas).
- `src/capex/cli/` — command-line entrypoints. Every skill has a CLI equivalent so humans and cron can invoke the same code without Claude in the loop.

## 5. Storage trunk (Layer 3)

The DB is the load-bearing layer of the whole system. Everything downstream
reads from it; everything upstream writes into it.

### 5.1 Why SQLite

- Zero infrastructure, single file, works identically in WSL / CI / macOS.
- Plenty fast for v1's quarterly cadence and one-company-at-a-time access patterns.
- Ships with Python's stdlib; no driver install, no container, no service.
- Easy migration path: when scale demands it, `sqlite3 capex.db .dump | psql` is a ten-second port to Postgres.

### 5.2 Files and commit strategy

Two files, both committed:

```
data/db/capex.db     # binary SQLite database, runtime truth
data/db/dump.sql     # plaintext SQL dump, auto-regenerated on every mutation
```

- **`capex.db`** is the runtime. Skills, CLIs, and queries all talk to this.
- **`dump.sql`** is for humans. `git diff dump.sql` on a PR tells a reviewer exactly what a DB-mutating commit did — which tables got rows, which values changed, which audit entries were written. Without this, binary diffs would make every DB write a black-box commit and break the auditability principle in §3.

Every successful mutation regenerates `dump.sql` via `capex.db.dump.dump_to_sql()`. This is wired into the `Database.mutating()` context manager, so there is no path to write the DB without also updating the dump.

When the binary outgrows comfort (~10 MB), the plan is to drop the binary from
the repo, keep only the dump, and rebuild the binary on clone via a
`make db` step. We are nowhere near that threshold.

### 5.3 Schema (v0.1)

Eight tables. Authoritative SQL at
`src/capex/db/migrations/0001_init.sql`. Field-level notes in that file's
comments.

| Table | Purpose |
|---|---|
| `schema_version` | Single-row migration tracking. |
| `companies` | Mirror of `data/_sources/_identity.yaml`. Refreshed at skill startup. |
| `source_documents` | One row per fetched filing. Immutable identity = sha256 of the bytes. |
| `metric_definitions` | Canonical metric registry. Mirror of `data/seeds/metric_definitions.yaml`. |
| `extractions` | The fact cache. One row per (source_document, metric, extracting_model). Written by the extraction worker, read by the query skill. |
| `validation_results` | Per-extraction check outcomes (provenance match, range checks, etc). |
| `audit_log` | Append-only record of every DB-mutating action. Skill name, timestamp, JSON payload. |
| `golden_facts` | Hand-labeled regression baseline, pinned to specific filings by sha256. |

**Deliberately not in v0.1:** `extraction_runs` (batch run metadata — YAGNI
until we batch), multi-writer locking (single writer assumed), triangulation
tables (handled at query time via SQL), user accounts.

### 5.4 Write discipline

- **All writes go through `Database.mutating()`.** This context manager opens a connection with foreign keys enforced, yields it to the caller, commits on successful exit, and regenerates `dump.sql`. A raw `sqlite3.connect()` write bypasses the dump hook and is considered a bug.
- **One `with mutating()` block = one atomic unit of work = one dump regeneration.** Multiple SQL statements inside a single block produce exactly one dump at the end.
- **Every mutation writes an `audit_log` row** describing what it did, as JSON in the `payload` column. This is how history gets reconstructed from the dump.
- **Reads may use `Database.connect()` directly.** Reads never regenerate the dump.

### 5.5 YAML-seeded mirror tables

Two tables are treated as caches of hand-edited YAML files, not as
system-of-record data:

- `companies` ← `data/_sources/_identity.yaml`
- `metric_definitions` ← `data/seeds/metric_definitions.yaml`

The sync functions in `src/capex/db/sync.py` use upsert semantics (no
drop-and-recreate) so FKs into these tables never break mid-sync. If a row
is removed from YAML but still has downstream references, the sync raises
an error and forces the user to resolve by hand. Drift always flows
YAML → DB, never reverse.

Adding a company or a metric is a two-step workflow: edit the YAML, run
`capex db sync-companies` (or `sync-metrics`).

## 6. Source archive (Layers 1-2)

Immutable raw archive plus derived canonical layer. This is the only part
of the v0.5 design that is unchanged.

```
data/_sources/
├── _identity.yaml             # company registry (authoritative)
├── _organizer_log.csv         # append-only log from organize-sources
└── <TICKER>/
    ├── _raw/                  # immutable, original regulator bytes
    │   ├── <sanitized>.pdf
    │   └── <sanitized>.fetch.json
    └── <YYYY>/                # canonical, regeneratable from _raw/
        └── [dd.mm.yyyy][TICKER][PERIOD][FORM].pdf
```

- `_raw/` is written by `fetch-company-report` and read by nothing else except `organize-sources`. A bug in naming cannot corrupt this layer.
- `<YYYY>/` is written by `organize-sources` and can be regenerated from scratch by deleting the year folders and re-running the sweep.
- The `sha256` of the `_raw/` bytes is the document's permanent identity and feeds the `source_documents.sha256` column as a uniqueness key.

See `skills/fetch-company-report/SKILL.md` and `skills/organize-sources/SKILL.md`
for the skill contracts. Phase 2 will slim those files to contract-only and
move the implementation details into `src/capex/fetch/` and `src/capex/organize/`.

## 7. Extraction (Layer 4)

One worker skill — `read-and-extract` — is the only thing in the system
that calls an LLM. Its contract (landing in Phase 3):

- **Input:** one `source_document_id` + a list of `metric_key`s to extract.
- **Context isolation:** one invocation sees one PDF. The parent agent never accumulates the contents of multiple filings in a single context window.
- **Output:** rows inserted into `extractions` and `validation_results`. Returns the list of extraction IDs for the caller's convenience.
- **Provenance:** every row carries `quote` (verbatim, ≤30 words), `locator_page`, `locator_section`, and `extraction_type` ∈ {direct, inferred, derived}.

The extraction layer depends only on the `ModelBackend` protocol in
`src/capex/adapters/base.py` (defined in Phase 3). Concrete backends
(Anthropic first, Google/OpenAI later) implement the protocol; the
extractor never imports an SDK directly.

### 7.1 Validation

Three layers of check, all running in Python:

1. **Syntactic** — Pydantic schema validation on every extraction row. Hard gate.
2. **Provenance** — substring match of `quote` against the source PDF text. Fabricated quotes are caught here and rejected before insertion.
3. **Consistency** — range / ratio / relational rules written as Python functions. Replaces the v0.5 Excel-formula validation layer.

Semantic checks that survive the restructure:

- **Golden-set regression** — hand-labeled fixtures in `golden_facts`, replayed on prompt/model changes. CI gates on this.
- **Eval agent** — different model family, narrow yes/no audits (Phase 4+).
- **Cross-source triangulation** — SQL queries across `extractions` rows for the same (company, period, metric) from different filings (10-Q / press release / transcript). Phase 4+.

## 8. Query (Layer 5)

`query-line-item` is the user-facing front door. Contract (Phase 3):

- **Input:** `{ticker, period, line_item}` as a structured call, or a natural-language question that the skill parses into that shape.
- **Resolution:** uses `metric_definitions.aliases` to map a free-text phrase (e.g. "capex", "capital expenditures") to a canonical `metric_key`.
- **Cache check:** looks up `extractions` for the matching `(source_document, metric_key)`. Returns the cached row if present.
- **Cache miss:** invokes `read-and-extract` as a worker subagent on the relevant filing, waits for the new rows to land, returns them.
- **Output:** `{value, unit, quote, page, source_path, sha256}`. The caller (human, another skill, a dashboard) knows exactly where the number came from.

Splitting query and read-and-extract into separate skills is deliberate:
the worker can be invoked by other orchestrators (batch precompute,
golden-set rebuild), and the query skill stays a thin, fast wrapper that
only touches the LLM on cache miss.

## 9. Export (Layer 6)

Read-only derivations of the DB into external formats. v0.5's Excel
workbook — previously the system of record — is demoted to one of
several exporters, with no special status.

- `src/capex/exporters/excel.py` — openpyxl writer. Reads from the DB, emits a .xlsx (Phase 4).
- `src/capex/exporters/csv.py` — per-metric or per-company flat files (Phase 4).
- `src/capex/exporters/json.py` — structured blob for downstream consumers (Phase 4).

Adding a new output format — a web dashboard, a Parquet feed for an
analyst's notebook — is a new file in `exporters/` that reads from the DB
and writes bytes. It never requires touching extraction, validation, or
storage.

## 10. Infrastructure

### 10.1 Repository layout (v0.6)

```
.
├── .github/workflows/                  # scheduled jobs, CI (updated Phase 4)
├── data/
│   ├── _sources/                       # immutable source archive
│   │   └── _identity.yaml              # authoritative company registry
│   ├── db/
│   │   ├── capex.db                    # SQLite runtime trunk
│   │   └── dump.sql                    # auto-generated SQL dump
│   └── seeds/
│       └── metric_definitions.yaml     # authoritative metric registry
├── docs/
│   ├── SYSTEM_DESIGN.md                # this file (trunk)
│   ├── RESTRUCTURING_PLAN.md           # active restructuring work
│   └── neocloud_capex_tracker_design_memo.pdf  # v0.5, historical
├── skills/
│   ├── fetch-company-report/SKILL.md
│   ├── organize-sources/SKILL.md
│   ├── read-and-extract/SKILL.md       # Phase 3
│   ├── query-line-item/SKILL.md        # Phase 3
│   └── export-workbook/SKILL.md        # Phase 4
├── src/capex/                          # the package
│   ├── fetch/
│   ├── organize/
│   ├── read/
│   ├── extract/
│   ├── adapters/
│   ├── protocol/
│   ├── db/
│   │   ├── schema.py
│   │   ├── dump.py
│   │   ├── sync.py
│   │   └── migrations/0001_init.sql
│   ├── query/
│   ├── validation/
│   ├── exporters/
│   └── cli/main.py
├── tests/
└── pyproject.toml
```

### 10.2 Why GitHub for both code and data

- Single source of truth across code, DB dump, source archive, and history.
- Every quarterly run is a commit → git-tracked time series of the dataset for free.
- Actions concurrency groups enforce single-writer.
- Free tier covers quarterly cadence comfortably.

### 10.3 Caveats handled

- **Binary .db diffs are useless** → auto-generated `dump.sql` committed alongside every DB write.
- **Repo bloat** → Git LFS for source PDFs is planned when the archive grows; `.db` stays in main repo until the single-file SQLite size warrants otherwise.
- **Secrets** → GitHub Actions secrets only; nothing committed.

## 11. Rejected alternatives

### 11.1 Workbook as the system of record (v0.5 design)

Rejected during the v0.6 restructuring. Excel formulas as validation rules
were hard to test, hard to version, and hard to review in PRs. Excel as the
storage trunk made alternative exports (CSV, JSON, Parquet, web) awkward.
Moving to a SQLite trunk gave us all three benefits (testability, diff-ability,
pluggable exports) with a few hours of work. Excel now lives as one of several
read-only exporters.

### 11.2 Postgres instead of SQLite

Deferred. v1 has no multi-writer use case and SQLite's zero-infrastructure
story is worth a lot during the early build phase. Porting later is ten
seconds via `.dump`.

### 11.3 Claude for Excel as an automated write interface

Still rejected (as in v0.5). Interactive sidebar only, no REST API, no
headless mode. Reclassified as an optional human interface for ad-hoc
analyst queries, strictly outside the automated pipeline.

### 11.4 Multi-model routing on day one

Still deferred (as in v0.5). Protocol is designed for it; routing logic
will not land until measured evidence justifies the orchestration overhead.

## 12. Open items

- [ ] Golden dataset scope, 3-year baseline methodology, labeling protocol — dedicated session
- [ ] Target company list for v1 — recommend starting with 3-4 US hyperscalers on SEC EDGAR
- [ ] Efficiency metrics formal definitions (capex/revenue, capex/OCF, capex/PP&E turnover)
- [ ] Eval agent model family choice
- [ ] Blocking vs flagging on eval agent disagreement
- [ ] Git LFS for source PDFs (recommended yes; decide when first PDF lands)
- [ ] Collaboration protocol — branching model, review rules, ownership of layers
- [ ] Whether to add a `watcher` layer that polls for new filings, or keep fetch-company-report as user-triggered in v1

## 13. Next steps

Work in progress is tracked in `docs/RESTRUCTURING_PLAN.md`. Phase 1
(foundation — memo rewrite, DB layer, YAML syncs) is complete.
Phase 2 wires the fetch and organize skills into the DB. Phase 3 adds the
read-and-extract and query-line-item skills — the first end-to-end
vertical slice.
