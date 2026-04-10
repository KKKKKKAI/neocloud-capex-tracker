---
name: fetch-company-report
description: Fetch the latest authoritative public filing (10-K, 10-Q, 20-F, HKEX annual or interim report) for a tracked company directly from the primary regulator (SEC EDGAR or HKEXnews), save the original bytes into the project's immutable `data/_sources/<TICKER>/_raw/` archive, write a sidecar JSON, and insert a `source_documents` row in the database. Use this skill whenever the user or another agent says anything like "fetch the latest Microsoft 10-K", "grab Nvidia's most recent quarterly", "pull the Tencent annual report", "download the 20-F for Alibaba", or asks to obtain, retrieve, ingest, or load a public-company financial filing from a primary source. Always prefer this skill over ad-hoc web scraping or general web search when the target is a formal SEC or HKEX filing — it is the only sanctioned way into the repo's source-of-truth archive.
---

# fetch-company-report

## Purpose

This skill is the single, reviewed entry point for bringing a public-company filing into the `neocloud-capex-tracker` repository. It exists because everything downstream — extraction, validation, the query skill — only trusts filings that came through a known, provenance-tracked path. If a file shows up in `data/_sources/` without a matching sidecar JSON and a `source_documents` DB row from this skill, downstream validators are allowed to reject it.

The skill does four things and nothing else:

1. Resolves a `(ticker, form_type)` request against the company identity table.
2. Asks the appropriate regulator "what's the latest filing matching this?" and downloads the original bytes.
3. Writes a sidecar JSON next to the file capturing everything needed to reproduce or verify the download.
4. Inserts a `source_documents` row in `data/db/capex.db` with full provenance.

It deliberately does **not** extract text, mark sections, rename files into the human-friendly canonical form, render HTML to PDF, or call any LLM. Those concerns belong to sibling skills (`organize-sources`, `read-and-extract`) so each piece can be tested, swapped, and reasoned about independently.

## When to trigger

Trigger when a user or calling agent asks for any of the following:

- Fetch / download / pull / grab / retrieve a named company's annual report, 10-K, 10-Q, 20-F, HKEX annual report, or HKEX interim report.
- "Get me the latest <company> filing" where <company> is a tracked ticker.
- A watcher pipeline signals that a new filing has appeared and needs to be archived.

Do **not** trigger for:

- 8-K, 6-K, proxy statements, press releases, earnings slide decks, investor day materials. Out of scope for v1.
- IR-website PDFs that are not on SEC or HKEX. We only trust primary regulators. There is no mirror or override mechanism — `_identity.yaml` is identity-only and does not record per-filing URLs.
- Companies not in the identity table. Raise `UnknownCompanyError` and ask the user to add the company first.

## Scope: sources

Only two regulators are supported, in strict priority order:

1. **SEC EDGAR** — for any company whose `companies.preferred_source` is `sec_edgar`. Covers US-listed names and dual-listed Chinese hyperscalers (BABA, BIDU, GDS) where a 20-F is filed. SEC is preferred for dual-listed annuals because EDGAR's API is more reliable.
2. **HKEXnews** — for HK-only filers (currently Tencent / 0700) and for dual-listed companies when HKEX has a more recent matching filing than SEC (timeliness rule, lands in Phase 2b alongside the dispatcher).

Anything outside these two is explicitly out of scope. If a future company forces expansion, that's a deliberate spec change with its own design pass, not an escape hatch in this skill.

## Inputs

```
{
  "ticker": "MSFT",       # required, must match a key in companies table
  "form_type": "10-K"     # required, one of: 10-K, 10-Q, 20-F, HK-AR, HK-IR
}
```

There is no `period` field. The skill is a **monitor**: given a ticker and form_type, it always fetches the most recent matching filing the regulator has published. To re-fetch a specific historical filing, that's a different operation that doesn't exist in v1.

## Outputs

On success, the skill returns the metadata of the inserted `source_documents` row and writes three things:

1. **The original regulator bytes** at `data/_sources/<TICKER>/_raw/<sanitized-filename>.<htm|pdf>` — exactly what the regulator served, no conversion. SEC 10-Ks/20-Fs are typically HTML; HKEX annual reports are PDFs.
2. **A sidecar JSON** at the same path with `.fetch.json` suffix appended to the stem.
3. **A `source_documents` row** in `data/db/capex.db` with the same metadata as the sidecar, plus an `audit_log` row recording the insertion.

The sidecar fields:

```json
{
  "raw_path": "data/_sources/MSFT/_raw/msft-20250630.htm",
  "sha256": "c4ae...",
  "source": "sec_edgar",
  "source_url": "https://www.sec.gov/Archives/edgar/data/789019/...",
  "accession_number": "0000950170-25-100235",
  "form_type": "10-K",
  "filing_date": "2025-07-30",
  "period_of_report": "2025-06-30",
  "ticker": "MSFT",
  "fetched_at": "2026-04-10T14:22:11Z",
  "fetcher_version": "0.1.0",
  "protocol_version": "0.1.0-draft"
}
```

`raw_path` is a repo-relative path with forward slashes. The DB row mirrors these fields plus a server-generated `id` and a derived `period_token` (`AR` / `Q1` / `Q2` / `Q3` / `H1` / `H2`) and `fiscal_year` (computed from `period_of_report` and the company's `fiscal_year_end_month`).

## Folder layout

```
data/_sources/
  <TICKER>/
    _raw/
      <sanitized>.<htm|pdf>     # original regulator bytes
      <sanitized>.<htm|pdf>.fetch.json
```

The `<TICKER>` directory is flat under `_raw/`; year-based subfolders and human-friendly filenames are created by `organize-sources`, not here. This skill does not create `<YYYY>/` directories.

The filename inside `_raw/` is whatever the regulator served, lightly sanitized:

- Lowercase.
- Replace anything outside `[a-z0-9._-]` with `-`.
- Collapse consecutive dashes.
- Preserve the original extension exactly as served.

If two fetches land on the same sanitized name but have different sha256 hashes, append `-<short-hash>` before the extension to disambiguate. If the sha256 matches something already in `_raw/` and the DB, treat it as a no-op and return the existing row — we don't rewrite history.

## Error vocabulary

All errors are exceptions raised by the implementation. Callers (the watcher, a human running the CLI, another skill) decide how to surface them.

- `UnknownCompanyError(ticker)` — ticker not in `companies` table.
- `FormTypeMismatchError(ticker, requested, supported)` — e.g. asked for 10-K on an HK-only company.
- `FilingNotFoundError(ticker, form_type)` — no matching filing exists yet.
- `SourceUnavailableError(source, http_status, message)` — EDGAR or HKEXnews returned a non-2xx.
- `SuspiciousFilingSizeError(path, size_bytes)` — size outside sane bounds (50 KB to 200 MB). Catches truncated downloads and runaway responses.
- `IntegrityError(expected_hash, actual_hash)` — hash mismatch between what was downloaded and what was committed.

## Implementation

Lives in `src/capex/fetch/`:

- `dispatcher.py` — entry point, looks up the company, picks the per-source fetcher.
- `sec.py` — SEC EDGAR fetcher, hits `data.sec.gov/submissions/CIK*.json`.
- `hkex.py` — HKEXnews fetcher, lands in Phase 2b.
- `sidecar.py` — atomic JSON writer/reader.

The User-Agent for SEC is read from the `CAPEX_FETCHER_UA` environment variable, defaulting to `"neocloud-capex-tracker f.kai.ye03@gmail.com"`. SEC requires a real contact email; override the env var in production deployments to identify yours.

## What this skill does NOT do

- No text extraction. No section parsing. No canonical-form filenames. No cross-filing validation. No LLM calls.
- No HTML→PDF rendering. We store the regulator's bytes as-is.
- No mirror URLs, no per-filing URL overrides, no IR-site fallbacks.
- No 8-K, 6-K, proxy, slide deck handling.
- No Slack/email/Jira surface for errors. Errors are exceptions; the caller decides.

Keep it small and boring. That's the point.
