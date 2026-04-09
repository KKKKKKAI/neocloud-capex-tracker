# System Design — neocloud-capex-tracker

**Status:** Draft shell derived from design memo v0.5.
**Authoritative source:** `docs/neocloud_capex_tracker_design_memo.pdf`.
**Last updated:** 2026-04-08

This document is the living system-design reference. When it disagrees with the
PDF memo, the more recent of the two wins. Any architectural change must be
reflected here before it lands in code.

---

## 1. Purpose

A collaborative project that automatically tracks, extracts, and evaluates
AI-related capital expenditure disclosures from major hyperscalers and neocloud
providers. Runs on a recurring schedule, ingests quarterly and annual filings
as they are released, routes work across multiple LLM providers under a strict
interchange protocol, and presents validated results in a shared Excel
workbook stored in version control.

## 2. Goals and non-goals

### Goals
- Track AI-attributable capex across major hyperscalers and pure-play neoclouds, quarterly cadence.
- Quantify both volume (raw spend) and efficiency (capex normalized to revenue, capacity, or output).
- Operate continuously and autonomously, triggered by the publication of new filings.
- Minimize token cost via model-agnostic routing under a universal interchange protocol.
- Provide a single human-readable artifact: an Excel workbook.
- Maintain a fully auditable trail: every extracted figure traceable to a specific quote in a specific source document.
- Friendly collaboration via GitHub: code, workbook, source archive, history all in one repository.

### Non-goals (v1)
- Real-time / intra-day tracking.
- Investment recommendations or financial advice.
- Coverage of private companies without quarterly disclosures.
- Hosted web dashboard.
- Multi-user concurrent editing of the workbook.

## 3. Guiding principles

- Provenance over plausibility.
- Mechanical checks before model checks.
- Single source of truth for validation logic, living in the Excel template as formulas.
- Model-agnostic interchange. Swapping models is a config change, not a refactor.
- Cheap watcher, expensive worker. LLM inference runs only when a new filing is detected.
- Defer features that aren't justified yet.

## 4. Architecture — nine layers

| # | Layer | Responsibility |
|---|---|---|
| 1 | Watcher | Scheduled cron job (GitHub Actions) polling SEC EDGAR + non-US IR pages. Detects new filings, emits events. Zero LLM cost. |
| 2 | Ingestion | Downloads filings, normalizes to canonical text, assigns stable page/section IDs, computes SHA-256 source hash. No LLM calls. |
| 3 | Extraction | Single model-agnostic `extract()` interface. Pluggable backends. Emits structured rows matching the `source_data` schema with full per-field provenance. |
| 4 | Workbook (template + live) | Hand-designed template defines structure, formulas, named ranges, validation rules. Live workbook derived from template and populated on every run. |
| 5 | Workbook write adapter | openpyxl-based Python module. Writes only into designated input cells of `source_data` and append-only rows of `audit_log`. |
| 6 | Formula evaluation pass | LibreOffice headless recalc between agent write and check-read. |
| 7 | Validation pipeline | Schema validator → provenance verifier → consistency rules → eval agent → cross-source triangulation. |
| 8 | Storage / distribution | GitHub repo (code, template, live workbook, CSV mirror, LFS-tracked filings). Actions concurrency groups enforce single-writer. |
| 9 | Optional human interface | Claude for Excel sidebar, ad-hoc analyst queries. Strictly outside the automated pipeline. |

## 5. Multi-model interchange protocol

Three concerns, tested separately:

### 5.1 Syntactic validity
Every output must parse against a versioned JSON schema (Pydantic). Hard gate.

### 5.2 Provenance / proof-of-work
Every extracted field carries:

- `value` — the fact
- `quote` — verbatim span, ≤ 30 words
- `locator` — canonical page ID from the ingestion layer (never model-counted)
- `source_doc_hash` — SHA-256
- `extraction_type` — one of `{direct, inferred, derived}`
- `confidence` — model-reported, advisory only
- `protocol_version` — pinned per output
- `extracting_model` — backend id + version

### 5.3 Semantic validity (six layers)

- **A. Schema tests** — unit tests over known-good / known-bad blobs
- **B. Golden-set regression** — hand-labeled fixtures, replayed on prompt/model changes
- **C. Provenance verification** — Python substring match of `quote` against source
- **D. Consistency rules** — declarative range/ratio/relational checks, as Excel formulas
- **E. Eval agent** — different model family, narrow yes/no audits
- **F. Cross-source triangulation** — formulas comparing 10-Q / press release / transcript

### 5.4 Known failure modes

- Fabricated quotes → Layer C substring verification
- Locator drift across models → ingestion-injected `<page id="...">` markers
- Eval agent shares biases with extractor → different model family required
- Provenance overhead > savings → measure tokens-per-extraction per model
- Silent regressions → golden-set CI gating

## 6. Workbook design

### 6.1 Sheet structure

- `source_data` — agent write target, one row per `(company, period, metric)` with provenance columns
- `checks` — formula-driven consistency checks per row, aggregate `all_checks_pass` boolean
- `derived_metrics` — formula-computed analytics (efficiency ratios, QoQ/YoY deltas)
- `golden` — hand-labeled 3-year baseline per company
- `audit_log` — append-only record of every agent write
- `schema` — self-describing protocol contract
- `dashboard` — read-only presentation pulling from `derived_metrics`

### 6.2 Write discipline

- openpyxl writes only into designated input cells. Formula sheets untouched.
- Excel Tables / structured references, not cell references.
- LibreOffice headless recalc between write and read.
- CSV mirror in `data/csv/` regenerated on every run.
- Sheet protection enabled on all formula sheets.

## 7. Repository and infrastructure

### 7.1 Layout

See `README.md` for the repo tree.

### 7.2 Why GitHub for both code and workbook

- Single source of truth across code, data, workbook, history.
- Every quarterly run is a commit → free git-tracked dataset history.
- Actions concurrency groups enforce single-writer.
- Free tier covers quarterly cadence.

### 7.3 Caveats handled

- Binary diffs useless → CSV mirror committed alongside every workbook write.
- Repo bloat → Git LFS for source PDFs; .xlsx stays in main repo until size warrants.
- Merge conflicts → workbook is single-writer.
- Secrets → GitHub Actions secrets only; nothing committed.

## 8. Rejected alternatives

### 8.1 Claude for Excel as an automated write interface
Interactive sidebar only, no REST API, no headless mode, no cron support. Verified via Anthropic support docs. Reclassified as an optional human interface.

### 8.2 Screen-control / computer-use automation of Claude for Excel
Technically feasible (Anthropic Computer Use API, pyautogui, Playwright, UiPath, Self-Operating Computer). Rejected for v1: high token cost, UI brittleness, always-on Windows VM burden, credential exposure to an autonomous agent, zero functional benefit vs openpyxl. May re-enter later as a watcher-layer tool for scraping non-US IR sites with no clean API.

### 8.3 Multi-model routing on day one
Deferred. Protocol is designed for it; routing logic will not land until measured evidence justifies the orchestration overhead.

## 9. Open items

- [ ] Golden dataset scope, 3-year baseline methodology, labeling protocol — dedicated session
- [ ] Excel template design vs openpyxl boundary — dedicated session
- [ ] Target company list for v1 — recommend starting with 3-4 US hyperscalers on SEC EDGAR
- [ ] `source_data` column schema — the protocol contract
- [ ] Efficiency metrics formal definitions
- [ ] Eval agent model family choice
- [ ] Blocking vs flagging on eval agent disagreement
- [ ] Git LFS for source PDFs (recommended yes)
- [ ] Live formula evaluation strategy — LibreOffice headless recalc (recommended) vs Python re-impl
- [ ] Collaboration protocol — branching model, review rules, ownership of layers

## 10. Next steps

1. Sign off on this memo (or annotate disagreements).
2. Define the collaboration protocol.
3. Lock the v1 target company list.
4. Draft the `source_data` column schema.
5. Excel template design session.
6. Golden dataset labeling session.
