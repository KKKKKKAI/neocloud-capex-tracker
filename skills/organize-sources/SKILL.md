---
name: organize-sources
description: Walk the `data/_sources/` archive, find any newly fetched company filings sitting in `_raw/` subfolders, and create canonical human-friendly copies under year-level directories using the naming convention `[yyyy.mm.dd][TICKER][PERIOD][FORM].<ext>`. Use this skill whenever the user says anything like "organize the sources folder", "rename the filings", "tidy up the annual report archive", "clean up _sources", or after any batch of `fetch-company-report` runs. Also use this skill proactively if the user is exploring `data/_sources/` and notices raw unrenamed files, or if a scheduled nightly sweep is kicking off. This is the only sanctioned writer of canonical filenames — never rename files in `_sources/` by hand or via other tools.
---

# organize-sources

## Purpose

`fetch-company-report` deliberately leaves filings in `data/_sources/<TICKER>/_raw/` with whatever filename the regulator served. That keeps the fetcher small and the raw archive immutable. But humans (and some downstream tooling) need a predictable, searchable layout: one folder per fiscal year, files named so they sort chronologically and reveal period/form at a glance.

This skill owns that translation. It is intentionally a separate concern from fetching, for three reasons:

1. **Immutability of `_raw/`.** If renaming lived inside the fetcher, a bug in the naming grammar would corrupt the raw archive. Here, the canonical layer is derivative and can be regenerated from `_raw/` at any time by deleting the year folders and rerunning.
2. **Testability.** Given a fixed `_raw/` tree and identity table, this skill's output is a pure function. No network, no LLM, no state.
3. **Re-runnability.** If we ever change the naming convention, we re-run this skill over the existing raw archive and get the new layout for free.

## When to trigger

- Organize, tidy, clean up, or rename the `_sources/` folder.
- Apply canonical naming to recently fetched filings.
- Run the nightly sweep over `data/_sources/`.
- Verify the `_sources/` tree is in a consistent state.

Also trigger when the user references raw unrenamed files in `_sources/` and asks what they are or where the canonical versions are.

Do **not** trigger for general filesystem cleanup, duplicate removal across the repo, or anything outside `data/_sources/`.

## Inputs

```
{
  "dry_run": false,    # if true, log intended actions but write nothing
  "ticker": null       # if set, scope the sweep to a single company
}
```

Default is a full sweep with writes enabled.

## Outputs

1. **Canonical file copies** at `data/_sources/<TICKER>/<YYYY>/[yyyy.mm.dd][TICKER][PERIOD][FORM].<ext>` (extension matches the original — `.htm` for SEC HTML filings, `.pdf` for HKEX or any PDF the regulator served).
2. **An updated `source_documents.canonical_path`** column for each file copied (UPDATE statement on the matching row identified by sha256).
3. **An append-only log** at `data/_sources/_organizer_log.csv` capturing every action taken (including no-ops and collisions).

Returns a summary object:

```json
{
  "scanned": 42,
  "copied": 3,
  "skipped_already_canonical": 39,
  "collisions": 0,
  "errors": []
}
```

## Naming grammar

```
[yyyy.mm.dd][TICKER][PERIOD][FORM].<ext>
```

| Token       | Value                                                                                          |
|-------------|------------------------------------------------------------------------------------------------|
| `yyyy.mm.dd`| `filing_date` from the sidecar / DB row (not `period_of_report`)                                |
| `TICKER`    | The `companies.ticker` key: SEC ticker (`MSFT`), HK stock code (`0700`), or canonical alias    |
| `PERIOD`    | One of `AR`, `Q1`, `Q2`, `Q3`, `H1`, `H2` — derived from form + `period_of_report` + FYE month |
| `FORM`      | One of `10-K`, `10-Q`, `20-F`, `HK-AR`, `HK-IR`                                                |
| `<ext>`     | Original file extension as served by the regulator (`.htm`, `.pdf`)                             |

### Period derivation rules

The `PERIOD` token is derived deterministically from `form_type`, `period_of_report`, and the company's `fiscal_year_end_month`.

- `10-K`, `20-F`, `HK-AR` → `AR` (always).
- `10-Q` → `Q1`, `Q2`, or `Q3` based on which fiscal quarter `period_of_report` falls in. There is no `Q4` for 10-Q because the fourth quarter rolls into the annual. If the math lands on Q4, raise `PeriodDerivationError`.
- `HK-IR` → `H1` or `H2` based on the half-year that `period_of_report` ends.

The fiscal year used for the `<YYYY>/` folder is the **fiscal year the filing belongs to**, not the calendar year of the filing date. For NVDA (FYE January), a 10-Q filed in October 2025 for fiscal Q3 of FY2026 lives under `NVDA/2026/`.

### Examples

- `[2025.07.30][MSFT][AR][10-K].htm` — Microsoft FY25 10-K (HTML primary).
- `[2025.10.24][NVDA][Q3][10-Q].htm` — Nvidia fiscal Q3 10-Q.
- `[2025.06.27][BABA][AR][20-F].htm` — Alibaba 20-F (dual-listed, fetched from SEC).
- `[2026.03.20][0700][AR][HK-AR].pdf` — Tencent annual report (HKEX-only PDF).
- `[2025.08.15][0700][H1][HK-IR].pdf` — Tencent interim report.

## Folder layout (what the skill produces)

```
data/_sources/
  MSFT/
    _raw/
      msft-20250630.htm
      msft-20250630.htm.fetch.json
    2025/
      [2025.07.30][MSFT][AR][10-K].htm    ← copy produced by this skill
  0700/
    _raw/
      tencent-2024-ar.pdf
      tencent-2024-ar.pdf.fetch.json
    2024/
      [2025.03.20][0700][AR][HK-AR].pdf
  _identity.yaml
  _organizer_log.csv
```

## Decision matrix

For each `<TICKER>/_raw/*.fetch.json` found:

- **Target does not exist** → copy the source file from `_raw/` to the target path. Log `copied`. UPDATE the matching `source_documents` row's `canonical_path`.
- **Target exists with the same sha256** → no-op. Log `skipped_already_canonical`. Verify the DB row's `canonical_path` matches; if not, UPDATE it.
- **Target exists with a different sha256** → amended-filing case. Append `-a1` (then `-a2`, etc.) before the extension until the new name is free, then copy. Log `collision_amended`.
- **Two raw files map to the same canonical name in one sweep** → dedupe by sha256 first; if genuinely different, apply `-a<N>` to the one with the later `fetched_at`. Log `collision_sameday`.

All copies are atomic (temp + rename in the same directory). The sha256 of every copy is verified against the sidecar; mismatches raise `IntegrityError` and the partial copy is removed.

## Error vocabulary

- `IdentityTableMissingError` — `companies` table is empty (run `db sync-companies`).
- `PeriodDerivationError(ticker, form_type, period_of_report, reason)` — can't figure out the period token.
- `IntegrityError(path, expected_hash, actual_hash)` — copy hash didn't match the sidecar.
- `OrphanedRawFileError(raw_path)` — file in `_raw/` has no sidecar.
- `OrphanedSidecarError(sidecar_path)` — sidecar references a file that doesn't exist.

All errors are collected into the summary's `errors` array rather than aborting the sweep mid-flight. One broken filing should not prevent the other 40 from being organized.

## Idempotency and safety properties

- **Idempotent.** Running twice in a row produces no new writes on the second run.
- **Safe to interrupt.** Atomic copies mean an interrupted run leaves no partial files.
- **Never deletes.** This skill never removes files, even from its own canonical tree.
- **Never touches `_raw/`.** Read-only on the raw archive.
- **Never touches sidecars.** Read-only on `.fetch.json` files.

## Implementation

Lives in `src/capex/organize/`:

- `namer.py` — pure functions for canonical filename grammar and period derivation.
- `walker.py` — sweep logic, atomic copy, DB UPDATE, log append.

## What this skill does NOT do

- No fetching. If a file isn't already in `_raw/`, this skill won't go get it.
- No text extraction, no LLM calls, no network.
- No deletion of any kind.
- No schema validation beyond the sidecar field check — the fetcher is responsible for sidecar correctness at write time.
- No propagation into extractions. The extraction layer is a separate concern that reads from the canonical tree this skill produces.

Small, boring, idempotent. If it ever grows past that, split it.
