---
name: read-and-extract
description: Read one source filing and extract structured financial metrics with full provenance. Use this skill when the user asks to "extract capex from the MSFT 10-K", "pull the headline numbers from this filing", "read the annual report and get the financial data", or any variation of extracting specific financial metrics from a single company filing. This skill processes ONE filing at a time to keep context manageable. It reads the relevant sections, extracts the requested metrics with verbatim quotes and section references, validates against XBRL where available, and writes the results to the database.
---

# read-and-extract

## Purpose

This skill is the extraction worker. Given a `source_documents` row (a filing
already fetched and organized by `fetch-company-report` + `organize-sources`),
it reads the relevant sections of the filing, extracts structured financial
metrics with full provenance, and writes the results to the `extractions`
table in the database.

It is the only part of the system that performs LLM-driven content analysis.
Everything upstream (fetch, organize) and downstream (query, export) is
deterministic Python.

## Extraction engine — v1 vs future

**v1 (current):** this skill runs inside **Claude Code**. When invoked, Claude
Code reads the filing sections, follows the prompt template, produces
structured JSON, and calls `src/capex/extract/writer.py` to persist the
results. The user's Claude subscription covers the cost. No separate API key
is needed.

**This means v1 extraction is interactive-only.** It requires a human in a
Claude Code session.

**Future (Phase 3.5+):** implement programmatic adapters in
`src/capex/adapters/` (Anthropic API, Google Gemini, OpenAI, etc.) so
extraction can run headlessly in CI or cron. The `writer.py` layer is already
adapter-agnostic — swapping the extraction engine is a caller change only.
See `src/capex/adapters/README.md` for the migration path.

## When to trigger

Trigger when the user or a calling agent asks to:

- Extract financial metrics from a specific company filing.
- "Read the MSFT 10-K and pull the capex numbers."
- "Extract headline metrics from BABA's 20-F."
- Process a newly fetched filing for the first time.
- Re-extract with updated prompts after a prompt version change.

Do **not** trigger for:

- Fetching or organizing filings — those are separate skills.
- Querying already-extracted data — use `query-line-item` instead (it checks
  the cache first and only invokes this skill on a miss).
- Batch extraction across all companies — invoke this skill once per filing.

## Context isolation

**One invocation = one filing = one context.** The parent agent never
accumulates multiple filings in a single context window. This prevents
context overflow on large filings (SEC 10-Ks are 7-8 MB HTML) and keeps
extraction quality high.

When extracting for multiple companies, the caller (human, query skill,
or a batch script) loops and invokes this skill once per filing.

## Inputs

```
{
  "ticker": "MSFT",
  "form_type": "10-K",           # optional — if omitted, uses the latest annual
  "metric_keys": null             # optional — if omitted, extracts all seed metrics
}
```

The skill resolves `(ticker, form_type)` to the most recent `source_documents`
row. If `metric_keys` is provided, only those metrics are extracted; otherwise
all metrics in `metric_definitions` are attempted.

## Outputs

For each successfully extracted metric, a row is inserted into `extractions`:

```json
{
  "source_document_id": 1,
  "metric_key": "capital_expenditures",
  "value": 88000,
  "value_text": "$88.0 billion",
  "unit": "USD_millions",
  "quote": "purchases of property and equipment were $88.0 billion",
  "locator_section": "Item 8 - Consolidated Statements of Cash Flows",
  "locator_page": null,
  "extraction_type": "direct",
  "confidence": null,
  "extracting_model": "claude-code",
  "protocol_version": "0.1.0-draft"
}
```

Key provenance fields:
- `quote` — verbatim span (≤30 words) from the source. **Must be exactly
  ctrl-F-able** in the original filing. If the human opens the HTML/PDF and
  searches for this text, they must find it.
- `locator_section` — human-navigable section reference, e.g.
  `"Item 8 - Consolidated Statements of Cash Flows, line 'Purchases of property and equipment'"`.
- `extraction_type` — `direct` (value on a labeled line), `inferred` (derived
  from narrative context), or `derived` (computed from other extracted values).

The skill also writes:
- `validation_results` rows for provenance checks and XBRL anchor matches.
- An `audit_log` row recording the extraction event.

Returns a summary: `{extracted: N, skipped: N, errors: []}`.

## Section scope

The skill reads only the relevant sections, not the entire filing:

**SEC 10-K / 10-Q / 20-F:**
- Item 7 (MD&A) — capex discussion, forward guidance, AI-attribution narrative
- Item 8 (Financial Statements) — cash flow statement, balance sheet, income statement
- Notes to Financial Statements — capex breakdown, PP&E detail
- Contractual obligations / purchase commitments — committed datacenter contracts

**HKEX HK-AR / HK-IR (PDF):**
- Management Discussion and Analysis
- Consolidated Statement of Cash Flows
- Notes to the Consolidated Financial Statements

## Metric scope (v1)

The 5 seed metrics from `data/seeds/metric_definitions.yaml`:

| metric_key | What to look for |
|---|---|
| `capital_expenditures` | Cash flow statement: "purchases of property and equipment" or equivalent |
| `revenue` | Income statement: top-line revenue |
| `operating_cash_flow` | Cash flow statement: "net cash from operating activities" |
| `depreciation_amortization` | Cash flow statement or income statement: D&A line |
| `property_plant_equipment_net` | Balance sheet: PP&E net of accumulated depreciation |

Future metrics (Phase 3.5+): AI-attributable capex, segment revenue
(cloud/datacenter), committed contracts.

## Restated comparatives — guardrails

When a filing's target table shows prior-period comparative columns
(standard for 10-K / 20-F segment tables and 10-Q quarterly tables),
extract one row **per comparative period** alongside the primary. Each
becomes its own extraction row: primary writes against the filing's
`source_documents` row; each comparative writes against a virtual
`source_documents` row via `ensure_restated_source_doc` so downstream
selectors (ordered by `filing_date DESC`) promote the latest-filed
value automatically.

Two guardrails are mandatory:

1. **Zero-fallback**: if the verified value for a comparative period is
   `0` or `None`, **skip the row entirely**. It's almost always an LLM
   mis-read of an empty cell — writing it would let a bogus 0 overwrite
   a valid original via the `filing_date DESC` tiebreak. Primary
   periods with value 0 are still allowed (could be a real zero) but
   will be demoted at selector time if a non-zero row exists.
2. **FYE-aware fiscal year**: derive the comparative's `fiscal_year`
   from `period_of_report + fiscal_year_end_month`, never from the
   calendar year of the period-end date. MSFT (FYE=June) Q2 ending
   `2024-12-31` is FY2025, not FY2024. Use the
   `_fiscal_year_from(period, fye)` helper in
   `src/capex/extract/extractors/llm_headless.py`.

See `docs/RESTATEMENT_POLICY.md` for the full design.

## Error vocabulary

- `DocumentNotFoundError` — no `source_documents` row for the requested (ticker, form_type).
- `SectionParseError` — couldn't identify the required sections in the filing.
- `ExtractionError` — the LLM produced unparseable or invalid output.
- `ProvenanceError` — the verbatim quote was not found in the source text (fabricated quote).

Errors are collected per-metric and returned in the summary's `errors` array.
One failed metric should not prevent the other 4 from being extracted.

## Implementation

- `src/capex/read/text.py` — text extraction from HTML or PDF
- `src/capex/read/sections.py` — section boundary detection and tree building
- `src/capex/extract/prompts/` — versioned prompt templates
- `src/capex/extract/writer.py` — adapter-agnostic DB writer
- `src/capex/protocol/v0_1_0.py` — Pydantic models for the extraction contract

## What this skill does NOT do

- No fetching or organizing — those are upstream skills.
- No Excel or CSV export — that's `export-workbook`.
- No cross-filing comparison — that's the query skill or a future triangulation layer.
- No model selection or routing — v1 uses Claude Code; the adapter layer (Phase 3.5) handles model choice.
