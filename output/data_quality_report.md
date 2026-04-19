# Data Quality Audit — 2026-04-19 23:36 UTC

**Run ID**: `audit-20260419-233632`

**Scope**: 13 tickers × 6 metrics × 2015–2025.  
**Total cells in universe**: 77

**Status**: ✓ 36 verified · * 26 derived · ⚠ 1 flagged · ⊘ 4 gap fixable · ✗ 10 gap unfixable

---

## Coverage matrix

Percentages are `(verified + derived) / total` for each ticker × metric bucket.

| Ticker | Revenue | CapEx | OCF | D&A | PP&E | Cloud Seg |
|---|---|---|---|---|---|---|
| BABA | — | — | — | — | — | 81% ⚠ |

## Flagged items (1)

### BABA — Cloud Seg
- **2018Q1** $771M: failed `continuity`
  - continuity jump factor 3.035x (2017Q4 → 2018Q1)

## Fixed in this run (5)

### BABA
- **2018Q1** Cloud Seg: xbrl_refetch (dry-run)
- **2015Q4** Cloud Seg: gap_extract (manual: scripts/extract_baba_cloud_6k.py)
- **2015FY** Cloud Seg: gap_extract (manual: scripts/extract_baba_cloud_6k.py)
- **2016Q4** Cloud Seg: gap_extract (manual: scripts/extract_baba_cloud_6k.py)
- **2016FY** Cloud Seg: gap_extract (manual: scripts/extract_baba_cloud_6k.py)

## Known unfixable gaps (10)

- **BABA**: 10 cells (metrics: Cloud Seg; years: 2015-2016)

## Run metadata

- Database: `data/db/capex.db`
- Metrics: revenue, capital_expenditures, operating_cash_flow, depreciation_amortization, property_plant_equipment_net, cloud_segment_revenue
- Checks: gap, identity, range, continuity, cross_source, sign, currency, segment_def, period_type
- Total cells audited: 77

