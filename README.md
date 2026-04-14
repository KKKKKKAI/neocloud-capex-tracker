# neocloud-capex-tracker

Automated tracker for AI-related capital expenditure and cloud revenue
disclosures across major hyperscalers and neocloud providers.

![Cloud Revenue](charts/cloud_revenue_annual.png)

**[Interactive Chart](https://KKKKKKAI.github.io/neocloud-capex-tracker/)** | **[Download Excel](workbook/capex_tracker_v18.xlsx)**

---

## What this is

A data pipeline that pulls quarterly and annual filings from SEC EDGAR
and HKEXnews, extracts financial metrics with full provenance, validates
every data point, and outputs an auditable Excel workbook where every
cell has a Shift+F2 citation linking to the exact filing, section, and
line item the number came from.

**13 companies** tracked. **1,455 data points** extracted.
**267 quarterly revenue** series across 12 companies.

## Data verification methods

Every extraction goes through a verification pipeline appropriate to
its source:

| Method | Records | How it works |
|---|---|---|
| **XBRL structured data** | 1,257 | Machine-readable SEC data. Values pulled from `data.sec.gov/api/xbrl/companyfacts/`. No LLM needed, no hallucination risk. Cross-checked against filing text. |
| **LLM + dual-agent verification** | 167 | Agent A reads the full filing and extracts value + context excerpts. Agent B receives ONLY the excerpts (not A's answer) and independently deduces the value. If A and B match: verified. If they disagree: refused, queued for human review. |
| **6-K press release extraction** | 31 | Quarterly revenue from 20-F filers' earnings press releases (6-K filings). Extracted via LLM, verified against filing text with provenance stored. |

**92 extractions** have passed dual-agent verification with evidence
stored in the `extraction_evidence` table. All citations in the Excel
workbook link to the exact SEC EDGAR or HKEXnews filing URL.

### Dual-agent verification flow

```
Agent A (Extractor)                    Agent B (Blind Verifier)
  Input:  Full filing + metric           Input:  ONLY A's excerpts
  Output: value + context excerpts       Output: independently deduced value
                                         (never sees A's answer)
              |                |
              +-- compare A,B -+
              |                |
        Match (exact/approx)    Mismatch
              |                     |
     Write to DB + store       Refuse to write
     evidence as citation      Queue for human review
```

## Companies tracked

| Category | Companies | Filing source | Quarterly |
|---|---|---|---|
| US Hyperscalers | MSFT, AMZN, GOOGL, META, ORCL | SEC EDGAR 10-K/10-Q | Yes (XBRL) |
| Chinese Hyperscalers | BABA, BIDU, GDS | SEC EDGAR 20-F/6-K | Yes (LLM from 6-K press releases) |
| HK-listed | Tencent (0700) | HKEXnews HK-AR | Annual only |
| Neoclouds | CRWV, APLD, IREN, NBIS | SEC EDGAR 10-K/10-Q/20-F | Varies |

## Metrics extracted

| Metric | Source | Coverage |
|---|---|---|
| Total revenue | XBRL + 6-K press releases | Annual + quarterly, 2015-2025 |
| Capital expenditures | XBRL | Annual + quarterly, 2015-2025 |
| Cloud/datacenter segment revenue | LLM extraction from filings | Annual, 2015-2025 |
| Operating cash flow | XBRL | Annual + quarterly, 2015-2025 |
| Depreciation & amortization | XBRL | Annual + quarterly, 2015-2025 |
| Property, plant & equipment (net) | XBRL | Annual + quarterly, 2015-2025 |

## Repository structure

```
src/capex/
  fetch/                  SEC EDGAR + HKEXnews filing downloaders
  read/                   HTML/PDF text extraction + section parsing
  extract/
    router.py             Unified extraction entry point
    coverage.py           coverage.yaml programmatic reader
    decumulate.py         Canonical quarterly de-cumulation
    extractors/           XBRL, segment regex, 6-K parser, LLM backends
    writer.py             DB writer with FX normalization + audit log
  verification/
    dual_agent.py         Dual-agent verification (hallucination prevention)
    evidence.py           Extraction evidence storage + retrieval
    comparator.py         Value comparison with tolerance
    prompts/              Agent A + Agent B prompt templates
  validation/
    checks.py             Range plausibility, YoY outlier checks
  xbrl/                   SEC XBRL companyfacts API time series
  fx/                     FX rate normalization (ECB via frankfurter.app)
  exporters/
    excel.py              Excel workbook generator (all values in USD)
    citations.py          Cell-level source citations for Shift+F2
    interactive_chart.py  Plotly HTML chart for GitHub Pages
    charts.py             Static PNG chart generator
  db/                     SQLite schema + migrations
  cli/                    Command-line interface

data/
  db/capex.db             SQLite database (system of record)
  db/dump.sql             Auto-generated SQL dump (for PR review)
  seeds/
    coverage.yaml         Per-company extraction treatments
    metric_definitions.yaml  Canonical metric registry
    chart_config.yaml     Chart visual standards

workbook/                 Generated Excel output (download above)
docs/                     GitHub Pages (interactive chart)
charts/                   Generated PNG charts
```

## CLI commands

```bash
capex db sync-all                    # initialize DB from YAML configs
capex fetch MSFT 10-K                # download filing from SEC EDGAR
capex extract MSFT --metric revenue  # extract via unified router
capex extract --batch                # batch extract all companies
capex review                         # show items pending human verification
capex export                         # generate Excel workbook
capex chart --interactive            # regenerate charts + GitHub Pages
```

## Getting started

```bash
pip install -e ".[export]"
capex db sync-all
capex extract --batch --metric revenue
capex export
```

## License

TBD. All rights reserved until a license is chosen.
