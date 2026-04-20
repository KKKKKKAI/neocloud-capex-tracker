# Restatement Policy

Listed companies routinely **restate** historical quarterly and annual
values when they reorganize reporting lines. The filing where a number
*first* appeared is not the filing where it's most accurate; the
*latest* filing that covers that period is. This doc explains how we
capture, prioritise, and cite restated values throughout the
extraction → audit → chart → Excel pipeline.

---

## The problem

Concrete example (MSFT Intelligent Cloud, reported by the user
2026-04-20):

| Period | As-reported in original 10-Q | Restated in next fiscal-year 10-K |
|---|---|---|
| FY2024 Intelligent Cloud | $105,362M | $87,464M |

MSFT narrowed the Intelligent Cloud scope in FY2024 (moved Office365
and Dynamics out to Productivity & Business Processes). The FY2025
10-K's retrospective segment table restates FY2024 lower; comparing
the two values gives a clean apples-to-apples series.

If we keep citing the original 10-K we draw an artificial "drop"
between FY2024 (old scope) and FY2025 (new scope). If we cite the
restated figure from the FY2025 10-K, the curve is smooth and the
Excel cell comment points at the filing that's authoritative today.

---

## The rule: newest `source_documents.filing_date` wins

Every selector in the pipeline now orders by
`source_documents.filing_date DESC` as the tiebreaker when multiple
extraction rows exist for the same
`(ticker, metric_key, fiscal_year, period_type)` cell.

Hardened selectors:

| File | Function |
|---|---|
| `src/capex/exporters/interactive_chart.py` | `_load_annual`, `_load_quarterly` |
| `src/capex/exporters/charts.py` | PNG annual query |
| `src/capex/audit/orchestrator.py` | `load_cells` |
| `src/capex/extract/reconcile.py` | `_load_existing` + `_group_rows` |

Because Excel cell comments and chart hover tooltips read the row's
`source_document.source_url`, `locator_section`, and `quote`, the
citation auto-rewires whenever a restated row wins: **2023Q3 figure
restated by the 2024Q3 10-Q cites the 2024Q3 download link**, without
any change in the Excel exporter itself.

---

## The three capture paths

### 1. XBRL companyfacts (automatic)

SEC's companyfacts API returns every historical value a company has
ever reported, each tagged with an `accn` (accession number) and a
`filed` date. When the same `(end_date, form, start_date, duration)`
signature appears with different `val` and different `accn`, it's a
restatement.

`src/capex/xbrl/timeseries.py::fetch_concept_timeseries` now surfaces
both entries — the original (marked `is_restatement=False`) and every
later-filed variant (`is_restatement=True` with `restated_from_accn`
pointing at the earlier version). `write_timeseries_to_db` stores the
restated row under `extracting_model='restated-xbrl'` with a
`source_document_id` that points at the *later* filing.

No extra action required — run `capex` for any XBRL-tagged metric
and restatements flow through automatically.

### 2. Segment-table annual restatements

10-Ks list 2–3 fiscal years of segment-revenue comparatives. The
prior-year rows in the current 10-K are restated comparatives.
`src/capex/audit/restatement.py::detect` reads each ticker's most
recent 10-K / 20-F (from `data/_sources/<ticker>/_raw/`), calls
`extract.segment.extract_segment_revenue`, and emits findings where
an extracted prior-year value differs from the DB by more than 0.5%.

```
$ capex audit                              # dry-run; lists findings
$ capex audit --apply                      # writes restated rows
$ capex extract --restated --ticker MSFT   # targeted refresh
```

Each written row:
- `extracting_model = "restated-segment@0.1.0"`
- `source_document_id` = id of the *restating* 10-K (the later filing)
- `quote` / `locator_section` = excerpt from the restating filing
- `audit_log` entry `action='extraction_restated'` linking old → new

### 3. Reconcile cascade (automatic)

Derived identities (`Q4 = FY − 9M`, `9M = Q1+Q2+Q3`, BIDU cloud =
`Total − Online − iQIYI`, etc.) run on whichever rows win the
selector. Because the filing_date tiebreaker now promotes restated
rows, reconcile automatically re-derives Q4 from restated Q1/Q2/Q3
whenever a quarterly 10-Q restates a prior-year comparative.

---

## How a reviewer reads the audit report

After every `capex audit` run the markdown report has a new section:

```
## Restatements (N)

Scanned 13 tickers × 1 metric from the latest 10-K/20-F for each.
A finding indicates the latest filing's segment table reports a
materially-different value for an earlier period than what's in the DB;
the newer filing's value wins by the `filing_date DESC` selector rule
once written back. Dry-run — re-run with `capex audit --apply` to commit.

| Cell | Existing | Restated | Δ | Filing |
|---|---|---|---|---|
| MSFT:cloud_segment_revenue:2024FY | $105,362M | $87,464M | 17.0% | [filed 2025-07-30](https://www.sec.gov/…/msft-20250630.htm) |
```

Findings also appear in the JSON sidecar
(`output/data_quality_report.json` → key `restatements`).

---

## Policy per company

Companies that historically restate get a `restatement_policy` block
in `data/seeds/coverage.yaml`:

```yaml
MSFT:
  restatement_policy:
    prefer_restated: true
    known_restatements:
      - "FY2018 segment_reorg (Commercial Cloud → Intelligent Cloud)"
      - "FY2024 segment_reorg (Intelligent Cloud scope narrowed)"
    note: |
      Intelligent Cloud revenue is restated every time MSFT changes
      its segment lines. The latest 10-K's retrospective segment
      table is authoritative — prior-year values in the current 10-K
      supersede the originals.
```

The treatments viewer (`docs/treatments.html`) renders the policy
block for each company so a reviewer can see at a glance which
tickers routinely restate.

Tracked policies today: MSFT, ORCL, BABA, BIDU, GDS.

---

## Cascade into Excel citations (no code change needed)

When the selector promotes the restated row:

- `exporters/excel.py` reads `source_url` from
  `source_documents` → Shift+F2 cell-comment link becomes the
  *restating* filing.
- `exporters/citations.py` reads `locator_section` + `quote` from the
  extraction row → cell comment body quotes the *restating* filing's
  segment-table paragraph.

The rule is enforced by data, not by special-case code in the
exporter.

---

## Known gaps / follow-ups

- **10-Q prior-year comparatives**: a 10-Q's income statement shows
  current quarter + prior-year same quarter. Extracting the prior-year
  column for a quarterly restatement is more involved than annual
  segment tables — deferred.
- **HKEX interim reports (HK-IR, HK-AR)**: `0700` and `9698` (GDS HK)
  publish restatements on HKEX too. We don't yet parse HKEX
  retrospectives.
- **MSFT supplemental 8-K quarterly segment restatements**: MSFT
  publishes an 8-K after a segment reorg with restated quarterly
  breakdowns. A bespoke extractor for that document would close the
  MSFT quarterly-restatement loop entirely.

---

## Implementation map

| Concern | File |
|---|---|
| XBRL restatement capture | `src/capex/xbrl/timeseries.py` |
| Segment-table restatement extract | `src/capex/extract/extractors/restated.py` |
| Detector + applier | `src/capex/audit/restatement.py` |
| Audit CLI + report | `scripts/audit_data_quality.py`, `src/capex/audit/report.py` |
| CLI `extract --restated` | `src/capex/cli/main.py::_extract_restated` |
| Selector hardening | `interactive_chart._load_annual/_load_quarterly`, `charts.py`, `audit/orchestrator.load_cells`, `extract/reconcile._load_existing/_group_rows` |
| Coverage policy | `data/seeds/coverage.yaml` |
| Treatments viewer surface | `src/capex/exporters/treatments_html.py` |
