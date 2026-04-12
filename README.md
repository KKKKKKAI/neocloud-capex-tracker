# neocloud-capex-tracker

Automated tracker for AI-related capital expenditure and cloud revenue
disclosures across major hyperscalers and neocloud providers.

![Cloud Revenue](charts/cloud_revenue_annual.png)

## What this is

A data pipeline that watches for new quarterly and annual filings from
key AI infrastructure spenders, extracts financial metrics with full
provenance using LLMs, validates against SEC XBRL data, and publishes
results in a queryable SQLite database + Excel workbook.

**Currently tracking 13 companies** — 5 US hyperscalers (MSFT, AMZN,
GOOGL, META, ORCL), 4 Chinese hyperscalers (BABA, BIDU, GDS, Tencent),
and 4 pure-play AI neoclouds (CRWV, APLD, IREN, NBIS).

**1,189 data points** extracted via SEC XBRL, covering quarterly headline
financials (revenue, capex, OCF, D&A, PP&E) since 2015, plus cloud
segment revenue from the latest annual reports.

## Key findings (FY2023–FY2025)

| Metric | FY2023 | FY2024 | FY2025 | YoY |
|---|---|---|---|---|
| **Aggregated cloud/DC revenue** | $214B | $262B | $324B | +24% |
| **MSFT Intelligent Cloud** | $73B | $87B | $106B | +21% |
| **AMZN AWS** | $91B | $108B | $129B | +20% |
| **GOOGL Google Cloud** | $33B | $43B | $59B | +36% |
| **MSFT capex** | $28B | $44B | $65B | +45% |
| **GOOGL capex** | $32B | $53B | $91B | +74% |
| **META capex** | $27B | $37B | $70B | +87% |
| **CoreWeave revenue** | $229M | $1.9B | $5.1B | +168% |

## Status

**MVP operational.** The full pipeline works end-to-end:

```
capex fetch MSFT 10-K          # fetch latest filing from SEC EDGAR
capex organize                 # canonical naming + DB row
capex extract MSFT             # dry-run: show sections + metrics
capex export                   # generate Excel workbook
```

- **Phase 1** ✅ SQLite DB foundation
- **Phase 2** ✅ SEC EDGAR + HKEXnews fetchers (all 13 companies)
- **Phase 3** ✅ Read + extract + query pipeline, XBRL time series, FX normalization
- **Phase 4** ✅ Excel export (8-sheet workbook), data quality flags
- **Phase 3.5** 🔄 Cloud segment revenue extraction, AI-capex isolation (in progress)

See `docs/RESTRUCTURING_PLAN.md` for the full roadmap.

## Architecture

Six layers, each independently testable. The SQLite database is the
system of record for extracted data; the `_sources/` archive holds the
original filing bytes.

1. **Fetch** — `capex fetch <TICKER> <FORM>` pulls from SEC EDGAR or HKEXnews
2. **Organize** — `capex organize` applies canonical naming (`[dd.mm.yyyy][TICKER][PERIOD][FORM]`)
3. **Store** — SQLite DB + auto-generated `dump.sql` for diff-friendly PR reviews
4. **Extract** — LLM reads filing sections, extracts metrics with verbatim provenance quotes
5. **Query** — `query_metric("MSFT", "capex")` → cached result with source reference
6. **Export** — `capex export` → 8-sheet Excel workbook with annual + quarterly series

## Companies tracked

| Category | Companies | Source | Quarterly? |
|---|---|---|---|
| US Hyperscalers | MSFT, AMZN, GOOGL, META, ORCL | SEC EDGAR (10-K, 10-Q) | Yes |
| Chinese Hyperscalers | BABA, BIDU, GDS | SEC EDGAR (20-F) | Annual only |
| HK-only | Tencent (0700) | HKEXnews (HK-AR, HK-IR) | Semi-annual |
| Pure-play Neoclouds | CRWV, APLD, IREN, NBIS | SEC EDGAR | Varies |

## Getting started

```bash
pip install -e ".[export]"

# Initialize DB + sync company/metric registries
capex db sync-all

# Fetch a filing
capex fetch MSFT 10-K
capex organize

# Generate the Excel workbook
capex export
# → workbook/capex_tracker.xlsx (8 sheets, annual + quarterly)
```

## Repository layout

```
├── charts/                  # generated visualizations (PNG)
├── data/
│   ├── _sources/            # filing archive (gitignored, ~1GB)
│   ├── db/capex.db          # SQLite database (1,189 extractions)
│   ├── db/dump.sql          # auto-generated SQL dump
│   └── seeds/               # YAML configs (coverage, metrics)
├── docs/                    # architecture + plan docs
├── skills/                  # Claude Code skill contracts
├── src/capex/               # the Python package
│   ├── fetch/               # SEC + HKEX fetchers
│   ├── read/                # filing text + section parser
│   ├── extract/             # LLM extraction writer + prompts
│   ├── xbrl/                # XBRL time series from SEC API
│   ├── fx/                  # FX rate normalization
│   ├── exporters/           # Excel workbook generator
│   ├── db/                  # SQLite + migrations
│   └── cli/                 # command-line interface
├── tests/
└── workbook/                # generated Excel output
```

## License

TBD. No license is set yet; all rights reserved until one is chosen.
