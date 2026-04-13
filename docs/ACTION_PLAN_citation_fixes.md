# Action Plan: Excel Citation Fixes

**Status:** Planned, not yet actioned.
**Created:** 2026-04-13
**Owner:** @KKKKKKAI
**Priority:** High — blocks analyst review of Excel output.

---

## Problem 1: Download links go to SEC directory, not the actual report

**Current:** `https://www.sec.gov/Archives/edgar/data/789019/000119312515272806/`
(a folder listing with many sub-files — confusing for analysts)

**Needed:** `https://www.sec.gov/Archives/edgar/data/789019/000119312515272806/msft-20150630.htm`
(direct link to the actual report file)

### Fix steps

- [ ] **1a. Audit source_documents.source_url** — for the 286 filings
  downloaded in Phase 6A.4, verify that `source_url` already contains
  the full URL with the document filename (`.htm` or `.pdf` suffix).
  The download script set this, but the 46 fixup entries may only have
  directory URLs.

- [ ] **1b. Backfill missing filenames** — for any `source_url` that
  ends with `/` (directory only), look up the primary document filename:
  - First check: the sidecar JSON at `raw_path + ".fetch.json"` has the
    original `source_url` with the filename.
  - Fallback: query SEC submissions API for the company → match by
    `accession_number` → get `primaryDocument` → append to the directory URL.
  - Update `source_documents.source_url` with the full document URL.

- [ ] **1c. Update citations.py `_build_external_url()`** — change the
  logic to:
  - If `source_url` contains `.htm` or `.pdf` → return it directly
    (already a full document URL).
  - If `source_url` ends with `/` → append the primary document
    filename from `source_documents` (need to add this field or
    derive from the sidecar).
  - NEVER return a directory-only URL.

- [ ] **1d. For HKEX filings** — `source_url` already points to the
  specific PDF on HKEXnews. Verify no changes needed.

---

## Problem 2: Method says "XBRL API" instead of citing the actual report section

**Current:**
```
Section: XBRL companyfacts API
Method: XBRL companyfacts API (capital_expenditures)
```

**Needed:**
```
Section: Item 8 - Consolidated Statements of Cash Flows
Line item: "Purchases of property and equipment"
Value cross-checked against SEC XBRL structured data.
```

### Fix steps

- [ ] **2a. Build deterministic section mapping** — for each headline
  metric, the section in a 10-K/10-Q is invariant:

  | metric_key | SEC 10-K/10-Q Section | Typical line item |
  |---|---|---|
  | `capital_expenditures` | Item 8 - Cash Flows Statement | "Purchases of property and equipment" |
  | `revenue` | Item 8 - Income Statement | "Total revenue" or "Net revenue" |
  | `operating_cash_flow` | Item 8 - Cash Flows Statement | "Net cash from operating activities" |
  | `depreciation_amortization` | Item 8 - Cash Flows Statement | "Depreciation, amortization, and other" |
  | `property_plant_equipment_net` | Item 8 - Balance Sheet | "Property and equipment, net" |

  For 20-F filers: similar sections but headings differ (e.g.,
  "Consolidated Statements of Cash Flows" without the "Item 8" prefix).

  Store this mapping in `data/seeds/coverage.yaml` under a new
  `section_mappings` block so it's reviewable and editable.

- [ ] **2b. LLM-read one filing per company to find exact line item
  wording** — for each of the 12 SEC companies, read their latest
  annual report (already in `data/_sources/<TICKER>/_raw/`), find the
  financial statements, and record the EXACT wording of each line item.

  Example for MSFT:
  - capex: "Additions to property and equipment" (not "Purchases of...")
  - revenue: "Total revenue"
  - OCF: "Net cash from operations"
  - D&A: "Depreciation, amortization, and other"
  - PP&E: "Property and equipment, net of accumulated depreciation"

  Store per-company overrides in `coverage.yaml` alongside the defaults.

  This is ~12 LLM reads (one per company), NOT 1,200.

- [ ] **2c. Backfill locator_section for all XBRL extractions** —
  ```sql
  UPDATE extractions SET
      locator_section = [mapped section + line item],
      quote = [exact line item wording from the filing]
  WHERE extracting_model = 'xbrl-companyfacts'
  AND source_document_id IN (
      SELECT id FROM source_documents WHERE ticker = ?
  )
  AND metric_key = ?
  ```

  Run for each (company, metric) pair using the per-company mapping
  from step 2b.

- [ ] **2d. Update extracting_model** — change from `'xbrl-companyfacts'`
  to `'xbrl-verified'` to indicate the value came from XBRL but the
  section reference was verified against the actual filing.

- [ ] **2e. Update citations.py** — remove the `"Method: XBRL
  companyfacts API"` line. Replace with the section reference from
  `locator_section`. Add a small note: `"Value cross-checked against
  SEC XBRL structured data."` This tells the analyst the number is
  reliable without implying it was never seen in the actual filing.

- [ ] **2f. Citation format after fix:**
  ```
  Source: [MSFT] FY2025 10-K (filed 2025-07-30)
  Section: Item 8 - Consolidated Statements of Cash Flows
  Line item: "Additions to property and equipment"
  Value: $64,551M (as reported)
  Value cross-checked against SEC XBRL structured data.

  Report: https://www.sec.gov/Archives/edgar/data/789019/000095017025100235/msft-20250630.htm
  ```

---

## Problem 3: Validation — annual reports first

- [ ] **3a. Regenerate Excel** with fixes from steps 1 + 2.

- [ ] **3b. Spot-check 3 companies × 3 metrics:**
  - MSFT capex → Shift+F2 should show "Item 8 - Cash Flows, line
    'Additions to property and equipment'", link goes to
    `msft-20250630.htm` (not a directory).
  - AMZN revenue → "Item 8 - Income Statement, line 'Net sales'",
    link goes to `amzn-20251231.htm`.
  - BABA cloud → derivation formula, link goes to
    `baba-20250331.htm`.
  - 0700 cloud → proxy warning, link goes to HKEXnews PDF.
  - BIDU cloud (derived) → footnote reasoning, link goes to
    `d38065d20f.htm` (or canonical name).

- [ ] **3c. User validates** the annual output.

- [ ] **3d. If approved** → proceed to quarterly extraction + source
  re-indexing.

---

## Execution order

```
1a-1d  Fix download links           ~1 hr
2a     Build section mapping         ~30 min
2b     LLM-read 12 reports           ~30 min
2c-2d  Backfill DB                   ~10 min
2e-2f  Update citations.py           ~30 min
3a     Regenerate Excel              ~5 min
3b-3c  Spot-check + user validation  ~10 min
```

## Notes for future sessions

- This plan is NOT yet committed to the RESTRUCTURING_PLAN.md — it's
  a standalone action plan document pending user review.
- All commits for this work should follow Conventional Commits:
  `fix(citations): use direct filing URL instead of directory`
  `data(extract): backfill section references for XBRL extractions`
- The section mapping in coverage.yaml is the USER-REVIEWABLE artifact.
  When the user reviews a citation and finds the wording is slightly
  different, they edit the YAML and regenerate.
- Quarterly extraction is BLOCKED on this — do annual validation first.
