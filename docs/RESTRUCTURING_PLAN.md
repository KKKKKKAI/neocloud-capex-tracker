# Restructuring Plan — v0.5 → v0.6

**Status:** Approved, not yet executed.
**Created:** 2026-04-09
**Owner:** @KKKKKKAI
**Supersedes:** parts of `SYSTEM_DESIGN.md` (will be rewritten in Phase 1).

This document is the working tracker for the v0.6 architectural restructuring.
It captures the decisions, the new architecture, the DB schema, and the phased
execution plan. Updated as work progresses; checkboxes flip to `[x]` as items
land. When the plan is fully executed, the contents move into
`SYSTEM_DESIGN.md` and this file is archived.

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

### Phase 1 — Foundation: memo + DB layer

- [ ] 1.1 Rewrite `docs/SYSTEM_DESIGN.md` with the six-layer model, mark PDF historical
- [ ] 1.2 Rewrite `README.md` to match new architecture
- [ ] 1.3 Restructure `src/` → `src/capex/` package; delete old stub modules
- [ ] 1.4 Update `pyproject.toml` to expose `capex` as the package
- [ ] 1.5 Create `src/capex/db/migrations/0001_init.sql` with the schema in §5
- [ ] 1.6 Implement `src/capex/db/schema.py` (migrator, version tracking)
- [ ] 1.7 Implement `src/capex/db/dump.py` (binary → SQL dump after every mutation)
- [ ] 1.8 Wire post-mutation hook so `dump.sql` regenerates automatically
- [ ] 1.9 Run migrator, generate empty `data/db/capex.db` + `data/db/dump.sql`
- [ ] 1.10 Implement `companies` sync function (YAML → DB mirror)
- [ ] 1.11 Create `data/seeds/metric_definitions.yaml` with `capital_expenditures` and 3-4 related items
- [ ] 1.12 Implement `metric_definitions` sync function (YAML → DB)

### Phase 2 — Skill ↔ src refactor + fetch/organize wiring

- [ ] 2.1 Slim `skills/fetch-company-report/SKILL.md` to contract-only
- [ ] 2.2 Slim `skills/organize-sources/SKILL.md` to contract-only
- [ ] 2.3 Implement `src/capex/fetch/sec.py` end-to-end for MSFT 10-K
- [ ] 2.4 Implement `src/capex/fetch/sidecar.py` (JSON sidecar writer)
- [ ] 2.5 Wire fetch success → `source_documents` insert + `audit_log` row
- [ ] 2.6 Implement `src/capex/organize/namer.py` (canonical filename grammar)
- [ ] 2.7 Implement `src/capex/organize/walker.py` (sweep `_raw/`, copy to canonical)
- [ ] 2.8 Wire organize success → update `canonical_path` on the existing `source_documents` row
- [ ] 2.9 Vertical test (manual): fetch MSFT 10-K → `_raw/` → organize → `2025/` → DB row exists with both paths populated

### Phase 3 — Read + query (the user-facing slice)

- [ ] 3.1 Write `skills/read-and-extract/SKILL.md` (contract-only)
- [ ] 3.2 Implement `src/capex/read/pdf.py` (PDF text extraction with stable page markers)
- [ ] 3.3 Implement `src/capex/read/pages.py` (page-id injection for the locator field)
- [ ] 3.4 Implement `src/capex/protocol/v0_1_0.py` (Pydantic models for the interchange schema)
- [ ] 3.5 Implement `src/capex/adapters/base.py` (model-agnostic interface)
- [ ] 3.6 Implement `src/capex/adapters/anthropic.py` (first concrete backend)
- [ ] 3.7 Implement `src/capex/extract/extractor.py` (orchestrates one-PDF-one-context extraction)
- [ ] 3.8 Implement `src/capex/extract/prompts/` (extraction prompts, versioned)
- [ ] 3.9 Wire read-and-extract → `extractions` insert + provenance check + `audit_log`
- [ ] 3.10 Write `skills/query-line-item/SKILL.md` (contract-only)
- [ ] 3.11 Implement `src/capex/query/line_items.py` (resolve question → check cache → call worker on miss → format response)
- [ ] 3.12 Vertical test: `query MSFT FY2025 capital_expenditures` → returns `{value, unit, quote, page, source_path, sha256}`, second call hits the cache

### Phase 4 — Validation hardening + Excel export (deferrable)

- [ ] 4.1 Implement `src/capex/validation/provenance.py` (substring match)
- [ ] 4.2 Implement `src/capex/validation/consistency.py` (range/ratio rules in Python)
- [ ] 4.3 Implement `src/capex/exporters/excel.py` (read DB → openpyxl workbook)
- [ ] 4.4 Update `.github/workflows/ci.yml` to test the new structure
- [ ] 4.5 Update `.github/workflows/watcher.yml` and `organize-sources.yml` to use new entrypoints

### Phase 5 — End-to-end hardening

- [ ] 5.1 Automated integration test for the Phase 3 vertical slice
- [ ] 5.2 Golden-set seed for MSFT FY2025 capex
- [ ] 5.3 Update `SYSTEM_DESIGN.md` with anything learned during implementation
- [ ] 5.4 Archive this `RESTRUCTURING_PLAN.md` once all checkboxes are flipped

---

## 8. Open questions / pending decisions

- [ ] **Excel export shape (Phase 4):** one sheet per metric? one sheet per company? mirror the old `source_data` shape? Decide in Phase 4 kickoff.
- [ ] **Extraction prompt versioning strategy:** prompts pinned per `protocol_version`, or independently versioned with their own field? Decide in Phase 3 step 3.8.
- [ ] **Page-id stability across re-fetches:** if a filing is re-fetched and the renderer version changes, the locator_page values may shift. Decide whether to re-extract or keep stale references. Likely re-extract on sha256 change.
- [ ] **Adapters beyond Anthropic:** Gemini? OpenAI? Decide once Phase 3 lands and we have measured cost per extraction.
- [ ] **Watcher layer:** the v0.5 design had a watcher polling EDGAR. Not in any phase yet. Add when fetch + organize + extract + query are all stable.

---

## 9. Things this plan deliberately does not do

- No multi-model routing in v1. The protocol supports it; the orchestration logic does not land until measured evidence justifies it.
- No web dashboard. DB-first makes one easy to add later, but v1 ships none.
- No real-time / intra-day tracking. Quarterly cadence only.
- No automated handling of 8-K, 6-K, proxy statements, press releases, slide decks. Out of scope.
- No HKEX implementation in Phase 2. SEC EDGAR only for the first vertical slice. HKEX adapter lands when the first HKEX-only company is added to `_identity.yaml`.
