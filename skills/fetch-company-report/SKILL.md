---
name: fetch-company-report
description: Fetch an authoritative public filing (10-K, 10-Q, 20-F, HKEX annual or interim report) for a given company directly from the primary regulator (SEC EDGAR or HKEXnews), save the PDF into the project's immutable `_raw/` archive, and return a file path plus provenance metadata for downstream agents. Use this skill whenever the user or another agent says anything like "fetch the latest Microsoft 10-K", "grab Nvidia's most recent quarterly", "pull the Tencent annual report", "download the 20-F for Alibaba", or asks to obtain, retrieve, ingest, or load a public-company financial filing from a primary source. Always prefer this skill over ad-hoc web scraping or general web search when the target is a formal SEC or HKEX filing — it is the only sanctioned way into the repo's source-of-truth archive.
---

# fetch-company-report

## Purpose

This skill is the single, reviewed entry point for bringing a public-company filing into the `neocloud-capex-tracker` repository. It exists because everything downstream — extraction, validation, triangulation, the Excel workbook, the published dashboard — only trusts filings that came through a known, provenance-tracked path. If a PDF shows up in `data/_sources/` without a matching sidecar JSON from this skill, downstream validators are allowed to reject it.

The skill does three things and nothing else:

1. Resolves a `(ticker, form_type, period)` request against the company identity table and the correct regulator.
2. Downloads the authoritative PDF (or renders HTML→PDF deterministically if the regulator only serves HTML) into `data/_sources/<TICKER>/_raw/`.
3. Writes a sidecar JSON capturing everything a human or a later agent would need to reproduce or verify the download.

It deliberately does **not** extract text, mark pages, rename files into the human-friendly canonical form, or call any LLM. Those concerns belong to sibling skills (`canonicalize-filing-pdf`, `organize-sources`) so that each piece can be tested, swapped, and reasoned about independently.

## When to trigger

Trigger when a user or calling agent asks for any of the following:

- Fetch / download / pull / grab / retrieve a named company's annual report, 10-K, 10-Q, 20-F, HKEX annual report, or HKEX interim report.
- "Get me the latest <company> filing" where <company> is a tracked ticker.
- A watcher pipeline signals that a new filing has appeared and needs to be archived.

Do **not** trigger for:

- 8-K, 6-K, proxy statements, press releases, earnings slide decks, investor day materials. These are out of scope for v1 — politely decline and explain.
- IR-website PDFs that are not mirrored on SEC or HKEX. We only trust primary regulators.
- Companies not in the identity table. Raise `UnknownCompanyError` and ask the user to add the company first.

## Scope: sources

Only two sources are supported, in strict priority order:

1. **SEC EDGAR** — for any company whose identity table entry has `preferred_source: sec_edgar`. This covers US-listed names (MSFT, NVDA, GOOGL, META, AMZN, ORCL, etc.) and all dual-listed Chinese hyperscalers where a 20-F is filed (BABA, JD, BIDU, NTES, etc.). SEC is always preferred when both a US and an HK listing exist — the EDGAR API is more stable, the document structure is more uniform, and 20-F disclosures are aligned with the rest of the dataset.
2. **HKEXnews** — for HK-primary companies with no SEC listing. Today that mainly means 0700 (Tencent) and anything else the user adds as `preferred_source: hkex`.

Anything outside these two (UK NSM, ESMA OAMs, CNINFO, IR sites, web search) is explicitly out of scope. If a future company forces us to expand, that's a deliberate spec change, not an ad-hoc escape hatch inside this skill.

## Inputs

The skill accepts a single request object:

```
{
  "ticker": "MSFT",           # required, must match identity table key
  "form_type": "10-K",        # required, one of: 10-K, 10-Q, 20-F, HK-AR, HK-IR
  "period": null              # optional; null means "most recent"
}
```

`period`, when provided, is a hint the resolver uses to disambiguate if the company filed more than one matching document. Acceptable forms:

- A fiscal period label: `"FY2025"`, `"Q1-FY2026"`, `"H1-2025"`.
- An ISO period-of-report date: `"2025-06-30"`.

If `period` is omitted, fetch the single most recent filing matching `form_type`.

## Outputs

On success, return a JSON object written to disk alongside the PDF and also returned to the caller:

```json
{
  "pdf_path": "data/_sources/MSFT/_raw/msft-10k-20250630.pdf",
  "sha256": "c4ae...",
  "source": "sec_edgar",
  "source_url": "https://www.sec.gov/Archives/edgar/data/789019/000095017025...",
  "accession_number": "0000950170-25-...",
  "form_type": "10-K",
  "filing_date": "2025-07-30",
  "period_of_report": "2025-06-30",
  "ticker": "MSFT",
  "fetched_at": "2026-04-09T14:22:11Z",
  "fetcher_version": "0.1.0",
  "protocol_version": "0.1.0-draft"
}
```

The sidecar file is written next to the PDF with the same stem and the suffix `.fetch.json`. Both files are immutable after this skill returns — they live in `_raw/` forever and are the audit trail. The organizer skill is the only other thing that ever touches them, and only to read.

`pdf_path` is always a repo-relative path, using forward slashes. Callers should resolve it against the repo root. This keeps output portable across the sandbox, the mount, and CI.

## Folder layout

```
data/_sources/
  <TICKER>/
    _raw/
      <original-or-sanitized-filename>.pdf
      <original-or-sanitized-filename>.fetch.json
```

The `<TICKER>` directory is flat under `_raw/`; year-based subfolders and human-friendly filenames are created by `organize-sources`, not here. This skill should not create `<YYYY>/` directories or canonical-name copies.

The filename inside `_raw/` should be whatever the regulator served, lightly sanitized:

- Lowercase.
- Replace anything outside `[a-z0-9._-]` with `-`.
- Collapse consecutive dashes.
- Preserve the original extension if it's `.pdf`; otherwise force `.pdf` after the HTML→PDF render step.

If two fetches land on the same sanitized name but have different sha256 hashes, append `-<short-hash>` before the extension to disambiguate. If the sha256 matches something already in `_raw/`, treat it as a no-op and return the existing path — we don't rewrite history.

## Step-by-step workflow

### 1. Resolve identity

Load `data/_sources/_identity.yaml`. If the requested `ticker` is not a key, raise `UnknownCompanyError(ticker)`. Read `preferred_source`, `edgar_cik` or `hkex_stock_code`, and `fiscal_year_end_month` from the entry.

If the caller asked for a form type that's incompatible with the preferred source (e.g. `10-K` for an HKEX-only company), raise `FormTypeMismatchError` with both the requested type and the source's supported types. Don't silently translate.

### 2. Dispatch to the source-specific fetcher

```
if entry.preferred_source == "sec_edgar":
    result = fetch_from_sec(entry, form_type, period)
elif entry.preferred_source == "hkex":
    result = fetch_from_hkex(entry, form_type, period)
```

The two fetchers are implemented in `scripts/sec_fetcher.py` and `scripts/hkex_fetcher.py`. See the "Implementation notes" section below for what each needs to do.

### 3. Verify the download

Before accepting the bytes:

- Check `Content-Type` if the server provided one — accept `application/pdf` for direct downloads, `text/html` if we're going to render.
- Read the first 8 bytes and confirm the PDF magic `%PDF-` if we think we have a PDF.
- Compute sha256.
- If the file is smaller than 50 KB or larger than 200 MB, raise `SuspiciousFilingSizeError` and do not commit. These bounds are intentionally loose; they're meant to catch truncated downloads and runaway responses, not to impose real limits.

### 4. Write to `_raw/` atomically

Write to a temp file in the same directory, fsync, then rename into place. This prevents a half-written PDF from being visible to the organizer or to a human poking around.

Write the sidecar `.fetch.json` after the PDF, same atomic pattern. If writing the sidecar fails, delete the PDF and raise — we never leave orphaned PDFs without sidecars.

### 5. Return the result

Return the JSON object described under "Outputs". The caller decides what to do next; this skill's job is done.

## Error types (structured, no UI coupling)

All errors are regular Python exceptions that the skill raises directly. Callers (the watcher, a human running the CLI, another skill) decide how to present them. Don't print, don't prompt, don't try to recover.

- `UnknownCompanyError(ticker)` — ticker not in identity table.
- `FormTypeMismatchError(ticker, requested, supported)` — e.g. asked for 10-K on an HK-only company.
- `AmbiguityError(dimension, candidates, reason)` — more than one filing matches the request and `period` wasn't specific enough. `candidates` is a list of `{accession_number, filing_date, period_of_report}` dicts so the caller can pick one and retry.
- `FilingNotFoundError(ticker, form_type, period)` — no matching filing exists (yet).
- `SourceUnavailableError(source, http_status, message)` — EDGAR or HKEXnews returned a non-2xx.
- `SuspiciousFilingSizeError(path, size_bytes)` — size outside sane bounds.
- `IntegrityError(expected_hash, actual_hash)` — something tampered with the file between download and sidecar write.

Keep the error types here. Sibling skills and callers import them from a shared module so the whole pipeline speaks the same error vocabulary.

## Implementation notes

### SEC EDGAR fetcher

Use the free EDGAR submissions API. No key required, but `User-Agent` is mandatory — set it to something identifying the project and contact, e.g. `"neocloud-capex-tracker contact@example.com"`. Rate-limit to 10 requests/second max per EDGAR's published guidance; in practice this skill will do fewer than 10 requests per invocation so a simple `time.sleep` between calls is fine.

Flow:

1. `GET https://data.sec.gov/submissions/CIK{cik_padded_to_10}.json` to list recent filings.
2. Filter by `form` matching `form_type`. For 10-Q specifically, filter to the most recent one whose `reportDate` matches the requested period; otherwise take index 0.
3. The response gives `accessionNumber`, `filingDate`, `reportDate`, and `primaryDocument` (usually the `.htm` index or the filing document itself).
4. Build the filing directory URL: `https://www.sec.gov/Archives/edgar/data/{cik_no_leading_zeros}/{accession_stripped_of_dashes}/`.
5. Prefer a `.pdf` if one exists in that directory. If not, the primary document is usually HTML — use the HTML→PDF renderer (see below).

Parse dates carefully: EDGAR's `reportDate` is the period end (what we call `period_of_report`), `filingDate` is when it hit EDGAR.

### HKEXnews fetcher

HKEXnews does not publish a clean JSON API, but the filings listing page (`https://www1.hkexnews.hk/listedco/listconews/advancedsearch/search_active_main.aspx`) supports query parameters that return a structured HTML result. A thin wrapper that posts the stock code and document-type filter and parses the returned table is sufficient for v1.

Annual reports and interim reports on HKEXnews are served as PDFs directly — HTML→PDF rendering is usually not needed for HK sources.

Rate-limit politely: one request per second is generous.

### HTML→PDF rendering (SEC HTML filings)

When the primary document is HTML, render with headless Chromium via `playwright` (preferred) or `weasyprint` as a fallback. Pin versions in `pyproject.toml` and record the renderer name and version in the sidecar JSON under an optional `rendered_by` field so reproducibility is auditable.

Renderer settings must be fixed and deterministic:

- A4 paper, 0.5in margins.
- Print backgrounds enabled.
- No header/footer.
- Bundled stylesheet if the filing references external CSS that might not be available at render time.

The resulting PDF is what gets hashed and written to `_raw/`. The sha256 is therefore a hash of *our* render, not of the original HTML — this is acceptable and documented in the memo because the renderer is pinned.

### Dependencies

Keep this skill's Python dependencies minimal and declared in its own `pyproject.toml` extra so the main project isn't forced to install Playwright unless it wants to fetch HTML-only filings.

```
requests>=2.31
pyyaml>=6.0
# optional for HTML→PDF:
playwright>=1.40
```

## Testing guidance

This skill is a good candidate for golden-file tests rather than live network tests.

- Keep a small set of real EDGAR and HKEX response fixtures in `tests/fixtures/` (maybe 3-4 of each form type).
- Mock `requests.get` to return those fixtures.
- Assert the sidecar JSON matches a golden copy byte-for-byte, except for `fetched_at` which is substituted.
- Have one opt-in integration test that actually hits EDGAR, marked `@pytest.mark.network`, skipped in CI unless a secret is set.

For the HTML→PDF path, store one HTML fixture and one "expected" PDF byte count range — don't assert exact byte equality because PDF output can vary slightly across renderer patch versions.

## What this skill does NOT do

Worth repeating because the boundary matters:

- No text extraction. No page markers. No canonical-form filenames. No cross-filing validation. No database writes. No LLM calls.
- No caching strategy beyond "sha256 dedupe in `_raw/`". The watcher decides when to invoke; this skill is stateless.
- No Slack, email, or Jira integration. Errors are exceptions. The caller decides how to surface them.

Keep it small and boring. That's the point.
