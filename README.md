# neocloud-capex-tracker

Automated tracker for AI-related capital expenditure disclosures across major
hyperscalers and neocloud providers.

## What this is

A collaborative project that watches for new quarterly and annual filings from
key AI infrastructure spenders (hyperscalers and pure-play neoclouds), extracts
capex figures and supporting commentary using LLMs under a strict interchange
protocol, validates every figure against the source document, and publishes the
results into a shared Excel workbook stored in this repository.

The goal is to quantify both the **volume** and the **efficiency** of AI-related
capex spending over time, with every figure fully traceable to a specific quote
in a specific source filing.

## Status

Early scaffold. No extraction logic is implemented yet. The current repository
contains the directory shell, placeholder modules for each architectural layer,
and the system design memo that governs what gets built next.

Read `docs/SYSTEM_DESIGN.md` for the current architecture summary and
`docs/neocloud_capex_tracker_design_memo.pdf` for the full pre-implementation
design memo.

## Architecture at a glance

Nine layers, each independently testable and swappable. Data flows top to
bottom; the Excel workbook serves simultaneously as storage, validation engine,
and presentation surface.

1. **Watcher** — scheduled job detecting newly published filings
2. **Ingestion** — downloads, normalizes, and canonicalizes source documents
3. **Extraction** — model-agnostic LLM interface producing structured rows
4. **Workbook (template + live)** — Excel template defining structure and rules
5. **Workbook write adapter** — openpyxl, writes only to designated input cells
6. **Formula evaluation pass** — LibreOffice headless recalc
7. **Validation pipeline** — schema, provenance, rules, eval agent, triangulation
8. **Storage / distribution** — GitHub repo + Git LFS for raw filings
9. **Optional human interface** — Claude for Excel as an ad-hoc analyst surface

## Repository layout

```
.
├── .github/workflows/     # scheduled jobs, CI
├── src/
│   ├── watcher/           # filing detection
│   ├── ingestion/         # download + normalize
│   ├── extraction/        # LLM extraction layer
│   ├── validation/        # schema / provenance / rules / eval agent
│   ├── adapters/          # model backends (Claude, Gemini, etc.)
│   ├── workbook/          # openpyxl write adapter
│   ├── protocol/          # interchange schema, versioning
│   └── cli/               # entrypoints
├── tests/
│   ├── schema/            # Layer A: schema tests
│   ├── golden/            # Layer B: regression fixtures
│   ├── unit/
│   └── integration/
├── workbook/              # Excel template + live workbook
├── data/
│   ├── csv/               # diff-friendly mirror, auto-regenerated
│   └── sources/           # raw filings (to be Git LFS tracked)
├── docs/                  # design memo and system design doc
└── scripts/               # bootstrap and utility scripts
```

## Getting started

This project is not runnable yet. For now, the right entry point is reading
`docs/SYSTEM_DESIGN.md` and the full design memo PDF.

## Contributing

Collaboration protocol is still being defined. Please do not open PRs with
implementation code until the protocol is agreed and posted here.

## License

TBD. No license is set yet; all rights reserved until one is chosen.
