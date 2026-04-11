# Restructuring Plan — v0.5 → v0.6

**Status:** Phase 1 complete (2026-04-09). Phases 2-5 pending.
**Created:** 2026-04-09
**Owner:** @KKKKKKAI
**Supersedes:** parts of `SYSTEM_DESIGN.md` (rewritten in Phase 1).

This document is the working tracker for the v0.6 architectural restructuring.
It captures the decisions, the new architecture, the DB schema, and the phased
execution plan. Updated as work progresses; checkboxes flip to `[x]` as items
land. When the plan is fully executed, the contents move into
`SYSTEM_DESIGN.md` and this file is archived.

---

## 0. Database scope (load-bearing clarification, 2026-04-09)

**The database does NOT store the content of annual reports.**

The annual reports themselves live as their original files (HTML or PDF, exactly as the regulator served them) in `data/_sources/<TICKER>/_raw/` (immutable archive) and `data/_sources/<TICKER>/<YYYY>/` (canonical layer maintained by `organize-sources`). The full text of a report is never inserted into a SQLite column — it would be inaccessible to humans and counterproductive for review.

The database stores **specific data points extracted from those reports**: one row per `(company, period, metric)` in the `extractions` table, e.g. "MSFT FY2025 capital_expenditures = 88,000 USD millions". Each extraction row carries provenance back to its source file via `source_document_id` (FK to the file's row in `source_documents`) plus a verbatim quote and a section reference (e.g. `"Item 8 - Consolidated Statements of Cash Flows, line 'Additions to property and equipment'"`).

The DB is the **structured fact ledger**. The `_sources/` directory is the **document archive**. The two are linked by `source_document_id` but they store different things: facts vs. files.

This is what makes the architecture useful: a human reviewer can read `dump.sql` to see exactly what numbers came from where, then click through to the original PDF in `_sources/` to verify. The DB never tries to be a document store.

---

## 1. Why we're restructuring

The v0.5 design memo committed the project to **workbook-as-engine**: an Excel
template would hold validation formulas, the openpyxl write adapter would be
the canonical writer, and LibreOffice headless recalc would evaluate
consistency rules. That bet had three downsides that became clear as the
skills layer matured:

1. **Excel as the source of truth makes alternative outputs hard.** Future
   exports (CSV, JSON, Parquet, web dashboard, API) all want to read from a
   queryable trunk, not parse a workbook.
2. **Validation logic in formulas is hard to test, version, and review.** SQL
   constraints and Python checks are diff-friendly and unit-testable.
3. **The skills layer outgrew its slot in the nine-layer model.** The skills
   describe a `fetch → organize → read → query` flow that the old layer model
   doesn't cleanly express.

The restructuring keeps the **immutable source archive**, the **interchange
protocol**, and the **provenance discipline** — these were the load-bearing
parts that worked. It replaces the workbook-as-engine layers with a SQLite
trunk and demotes Excel to one of several possible exporters.

---

## 2. Decisions locked

| # | Decision | Choice | Rationale |
|---|---|---|---|
| A | Database engine | **SQLite** | Single-file, zero infra, fast setup. |
| B | DB commit strategy | **Binary `data/db/capex.db` AND auto-generated `data/db/dump.sql`** | Binary is runtime; dump is what humans review in PRs. Preserves auditability without parsing-the-binary in CI. When the binary outgrows comfort (~10MB), drop it and rebuild from dump on clone. |
| C | Skill ↔ src boundary | **Skills are thin contracts; `src/capex/` does the work.** Each skill is a markdown contract + a call into a Python module. | Same Python can be invoked by Claude, by cron, or by a human CLI. SKILL.md files become contract-only; implementation lives in Python with proper docstrings and tests. |
| D | Identity table | **`_identity.yaml` stays authoritative**, DB has a `companies` mirror refreshed at skill startup. | Companies change rarely; YAML is the best human-edit surface; refresh cost is ~1ms. FK references in the DB use `ticker` (stable string), not surrogate ids. Always YAML → DB, never reverse. |
| E | First vertical slice | **Fetch + organize one filing → DB row + new `query-line-item` skill.** | User-stated requirement: lookup line items by name, return value + provenance, with per-PDF context isolation. |
| F | Memo authority | **`SYSTEM_DESIGN.md` becomes the trunk.** PDF marked historical with a "superseded" header in the markdown doc. PDF is not deleted — kept as the original reasoning record for rejected alternatives. | PDFs are not iterable. |

---

## 3. New architecture — six layers

The old nine layers collapse to six because workbook is no longer load-bearing.

| # | Layer | Lives in | Skills that touch it |
|---|---|---|---|
| 1 | **Source acquisition** | `src/capex/fetch/` + `data/_sources/<TICKER>/_raw/` | `fetch-company-report` |
| 2 | **Canonicalization** | `src/capex/organize/` + `data/_sources/<TICKER>/<YYYY>/` | `organize-sources` |
| 3 | **Storage trunk** | `src/capex/db/` + `data/db/capex.db` + `data/db/dump.sql` | every mutating skill |
| 4 | **Read + extract** | `src/capex/read/` + `src/capex/extract/` + `src/capex/adapters/` | `read-and-extract` (worker) |
| 5 | **Query / lookup** | `src/capex/query/` | `query-line-item` (user-facing) |
| 6 | **Export** | `src/capex/exporters/` | `export-workbook`, future CSV/JSON/Parquet |

**Cross-cutting modules:**
- `src/capex/protocol/` — versioned Pydantic schemas for the interchange contract.
- `src/capex/validation/` — provenance verifier (substring match), consistency rules (Python, formerly Excel formulas).
- `src/capex/cli/` — entrypoints so humans can invoke any layer without Claude.

### What this kills from v0.5

- Layer 4 (workbook template), 5 (openpyxl write adapter as primary writer),
  6 (LibreOffice headless recalc), and the formula-as-validation parts of
  layer 7. Workbook becomes a read-only export.
- The `data/csv/` mirror as a diffability hack — replaced by `dump.sql`.
- Sheet protection rules and openpyxl write discipline.

### What survives unchanged

- The `_sources/` archive layout (`_raw/` immutable + canonical year folders).
- The interchange protocol fields (`value`, `quote`, `locator`, `sha256`,
  `extraction_type`, `confidence`, `extracting_model`, `protocol_version`).
  These now describe a DB row shape instead of a workbook row shape.
- Provenance verifier (substring match against source text).
- Golden-set regression — moves from a sheet to a `golden_facts` DB table.

---

## 4. Skills inventory

| Skill | Status | Role |
|---|---|---|
| `fetch-company-report` | exists, refactor to contract-only | Fetch authoritative PDF from SEC/HKEX, write `source_documents` row. |
| `organize-sources` | exists, refactor to contract-only | Canonical naming under `<TICKER>/<YYYY>/`, update `canonical_path` on the existing row. |
| `read-and-extract` | **NEW** | Worker. Takes one `source_document_id` + a list of metric keys. Runs an extraction subagent (one PDF, one isolated context). Writes `extractions` rows + `validation_results`. |
| `query-line-item` | **NEW** | User-facing. Takes `{ticker, period, line_item}` (or natural language). Resolves to canonical metric. Checks `extractions` cache. Falls back to `read-and-extract` on miss. Returns `{value, unit, quote, page, source_path, sha256}`. |
| `export-workbook` | NEW (Phase 4) | Reads DB, writes Excel. Read-only output, not the engine. |

**Why query and read-and-extract are split:** read-and-extract is the worker
that runs in an isolated subagent per PDF (the "don't blow up context"
requirement). query is the front door that resolves the question, checks the
DB cache, and only invokes the worker on a cache miss. Different concerns,
separately testable.

---

## 5. Database schema (v0.1, SQLite)

Eight tables. Authoritative SQL lives in `src/capex/db/migrations/0001_init.sql`
once Phase 1 lands.

### 5.1 `companies`
Mirror of `_identity.yaml`, refreshed at skill startup.

```
ticker                 TEXT PRIMARY KEY
name                   TEXT NOT NULL
preferred_source       TEXT NOT NULL CHECK(preferred_source IN ('sec_edgar','hkex'))
edgar_cik              TEXT
hkex_stock_code        TEXT
fiscal_year_end_month  INTEGER NOT NULL CHECK(fiscal_year_end_month BETWEEN 1 AND 12)
synced_at              TEXT NOT NULL  -- ISO timestamp
```

### 5.2 `source_documents`
One row per fetched filing.

```
id                     INTEGER PRIMARY KEY AUTOINCREMENT
ticker                 TEXT NOT NULL REFERENCES companies(ticker)
form_type              TEXT NOT NULL CHECK(form_type IN ('10-K','10-Q','20-F','HK-AR','HK-IR'))
filing_date            TEXT NOT NULL  -- ISO date
period_of_report       TEXT NOT NULL  -- ISO date
fiscal_year            INTEGER NOT NULL
period_token           TEXT NOT NULL  -- AR / Q1 / Q2 / Q3 / H1 / H2
sha256                 TEXT NOT NULL UNIQUE
raw_path               TEXT NOT NULL  -- repo-relative
canonical_path         TEXT           -- repo-relative, set after organize-sources
source                 TEXT NOT NULL CHECK(source IN ('sec_edgar','hkex'))
source_url             TEXT NOT NULL
accession_number       TEXT           -- SEC only
fetched_at             TEXT NOT NULL
fetcher_version        TEXT NOT NULL
protocol_version       TEXT NOT NULL
UNIQUE(ticker, form_type, period_of_report)
```

### 5.3 `metric_definitions`
Canonical metric registry. Seeded from a YAML file (same pattern as companies)
so additions are reviewable.

```
key                    TEXT PRIMARY KEY  -- e.g. 'capital_expenditures'
label                  TEXT NOT NULL
aliases                TEXT NOT NULL     -- JSON array of common phrasings
unit_default           TEXT NOT NULL     -- 'USD_millions' | 'count' | etc
description            TEXT
```

### 5.4 `extractions`
The cache the query skill checks. One row per `(source_document, metric, model)`.

```
id                     INTEGER PRIMARY KEY AUTOINCREMENT
source_document_id     INTEGER NOT NULL REFERENCES source_documents(id)
metric_key             TEXT NOT NULL REFERENCES metric_definitions(key)
value                  REAL              -- nullable for 'not disclosed'
value_text             TEXT              -- raw text as it appears in the filing
unit                   TEXT NOT NULL
quote                  TEXT NOT NULL     -- ≤ 30 words verbatim
locator_page           INTEGER           -- canonical page id
locator_section        TEXT              -- section heading or anchor
extraction_type        TEXT NOT NULL CHECK(extraction_type IN ('direct','inferred','derived'))
confidence             REAL
extracting_model       TEXT NOT NULL
protocol_version       TEXT NOT NULL
extracted_at           TEXT NOT NULL
UNIQUE(source_document_id, metric_key, extracting_model)
```

### 5.5 `validation_results`
Per-extraction check outcomes.

```
id                     INTEGER PRIMARY KEY AUTOINCREMENT
extraction_id          INTEGER NOT NULL REFERENCES extractions(id)
check_name             TEXT NOT NULL     -- 'provenance_substring' | 'range_check' | etc
passed                 INTEGER NOT NULL CHECK(passed IN (0,1))
details                TEXT              -- JSON
checked_at             TEXT NOT NULL
```

### 5.6 `audit_log`
Append-only record of every DB-mutating action. Replaces the v0.5 audit_log
sheet.

```
id                     INTEGER PRIMARY KEY AUTOINCREMENT
ts                     TEXT NOT NULL
actor                  TEXT NOT NULL     -- skill name + version
action                 TEXT NOT NULL     -- 'extraction_inserted' | 'source_document_inserted' | etc
target_table           TEXT NOT NULL
target_id              INTEGER
payload                TEXT              -- JSON of what changed
```

### 5.7 `golden_facts`
Hand-labeled regression baseline. Pinned to a specific filing by sha256 so
golden tests are reproducible across re-fetches.

```
id                     INTEGER PRIMARY KEY AUTOINCREMENT
ticker                 TEXT NOT NULL
period                 TEXT NOT NULL
metric_key             TEXT NOT NULL
expected_value         REAL
expected_unit          TEXT NOT NULL
source_doc_sha256      TEXT NOT NULL
note                   TEXT
UNIQUE(ticker, period, metric_key)
```

### 5.8 `schema_version`
Single-row table tracking migration version. Used by the migrator to refuse
running against the wrong schema.

```
version                INTEGER PRIMARY KEY
applied_at             TEXT NOT NULL
```

### Deliberately NOT in v0.1
- `extraction_runs` (batch run metadata) — YAGNI until we batch.
- Multi-writer locking — single writer assumed.
- Triangulation tables — handled at query time with SQL when we have multiple docs covering the same metric.
- User accounts.

---

## 6. New directory layout

```
.
├── .github/workflows/
├── data/
│   ├── _sources/                       # immutable source archive (unchanged)
│   │   ├── _identity.yaml              # company identity, authoritative
│   │   ├── _organizer_log.csv
│   │   └── <TICKER>/
│   │       ├── _raw/
│   │       └── <YYYY>/
│   ├── db/                             # NEW
│   │   ├── capex.db                    # binary, committed
│   │   └── dump.sql                    # auto-generated, committed, diff-friendly
│   └── seeds/                          # NEW
│       └── metric_definitions.yaml     # seed for the metric_definitions table
├── docs/
│   ├── SYSTEM_DESIGN.md                # rewritten Phase 1
│   ├── RESTRUCTURING_PLAN.md           # this file
│   └── neocloud_capex_tracker_design_memo.pdf  # historical, marked superseded
├── skills/
│   ├── fetch-company-report/
│   ├── organize-sources/
│   ├── read-and-extract/               # NEW
│   ├── query-line-item/                # NEW
│   └── export-workbook/                # NEW (Phase 4)
├── src/
│   └── capex/                          # importable package, replaces loose src/ modules
│       ├── __init__.py
│       ├── fetch/
│       │   ├── sec.py
│       │   ├── hkex.py
│       │   └── sidecar.py
│       ├── organize/
│       │   ├── namer.py
│       │   └── walker.py
│       ├── read/
│       │   ├── pdf.py
│       │   └── pages.py
│       ├── extract/
│       │   ├── extractor.py
│       │   └── prompts/
│       ├── adapters/
│       │   ├── base.py
│       │   └── anthropic.py
│       ├── protocol/
│       │   └── v0_1_0.py
│       ├── db/
│       │   ├── schema.py
│       │   ├── migrations/
│       │   │   └── 0001_init.sql
│       │   ├── queries.py
│       │   └── dump.py
│       ├── query/
│       │   └── line_items.py
│       ├── validation/
│       │   ├── provenance.py
│       │   └── consistency.py
│       ├── exporters/
│       │   ├── excel.py
│       │   ├── csv.py
│       │   └── json.py
│       └── cli/
│           └── main.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── golden/
│   └── fixtures/
└── pyproject.toml
```

The old loose `src/<layer>/*.py` files get deleted in Phase 1 — their contents
were placeholder stubs and the new layout supersedes them.

---

## 7. Phased execution plan

Each phase is a coherent commit set. Stop after Phase 3 for review before
proceeding to Phase 4.

### Phase 1 — Foundation: memo + DB layer ✅ COMPLETE 2026-04-09

- [x] 1.1 Rewrite `docs/SYSTEM_DESIGN.md` with the six-layer model, mark PDF historical
- [x] 1.2 Rewrite `README.md` to match new architecture
- [x] 1.3 Restructure `src/` → `src/capex/` package; delete old stub modules
- [x] 1.4 Update `pyproject.toml` to expose `capex` as the package
- [x] 1.5 Create `src/capex/db/migrations/0001_init.sql` with the schema in §5
- [x] 1.6 Implement `src/capex/db/schema.py` (migrator, version tracking)
- [x] 1.7 Implement `src/capex/db/dump.py` (binary → SQL dump after every mutation)
- [x] 1.8 Wire post-mutation hook so `dump.sql` regenerates automatically (via `Database.mutating()`)
- [x] 1.9 Run migrator, generate empty `data/db/capex.db` + `data/db/dump.sql`
- [x] 1.10 Implement `companies` sync function (YAML → DB mirror)
- [x] 1.11 Create `data/seeds/metric_definitions.yaml` with `capital_expenditures` and 4 related items
- [x] 1.12 Implement `metric_definitions` sync function (YAML → DB)

**Phase 1 deliverables:**
- `src/capex/` package with skeleton subpackages for all six layers
- `src/capex/db/{schema.py, dump.py, sync.py}` + `migrations/0001_init.sql`
- `src/capex/cli/main.py` with `db migrate` / `db sync-companies` / `db sync-metrics` / `db sync-all` subcommands
- `data/db/capex.db` (90 KB) + `data/db/dump.sql` (111 lines, diffable)
- `data/seeds/metric_definitions.yaml` seeded with 5 metrics (capital_expenditures, depreciation_amortization, property_plant_equipment_net, revenue, operating_cash_flow)
- `tests/test_smoke.py` updated to test imports + migrator + dump
- Verified end-to-end: `capex db sync-all` produces 1 company + 5 metrics + 2 audit entries; idempotent on re-run

### Phase 2 — Source acquisition pipeline (split: 2a SEC then 2b HKEX)

**Decisions ratified 2026-04-09 before Phase 2 starts:**

| ID | Question | Decision |
|---|---|---|
| D1 | HKEX in Phase 2 or defer? | **Split.** Phase 2a = SEC EDGAR only (12 of 13 names). Phase 2b = HKEX + dual-listed dispatcher. Tencent dark between 2a and 2b — acceptable. |
| D2 | HTML→PDF rendering or store HTML as-is? | **Neither — store the raw bytes the regulator served**, no conversion either direction. We do not need PDF rendering. Locators are *section + verbatim quote* (not page numbers), e.g. `"Item 8 - Balance Sheet table, row 'Total Debt'"`. Quotes must be **verbatim from the source text** so a human can ctrl-F them in the original document. The Playwright dependency is dropped from the plan entirely. |
| D3 | Foreign filers and quarterly cadence | **Timeliness wins.** For dual-listed companies, the dispatcher checks all available regulators for the requested form_type and picks **whichever has the most recently filed matching document**. Annual reports usually mean SEC 20-F. Quarterly disclosures may come earlier on HKEX (e.g. BABA HK-IR before SEC 20-F) — in that case use HKEX. The dispatcher logic lives in Phase 2b because it needs the HKEX fetcher to be wired. |
| D4 | SEC User-Agent contact | `f.kai.ye03@gmail.com`. Default UA = `"neocloud-capex-tracker f.kai.ye03@gmail.com"`. Override via `CAPEX_FETCHER_UA` env var. |
| D5 | Sidecar JSON: keep or kill? | **Keep.** Two-source redundancy: DB row is the queryable index, sidecar is the immutable on-disk archival truth. Sidecar survives DB corruption and enables a recovery sweep. |

#### Phase 2a — SEC fetch + organize wiring (SEC EDGAR only) ✅ COMPLETE 2026-04-10

- [x] 2a.1 Slim `skills/fetch-company-report/SKILL.md` to contract-only. Dropped HTML→PDF rendering section.
- [x] 2a.2 Slim `skills/organize-sources/SKILL.md` to contract-only. Updated for both `.htm` and `.pdf`.
- [x] 2a.3 Implement `src/capex/fetch/sidecar.py` (atomic JSON writer + validator + reader)
- [x] 2a.4 Implement `src/capex/fetch/sec.py` end-to-end (stdlib only — urllib.request, no requests dep)
- [x] 2a.5 Implement `src/capex/fetch/dispatcher.py` — also added `src/capex/fetch/errors.py` for the shared error vocabulary
- [x] 2a.6 Wire fetch → `source_documents` INSERT + `audit_log` row in one `mutating()` block (bundled into dispatcher.fetch_and_record)
- [x] 2a.7 Implement `src/capex/organize/namer.py` — pure functions for period derivation and canonical naming
- [x] 2a.8 Implement `src/capex/organize/walker.py` — sweep, atomic copy, hash verification, organizer log
- [x] 2a.9 Wire organize → `canonical_path` UPDATE + `audit_log` row (bundled into walker)
- [x] 2a.10 CLI subcommands: `capex fetch <TICKER> <FORM>` and `capex organize [--ticker T] [--dry-run]`
- [x] 2a.11 `CAPEX_FETCHER_UA` env var + default `"neocloud-capex-tracker f.kai.ye03@gmail.com"` in `src/capex/fetch/__init__.py`
- [x] 2a.12 Period derivation unit tests — 21 cases covering MSFT/ORCL/APLD/IREN/BABA non-Dec FYEs, calendar-year baselines, HKEX H1/H2, Q4 raises, unknown form raises. All passing.
- [x] 2a.13 `tests/conftest.py` with `@pytest.mark.network` marker — skipped unless `RUN_NETWORK_TESTS=1`
- [x] 2a.14 **Vertical test (manual):** PASSED. `capex fetch MSFT 10-K` downloaded the real Microsoft FY2025 10-K (7.8 MB Inline XBRL HTML, accession `0000950170-25-100235`), wrote sidecar, inserted `source_documents` row id=1. `capex organize` copied to `data/_sources/MSFT/2025/[30.07.2025][MSFT][AR][10-K].htm`, populated `canonical_path`, appended to `_organizer_log.csv`. Idempotent on re-run (second fetch = "already in DB", second organize = "skipped_already_canonical"). `dump.sql` regenerated with all changes. Audit log captured `source_document_inserted` + `canonical_path_set`.

#### Phase 2b — HKEX fetcher (HK-AR + HK-IR) ✅ COMPLETE 2026-04-10

**Scope decision 2026-04-10:** HKEX does NOT mandate quarterly reports. HK-listed companies publish Annual Reports (mandatory) and Interim Reports (half-yearly, mandatory). Some voluntarily publish quarterly results announcements, but these are condensed press releases filed as "Announcements" on HKEXnews — too shallow for capex footnote analysis. We implement HK-AR and HK-IR only. Quarterly press releases are parked until the coverage gap becomes load-bearing. This means Tencent gets 2 data points/year vs SEC filers' 4.

Golden test fixtures (2b.2 from original plan) are parked — revisit when HKEXnews scraper stability becomes a concern.

**HKEXnews approach (discovered during implementation):** HKEXnews has no clean REST API. Its search is a JSF application that's impractical to scrape via stdlib. Instead, `hkex.py` uses the paginated JSON feed at `lcisehk1relsdc_{page}.json` (~500 filings/page, ~9 pages covering ~2 years). Scans by stock code + tier-2 category code (40100=Annual, 40200=Interim). This works perfectly for the "monitor new filings" use case. Historical filings older than ~2 years may not be found — the fetcher raises `FilingNotFoundError` with a clear message.

- [x] 2b.1 Implement `src/capex/fetch/hkex.py` — JSON feed scanner + PDF downloader. Stdlib only. Supports English variant auto-detection (`_c.pdf` → tries `_e.pdf`). Period-of-report derived from filing title using regex (e.g. "2025 年報" → 2025-12-31).
- [x] 2b.2 Updated `_identity.yaml`: BABA → `hkex_stock_code: "9988"`, BIDU → `"9888"`, GDS → `"9698"`. Dual-listed names now carry both `edgar_cik` and `hkex_stock_code`.
- [x] 2b.3 Updated dispatcher: form-type-based routing. SEC forms (10-K/10-Q/20-F) → `sec.py`; HKEX forms (HK-AR/HK-IR) → `hkex.py`. Dual-listed companies can use either fetcher depending on the form_type requested.
- [x] 2b.4 Ran `capex db sync-companies` — 13 companies synced with new `hkex_stock_code` fields.
- [x] 2b.5 **Vertical test HK-AR: PASSED.** `capex fetch 0700 HK-AR` downloaded Tencent's 2025 Annual Report (4.3 MB PDF, filed 2026-04-09 — literally yesterday!), sidecar written, DB row id=13. `capex organize` produced `data/_sources/0700/2025/[09.04.2026][0700][AR][HK-AR].pdf`. Canonical path populated. Idempotent on re-run.
- [x] 2b.6 **Vertical test HK-IR: expected failure.** `capex fetch 0700 HK-IR` raised `FilingNotFoundError` — Tencent's latest interim report (~Aug 2025) is older than the JSON feed's coverage window. This is the known limitation of the feed-based approach. The monitor use case (catching new filings within hours of publication) works; deep historical lookups do not. Acceptable for v1.

### Phase 3 — Read + extract + query (the user-facing slice)

**Extraction engine decision 2026-04-10:** v1 uses **Claude Code itself** as the
extraction engine (runs on the user's Max Pro subscription — $0 incremental
cost). When the `read-and-extract` skill is invoked, Claude Code reads the
filing sections, extracts metrics per the prompt template, and calls
`src/capex/extract/writer.py` to persist results. No separate Anthropic API key
is needed for v1.

> ⚠️ **ADAPTER MIGRATION PATH (Phase 3.5):** The extraction layer is currently
> coupled to Claude Code's conversational runtime. This means extraction only
> works interactively — not in CI, cron, or headless pipelines. When automated
> or multi-model extraction is needed, implement `src/capex/adapters/` with:
>
> - `base.py` — model-agnostic `ModelBackend` protocol: `extract(system, user, response_schema) → str`
> - `anthropic.py` — calls `anthropic.Client().messages.create()` with `ANTHROPIC_API_KEY` env var
> - `google.py`, `openai.py`, etc. — future adapters for model comparison
>
> The `writer.py` layer is already adapter-agnostic: it accepts structured
> dicts and writes DB rows regardless of which model produced them. Swapping
> from Claude Code to a programmatic adapter is a **caller change only** —
> the skill invokes the adapter instead of relying on Claude Code's implicit
> LLM. The writer, the DB schema, the validation pipeline, and the prompt
> templates stay exactly the same. See `src/capex/adapters/README.md`.

**Metric scope for v1:** headline metrics only (the 5 seed metrics). Later
phases will add:
- **AI-attributable capex isolation** — reading footnotes/MD&A to split total
  capex into AI infrastructure vs other. Requires a more nuanced prompt and
  narrative reasoning.
- **Segment revenue/cost** — cloud/datacenter segment revenue from segment
  reporting disclosures (e.g. MSFT "Intelligent Cloud", AMZN "AWS", GOOGL
  "Google Cloud"). Requires identifying the right segment table and mapping
  company-specific segment names to a canonical taxonomy.

**Section scope for extraction:** Items 7, 8, Notes, plus debt/commitments
sections when present. Specifically:
- Item 7 (MD&A) — capex discussion, forward guidance, AI attribution narrative
- Item 8 (Financial Statements) — cash flow statement, balance sheet, income statement
- Notes to Financial Statements — capex breakdown, PP&E detail, lease commitments
- Contractual obligations / purchase commitments — committed datacenter contracts
- For HKEX PDFs: Management Discussion, Consolidated Cash Flow, Notes to Accounts

#### Phase 3 steps

- [x] 3.1 Write `skills/read-and-extract/SKILL.md` (contract-only)
- [x] 3.2 Implement `src/capex/read/text.py` — HTML text extraction (SEC Inline XBRL) + PDF stub. Tables preserved as [TABLE]/[/TABLE] markers.
- [x] 3.3 Implement `src/capex/read/sections.py` — SEC Item boundary parser (handles duplicate headers from TOC vs body by only breaking on Item NUMBER changes). HKEX pattern matcher for section headings.
- [x] 3.4 Implement `src/capex/protocol/v0_1_0.py` — stdlib dataclasses (not Pydantic in v1). `ExtractionResult`, `ExtractionBatch`, `validate_result()`.
- [x] 3.5 Implement `src/capex/extract/writer.py` — adapter-agnostic DB writer. Idempotent on `(source_document_id, metric_key, extracting_model)`.
- [x] 3.6 Implement `src/capex/extract/prompts/v0_1_headline.txt` — versioned prompt template for the 5 seed metrics.
- [x] 3.7 Create `src/capex/adapters/README.md` — migration-path doc for adding alternative model backends.
- [x] 3.8 Stub `src/capex/adapters/base.py` — `ModelBackend` protocol class.
- [x] 3.9 Wire `capex extract <TICKER>` CLI dry-run subcommand (shows sections + token estimate + metrics list).
- [x] 3.10 Implement `src/capex/validation/xbrl_anchor.py` — hits SEC companyfacts API (stdlib HTTP), compares LLM value to XBRL value.
- [x] 3.11 XBRL anchor is **opt-in, not auto-triggered**. Rationale (2026-04-10): the pipeline will extract non-standard items (AI-capex, segment revenue) that have no XBRL concepts. Auto-triggering would produce noisy "concept not found" results. The check is available via `run_xbrl_checks()` and `write_xbrl_results()` for manual or batch use. edgartools is NOT a production dependency — it was used for a one-time development cross-check and confirmed our MSFT values are exact matches.
- [x] 3.12 Added `xbrl_concept` as XBRL concept tags in `metric_definitions.yaml` aliases for all 5 seed metrics.
- [x] 3.13 Write `skills/query-line-item/SKILL.md` (contract-only)
- [x] 3.14 Implement `src/capex/query/line_items.py` — metric resolution via aliases, cache lookup, provenance formatting.
- [x] 3.15 **Vertical test PASSED.** MSFT FY2025: 5 headline metrics extracted by Claude Code, written to DB. XBRL anchor validated capex ($64,551M) and PP&E ($204,966M) at 0.0% diff. Revenue ($281,724M), OCF ($136,162M), D&A ($34,153M) all exact matches against edgartools XBRL cross-check. Query skill returns cached results with provenance.

#### Phase 3.6 — Currency normalization (pre-Phase 4 requirement)

**Decision 2026-04-10:** non-USD reporters (BABA, BIDU, GDS in CNY; Tencent
in CNY) need FX conversion for like-for-like comparison. All extracted values
are stored in local currency (`value`) AND USD-normalized (`value_usd`) with
the FX rate + date recorded alongside. USD companies get `fx_rate=1.0`.

FX rate source: **frankfurter.app** — free, no API key, open-source wrapper
around ECB data, supports historical rates back to 1999. Simple REST:
`GET https://api.frankfurter.app/2025-06-30?from=CNY&to=USD`

Companies requiring FX conversion:
- BABA, BIDU, GDS, 0700 → CNY/RMB (Chinese yuan)
- All others → USD (no conversion, fx_rate=1.0)
- IREN: Australian company but reports in USD on NASDAQ

- [ ] 3.6.1 Create `src/capex/db/migrations/0002_fx_normalization.sql` — add `reporting_currency` to `companies`, add `fx_rates` table, add `value_usd`/`fx_rate`/`fx_rate_date`/`reporting_currency` to `extractions`.
- [ ] 3.6.2 Update `_identity.yaml` — add `reporting_currency` for non-USD companies (BABA/BIDU/GDS/0700 → CNY).
- [ ] 3.6.3 Update `src/capex/db/sync.py` — handle new `reporting_currency` field in company sync.
- [ ] 3.6.4 Implement `src/capex/fx/rates.py` — fetch period-end FX rates from frankfurter.app, cache in `fx_rates` table. Stdlib HTTP only.
- [ ] 3.6.5 Update `src/capex/extract/writer.py` — after inserting an extraction, look up the company's reporting_currency. If non-USD, fetch the FX rate for `period_of_report` date, compute `value_usd = value * fx_rate`, write both to the row.
- [ ] 3.6.6 Update `src/capex/query/line_items.py` — return `value_usd` alongside `value` in query results. Display both when currencies differ.
- [ ] 3.6.7 **Verify:** extract a metric for a CNY company (e.g. run dry-run against BABA 20-F), confirm FX rate lookup works and value_usd is populated correctly.

#### Phase 3.5 — Adapter migration + advanced metrics (deferred)

These steps are NOT part of the v1 vertical slice. They land when automated
extraction or multi-model comparison becomes necessary.

- [ ] 3.5.1 Implement `src/capex/adapters/anthropic.py` — `anthropic.Client().messages.create()` with `ANTHROPIC_API_KEY` env var, structured output via tool_use, reads prompt template from `extract/prompts/`.
- [ ] 3.5.2 Implement `src/capex/extract/extractor.py` — headless orchestrator that calls an adapter programmatically (no Claude Code in the loop). Reads sections, formats prompt, calls adapter, parses response, hands to writer.py.
- [ ] 3.5.3 Wire the `capex extract <TICKER>` CLI to call the headless extractor instead of printing dry-run output. Requires `ANTHROPIC_API_KEY` to be set.
- [ ] 3.5.4 Add AI-attributable capex prompt — reads MD&A and footnotes to isolate the AI infrastructure fraction of total capex. Writes to a new `metric_key` (e.g. `ai_capex_estimate`) with `extraction_type = 'inferred'`.
- [ ] 3.5.5 Add segment revenue/cost extraction — identify cloud/datacenter segment tables (MSFT "Intelligent Cloud", AMZN "AWS", GOOGL "Google Cloud", META "Family of Apps" vs "Reality Labs"), extract segment revenue. Requires a per-company segment-name mapping in `metric_definitions.yaml` or a separate config.
- [ ] 3.5.6 Add adapters for other models: `google.py` (Gemini), `openai.py` (GPT-4o), etc. Wire model selection via `CAPEX_EXTRACT_MODEL` env var.

### Phase 4 — Data quality + Excel export

- [ ] 4.1 Fix XBRL concept gaps: add alternative XBRL concepts for META (revenue 2018+), GOOGL (2022 gap). Filter NBIS to FY2024+ (post-Yandex restructuring). Re-run affected companies.
- [ ] 4.2 Implement de-cumulation in query/export layer — 10-Q values are cumulative YTD in DB; export computes standalone quarterly = YTD(Qn) - YTD(Qn-1). Balance sheet items (PP&E) are point-in-time and not de-cumulated.
- [ ] 4.3 Implement `src/capex/validation/consistency.py` — sanity rules across all extractions: capex > 0, revenue > 0, capex < total_assets, OCF reasonable range. Flag violations, don't block.
- [ ] 4.4 Implement `src/capex/exporters/excel.py` — read DB → openpyxl workbook with these sheets:
  - **Revenue (Annual)** — one row per company, one column per fiscal year. USD-normalized.
  - **Revenue (Quarterly)** — one row per company, one column per quarter. De-cumulated standalone values.
  - **Capex (Annual)** — same layout as Revenue Annual. The headline deliverable.
  - **Capex (Quarterly)** — de-cumulated quarterly standalone capex.
  - **All Metrics (Annual)** — company × metric × year pivot. Full annual dataset.
  - **All Metrics (Quarterly)** — same but quarterly granularity.
  - **Data Quality** — flags: META gaps, NBIS pre-restructuring, concept mismatches, missing periods.
  - **Metadata** — extraction timestamps, XBRL concepts used, FX rates, coverage dates, methodology notes.
- [ ] 4.5 Add `capex export` CLI subcommand — generates the workbook to `workbook/capex_tracker.xlsx`.
- [ ] 4.6 Update `.github/workflows/ci.yml` — install openpyxl, run full test suite including new tests.

### Phase 5 — End-to-end hardening

- [ ] 5.1 Automated integration test for the Phase 3 vertical slice
- [ ] 5.2 Golden-set seed for MSFT FY2025 capex
- [ ] 5.3 Update `SYSTEM_DESIGN.md` with anything learned during implementation
- [ ] 5.4 Archive this `RESTRUCTURING_PLAN.md` once all checkboxes are flipped

---

## 8. Open questions / pending decisions

**Resolved 2026-04-09 (recorded in Phase 2 decisions table above):**

- ~~Page-id stability across re-fetches~~ → Resolved by D2: no rendering, no page numbers. Locators are section + verbatim quote, both stable across re-fetches.
- ~~HKEX implementation timing~~ → Resolved by D1: Phase 2b.
- ~~Mirror URL / IR-site fallback policy~~ → Resolved 2026-04-09: regulator-only, identity-only YAML, no exceptions ever.
- ~~SEC User-Agent string~~ → Resolved by D4: `f.kai.ye03@gmail.com`, env-var override.

**Still open:**

- [ ] **Excel export shape (Phase 4):** one sheet per metric? one sheet per company? mirror the old `source_data` shape? Decide in Phase 4 kickoff.
- [ ] **Extraction prompt versioning strategy:** prompts pinned per `protocol_version`, or independently versioned with their own field? Decide in Phase 3 step 3.8.
- [ ] **Adapters beyond Anthropic:** Gemini? OpenAI? Decide once Phase 3 lands and we have measured cost per extraction.
- [ ] **Watcher layer:** the v0.5 design had a watcher polling EDGAR. Not in any phase yet. Add when fetch + organize + extract + query are all stable. Likely lands as a thin GitHub Actions cron job that calls `capex fetch` for each company in `_identity.yaml`.
- [ ] **6-K subtype filtering for foreign issuers:** when we eventually fetch BABA/BIDU/etc 6-K filings (post-Phase 2b), do we ingest all 6-Ks or filter to ones containing earnings press releases? 6-K is a heterogeneous form. Open until quarterly visibility for Chinese hyperscalers becomes load-bearing.
- [ ] **`extractions.locator_page` column:** kept nullable in the v0.1 schema. Phase 3 may drop it via migration 0002 once we confirm the section + quote locator strategy works. Don't drop early — keep optionality until we have at least 10 real extractions in the DB.
- [ ] **Section heading parsing across regulators:** SEC HTML 10-Ks use `Item 1`, `Item 7`, etc. HKEX PDFs use different conventions ("Management Discussion and Analysis", "Financial Statements", numbered notes). The locator_section field needs a normalized form that's consistent across regulators. Decide in Phase 3 when read/sections.py is implemented.

---

## 9. Things this plan deliberately does not do

- No multi-model routing in v1. The protocol supports it; the orchestration logic does not land until measured evidence justifies it.
- No web dashboard. DB-first makes one easy to add later, but v1 ships none.
- No real-time / intra-day tracking. Quarterly cadence only.
- No automated handling of 8-K, proxy statements, press releases, slide decks. Out of scope. (6-K is open — see Phase 2 decisions.)
- **No mirror URLs, no per-filing URL overrides, no IR-site fallbacks. Ever.** Decided 2026-04-09. `_identity.yaml` is identity-only: `(ticker → name, source, CIK or HK code, FYE month)`. The fetch skill is a monitor — given a ticker and form_type, it asks the regulator "what's the latest matching this?" and downloads from there. URLs come from the regulator at fetch time, never from `_identity.yaml`. If a filing isn't on SEC EDGAR or HKEXnews, we don't ingest it. If a future need forces a different source, that's a deliberate spec change with its own design pass, not an escape hatch in `_identity.yaml`.
- HKEX implementation timing is now an active Phase 2 decision (Tencent is in `_identity.yaml`). See Phase 2 brief. Options: Phase 2a SEC-only → Phase 2b HKEX, or single combined Phase 2. Tencent monitoring is dark until HKEX lands either way.
- **No third-party SEC EDGAR library as the primary fetcher or extractor.** Considered 2026-04-10. The standout option was [`dgunning/edgartools`](https://github.com/dgunning/edgartools) (~2k stars, MIT, very active) which offers both filing fetching and XBRL financial-data extraction via standardized accessors. Rejected as the primary path for two reasons: (1) **edgartools only pulls structured XBRL line items** — the actual differentiator for this project is reading the surrounding narrative (MD&A, footnotes, segment commentary) to isolate AI-attributable capex from total capex, which requires our own LLM-driven extraction with section + verbatim quote provenance. A library that gives us numbers without context misses the interesting work. (2) **Symmetry with HKEX.** No reputable HKEX library exists, so HKEX will be custom regardless. Building both fetchers ourselves keeps the codebase coherent (one mental model, one provenance pipeline, no context-switching between "library mental model" and "raw scraper mental model"). The cost is ~200 lines of SEC URL-handling code in Phase 2a, which is contained and well-understood. SEC's free XBRL companyfacts API is still used in Phase 3.10–3.12 as a numeric *validation anchor* — not as an extractor — accessed via stdlib HTTP without any library dependency.
