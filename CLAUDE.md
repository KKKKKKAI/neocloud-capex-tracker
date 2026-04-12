# CLAUDE.md — Agent Reference for neocloud-capex-tracker

This is the master reference for any Claude agent working on this
codebase. Read this FIRST before making any changes.

## CLI Commands

```bash
capex db migrate              # apply pending DB migrations
capex db sync-all             # migrate + sync companies + metrics from YAML
capex fetch <TICKER> <FORM>   # download latest filing from SEC/HKEX → _raw/
capex extract <TICKER>        # dry-run: show sections + metrics for extraction
capex export [-o PATH]        # generate Excel workbook from DB
capex chart [-o PATH]         # regenerate PNG chart (YoY auto-recalculated)
```

## Key Modules

| Module | Purpose |
|---|---|
| `src/capex/fetch/sec.py` | SEC EDGAR fetcher — downloads filings with canonical names |
| `src/capex/fetch/hkex.py` | HKEXnews fetcher — downloads HKEX annual/interim reports |
| `src/capex/fetch/dispatcher.py` | Routes fetch requests by form_type + source |
| `src/capex/fetch/sidecar.py` | JSON sidecar writer/reader for raw archive |
| `src/capex/read/text.py` | Extract text from HTML (SEC) or PDF (HKEX via pdfplumber) |
| `src/capex/read/sections.py` | Parse text into named sections (Items 7, 8, Notes) |
| `src/capex/extract/writer.py` | Adapter-agnostic DB writer — validates + writes extractions |
| `src/capex/extract/segment.py` | Generalized segment revenue extractor with table scoring |
| `src/capex/xbrl/timeseries.py` | XBRL companyfacts API — pulls full quarterly history |
| `src/capex/fx/rates.py` | FX rate lookups via frankfurter.app (ECB data) |
| `src/capex/db/schema.py` | SQLite Database wrapper + migrator |
| `src/capex/db/sync.py` | YAML → DB sync (companies + metric_definitions) |
| `src/capex/exporters/excel.py` | Excel workbook generator (all values in USD) |
| `src/capex/exporters/charts.py` | Chart generator (YoY always recalculated from DB) |
| `src/capex/exporters/citations.py` | Source citation formatter for Excel cell comments |
| `src/capex/query/line_items.py` | User-facing metric lookup with cache |
| `src/capex/organize/namer.py` | Canonical filename grammar + period derivation (KEPT) |
| `src/capex/organize/walker.py` | DEPRECATED — organize step removed, naming at fetch time |

## Key Data Files

| File | Purpose |
|---|---|
| `data/_sources/_identity.yaml` | Company registry — ticker, CIK, FYE, currency |
| `data/seeds/coverage.yaml` | Coverage treatments — per-company adjustments, derivations |
| `data/seeds/metric_definitions.yaml` | Metric registry with XBRL concepts + aliases |
| `data/db/capex.db` | SQLite database (the system of record) |
| `data/db/dump.sql` | Auto-generated SQL dump (for PR review) |

## Skills

| Skill | When to use |
|---|---|
| `fetch-company-report` | Download a filing from SEC/HKEX |
| `read-and-extract` | Extract metrics from a downloaded filing (v1: Claude Code interactive) |
| `query-line-item` | Look up an extracted metric with provenance |
| `organize-sources` | DEPRECATED — naming now happens at fetch time |

## Critical Rules

1. **ALWAYS fetch before extracting.** Use `capex fetch` to download
   reports to `data/_sources/<TICKER>/_raw/` BEFORE extracting data.
   NEVER download to temp files that get deleted.

2. **All Excel values in USD.** Non-USD companies are FX-converted.
   Local currency is in the cell comment only (for audit).

3. **Citations use EXTERNAL URLs only.** SEC EDGAR or HKEXnews links
   that an analyst can copy-paste into a browser. NEVER reference
   local file paths, GitHub repo URLs, or our codebase.

4. **YoY growth is always recalculated.** Never cache or filter YoY
   values. Call `capex chart` after any data change.

5. **BABA + BIDU XBRL values are in USD.** SEC XBRL for 20-F filers
   returns USD convenience translations. Do NOT treat them as CNY.
   GDS XBRL IS in CNY (correct).

## Common Workflows

**Add a new company:**
1. Add entry to `data/_sources/_identity.yaml`
2. Add entry to `data/seeds/coverage.yaml` (treatments + adjustments)
3. Run `capex db sync-all`
4. Run `capex fetch <TICKER> <FORM>`
5. Extract metrics (via Claude Code or headless adapter)
6. Run `capex export` + `capex chart`

**Extract a new metric from existing filings:**
1. Add metric to `data/seeds/metric_definitions.yaml`
2. Run `capex db sync-metrics`
3. Read the filing from `data/_sources/<TICKER>/_raw/`
4. Extract via Claude Code or `segment.py`
5. Write results via `writer.py`

**Regenerate outputs after data changes:**
```bash
capex export -o workbook/capex_tracker.xlsx
capex chart
git add charts/ workbook/ data/db/ && git commit && git push
```
