---
name: organize-sources
description: Walk the `data/_sources/` archive, find any newly fetched company filings sitting in `_raw/` subfolders, and create canonical human-friendly copies at the year level using the naming convention `[dd.mm.yyyy][TICKER][PERIOD][FORM].pdf`. Use this skill whenever the user says anything like "organize the sources folder", "rename the filings", "tidy up the annual report archive", "clean up _sources", or after any batch of `fetch-company-report` runs. Also use this skill proactively if the user is exploring `data/_sources/` and notices raw unrenamed PDFs, or if a scheduled nightly sweep is kicking off. This is the only sanctioned writer of canonical filenames — never rename files in `_sources/` by hand or via other tools.
---

# organize-sources

## Purpose

`fetch-company-report` deliberately leaves filings in `data/_sources/<TICKER>/_raw/` with whatever filename the regulator served. That keeps the fetcher small and the raw archive immutable. But humans (and some downstream tooling) need a predictable, searchable layout: one folder per fiscal year, files named so they sort chronologically and reveal period/form at a glance.

This skill owns that translation. It's intentionally a separate concern from fetching, for three reasons:

1. **Immutability of `_raw/`.** If renaming lived inside the fetcher, a bug in the naming grammar would corrupt the raw archive. Here, the canonical layer is derivative and can be regenerated from `_raw/` at any time by deleting the year folders and rerunning.
2. **Testability.** Given a fixed `_raw/` tree and identity table, this skill's output is a pure function. No network, no LLM, no state.
3. **Re-runnability.** If we ever decide to change the naming convention, we re-run this skill over the existing raw archive and get the new layout for free.

## When to trigger

Trigger when the user or a scheduled job asks to:

- Organize, tidy, clean up, or rename the `_sources/` folder.
- Apply canonical naming to recently fetched filings.
- Run the nightly sweep over `data/_sources/`.
- Verify the `_sources/` tree is in a consistent state.

Also trigger when the user references raw unrenamed PDFs in `_sources/` and asks what they are or where the "clean" versions are.

Do **not** trigger for general filesystem cleanup, duplicate removal across the repo, or anything outside `data/_sources/`.

## Inputs

None. The skill walks the filesystem. Optionally accepts:

```
{
  "dry_run": false,     # if true, log intended actions but write nothing
  "ticker": null        # if set, scope the sweep to a single company
}
```

Default is a full sweep with writes enabled.

## Outputs

The skill writes:

1. **Canonical PDF copies** at `data/_sources/<TICKER>/<YYYY>/[dd.mm.yyyy][TICKER][PERIOD][FORM].pdf`.
2. **An append-only log** at `data/_sources/_organizer_log.csv` capturing every action taken (including no-ops and collisions).

And returns a summary object:

```json
{
  "scanned": 42,
  "copied": 3,
  "skipped_already_canonical": 39,
  "collisions": 0,
  "errors": []
}
```

## Naming grammar (authoritative)

```
[dd.mm.yyyy][TICKER][PERIOD][FORM].pdf
```

| Token       | Value                                                                                   |
|-------------|-----------------------------------------------------------------------------------------|
| `dd.mm.yyyy`| `filing_date` from the sidecar (not `period_of_report`)                                  |
| `TICKER`    | Identity-table key: SEC ticker (`MSFT`), HK stock code (`0700`), or canonical alias     |
| `PERIOD`    | One of `AR`, `Q1`, `Q2`, `Q3`, `H1`, `H2` — derived from form + `period_of_report` + fiscal year end |
| `FORM`      | One of `10-K`, `10-Q`, `20-F`, `HK-AR`, `HK-IR` — taken from `form_type` in the sidecar |

### Period derivation rules

The `PERIOD` token is derived deterministically from `form_type` and `period_of_report`, using the company's `fiscal_year_end_month` from the identity table.

- `10-K`, `20-F`, `HK-AR` → `AR` (always).
- `10-Q` → `Q1`, `Q2`, or `Q3` based on which fiscal quarter `period_of_report` falls in. There's no `Q4` for 10-Q because the fourth quarter rolls into the annual. If the math lands on "Q4" because of weird fiscal calendars, raise a `PeriodDerivationError` and log — don't guess.
- `HK-IR` → `H1` or `H2` based on the half-year that `period_of_report` ends. HK interim reports are typically H1-only (mid-year), but H2 is valid for some fiscal calendars.

Example calculation for NVDA (fiscal year ends in January, so fiscal_year_end_month = 1):

- Period end April → month 3 of fiscal year → Q1
- Period end July → month 6 of fiscal year → Q2
- Period end October → month 9 of fiscal year → Q3

### Examples

- `[30.07.2025][MSFT][AR][10-K].pdf` — Microsoft FY25 10-K filed 2025-07-30.
- `[24.10.2025][NVDA][Q3][10-Q].pdf` — Nvidia fiscal Q3 10-Q filed 2025-10-24.
- `[27.06.2025][BABA][AR][20-F].pdf` — Alibaba 20-F filed 2025-06-27 (dual-listed, fetched from SEC).
- `[20.03.2026][0700][AR][HK-AR].pdf` — Tencent annual report, HKEX-only, filed 2026-03-20.
- `[15.08.2025][0700][H1][HK-IR].pdf` — Tencent interim report.

## Folder layout (what the skill produces)

```
data/_sources/
  MSFT/
    _raw/
      msft-10k-20250630.pdf
      msft-10k-20250630.fetch.json
    2025/
      [30.07.2025][MSFT][AR][10-K].pdf      ← copy produced by this skill
  NVDA/
    _raw/
      nvda-10q-q3-20251026.pdf
      nvda-10q-q3-20251026.fetch.json
    2026/
      [24.10.2025][NVDA][Q3][10-Q].pdf      ← note: fiscal year folder
  _identity.yaml
  _organizer_log.csv
```

The year in the folder path is the **fiscal year the filing belongs to**, not the calendar year of the filing date. For NVDA, a 10-Q filed in October 2025 for fiscal Q3 of FY2026 lives under `NVDA/2026/`. This matches how the data will be aggregated in the workbook and keeps the folder structure semantically meaningful.

## Step-by-step workflow

### 1. Load the identity table

Read `data/_sources/_identity.yaml` once at the start. Cache it in memory for the duration of the run. If it's missing or malformed, abort — we can't derive fiscal periods without it.

### 2. Walk the archive

For each `<TICKER>/_raw/*.fetch.json`:

1. Read the sidecar JSON.
2. Validate it has the required fields: `pdf_path`, `sha256`, `ticker`, `form_type`, `filing_date`, `period_of_report`. If any are missing, log an error and skip — don't crash the whole sweep on one bad sidecar.
3. Cross-check: `ticker` in the sidecar must match the parent folder name, and the referenced PDF must exist next to the sidecar. Mismatches are logged as errors.

### 3. Compute the canonical name and target path

Given the sidecar and the identity entry:

1. Parse `filing_date` into `dd.mm.yyyy`.
2. Compute `PERIOD` using the rules above.
3. Compute the fiscal year the filing belongs to (period_of_report month vs fiscal_year_end_month).
4. Build the target path: `data/_sources/<TICKER>/<fiscal_year>/[dd.mm.yyyy][TICKER][PERIOD][FORM].pdf`.

### 4. Decide what to do

Four cases:

- **Target does not exist** → copy the PDF from `_raw/` to the target path. Log `copied`.
- **Target exists and is the same file** (sha256 of the target matches the sidecar) → no-op. Log `skipped_already_canonical`.
- **Target exists but is a different file** → this is the amended-filing case. Append `-a1` (then `-a2`, etc.) before the `.pdf` extension until the new name is free, then copy. Log `collision_amended`.
- **Two different raw files would map to the same canonical name in a single sweep** → dedupe by sha256 first; if they're genuinely different, apply the `-a<N>` suffix to the one with the later `fetched_at`. Log `collision_sameday`.

### 5. Copy atomically

Copy via a temp file in the same directory, fsync, then rename. Never write directly to the target path. This prevents a partial copy from being visible if the skill is interrupted.

`shutil.copy2` preserves mtime, which is nice-to-have but not required. What matters is that the sha256 of the copy equals the sha256 recorded in the sidecar; verify this after copying and raise `IntegrityError` if it doesn't match. Delete the partial copy on mismatch.

### 6. Append to the log

`data/_sources/_organizer_log.csv` has the schema:

```
timestamp,action,ticker,form_type,period,fiscal_year,source_path,target_path,sha256,notes
```

One row per action, including no-ops and errors. The log is append-only; never rewrite or truncate it. This is the audit trail for "why does this file exist and where did it come from".

### 7. Return the summary

Return the summary JSON described under "Outputs". Callers (nightly cron, human CLI) decide what to do with it.

## Dry-run mode

If `dry_run` is true, do everything up to and including computing target paths and classifying each file, but write nothing — not the copies, not the log. Print the intended actions to stdout in a table format. This is the recommended first step after changing the naming grammar or the identity table.

## Error types

- `IdentityTableMissingError` — `_identity.yaml` not found.
- `IdentityTableMalformedError(reason)` — YAML parse error or missing required fields.
- `PeriodDerivationError(ticker, form_type, period_of_report, reason)` — can't figure out the period token, usually because of a fiscal calendar edge case.
- `IntegrityError(path, expected_hash, actual_hash)` — the copy didn't match the sidecar hash.
- `OrphanedRawFileError(pdf_path)` — a PDF in `_raw/` has no sidecar.
- `OrphanedSidecarError(sidecar_path)` — a sidecar references a PDF that doesn't exist.

All errors are collected into the summary's `errors` array rather than raising mid-sweep. One broken filing should not prevent the other 40 from being organized. The only thing that aborts a sweep is an unreadable identity table.

## Idempotency and safety properties

- **Idempotent.** Running twice in a row produces no new writes on the second run (all actions become `skipped_already_canonical`).
- **Safe to interrupt.** Atomic copies mean an interrupted run leaves no partial files. The next run picks up where the previous one stopped.
- **Never deletes.** This skill never removes files, even from its own canonical tree. If canonical files need to be cleaned up (e.g. after a naming-grammar change), that's a manual operation with its own review.
- **Never touches `_raw/`.** Read-only on the raw archive. Always.
- **Never touches sidecars.** Read-only on `.fetch.json` files.

## Testing guidance

Golden-file tests work well here:

- Set up a fake `_sources/` tree in a tmp dir with a handful of `_raw/` PDFs and sidecars.
- Run the skill.
- Assert the tree now contains the expected canonical files and `_organizer_log.csv` entries.
- Run it again and assert zero new writes (idempotency).

Fiscal-year edge cases deserve explicit unit tests:

- Microsoft (FYE June) filing a 10-Q in October for FY Q1.
- Nvidia (FYE late January) filing a 10-Q in April for FY Q1.
- Alibaba (FYE March) filing a 20-F in June.
- A calendar-year company (FYE December) filing a 10-Q in October for Q3.
- A hypothetical amended 10-K/A landing on top of an existing 10-K.

## What this skill does NOT do

- No fetching. If a file isn't already in `_raw/`, this skill won't go get it.
- No text extraction, no LLM calls, no network.
- No deletion of any kind.
- No schema validation beyond the sidecar field check — the fetcher is responsible for sidecar correctness at write time.
- No propagation into the workbook. The workbook writer is a separate layer that reads from the canonical tree this skill produces.

Small, boring, idempotent. If it ever grows past that, split it.
