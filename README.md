# neocloud-capex-tracker

Automated tracker for AI-related capital expenditure disclosures across major
hyperscalers and neocloud providers.

## What this is

A collaborative project that watches for new quarterly and annual filings from
key AI infrastructure spenders (hyperscalers and pure-play neoclouds), extracts
capex figures and supporting commentary using LLMs under a strict interchange
protocol, validates every figure against the source document, and stores the
results in a SQLite database that can be queried ad-hoc or exported into Excel,
CSV, JSON, or any other format on demand.

The goal is to quantify both the **volume** and the **efficiency** of
AI-related capex spending over time, with every figure fully traceable to a
specific quote in a specific source filing.

## Status

**v0.6 foundation landed.** Phase 1 of the restructuring is complete: the
SQLite storage trunk is live, the migrator is working, the YAML-seeded
`companies` and `metric_definitions` tables sync cleanly, and the
auto-generated `dump.sql` gives every DB-mutating commit a diff-friendly
audit trail.

No extraction or fetch logic is implemented yet — those land in Phases 2
and 3. See `docs/RESTRUCTURING_PLAN.md` for the phased rollout and
`docs/SYSTEM_DESIGN.md` for the current architecture.

## Architecture at a glance

Six layers, each independently testable and swappable. The SQLite database
is the single system of record; everything upstream writes to it and
everything downstream reads from it.

1. **Source acquisition** — `fetch-company-report` skill pulls authoritative filings from SEC EDGAR or HKEXnews into an immutable `_raw/` archive.
2. **Canonicalization** — `organize-sources` skill renames into `<TICKER>/<YYYY>/[dd.mm.yyyy][TICKER][PERIOD][FORM].pdf`.
3. **Storage trunk** — SQLite at `data/db/capex.db` with an auto-generated `dump.sql` sibling for diff-ability.
4. **Read + extract** — `read-and-extract` skill is a worker that opens one PDF per subagent context, extracts structured rows with full provenance, and writes them to the DB.
5. **Query / lookup** — `query-line-item` skill is the user front door. Resolves a natural-language question against the extractions cache, falls back to `read-and-extract` on a miss, returns the value with its source quote and page.
6. **Export** — `exporters/` renders the DB into Excel, CSV, JSON, or any future format. Excel is no longer the engine; it's just one of several read-only views.

Full details in `docs/SYSTEM_DESIGN.md`.

## Repository layout

```
.
├── .github/workflows/                  # scheduled jobs, CI
├── data/
│   ├── _sources/                       # immutable source archive
│   │   └── _identity.yaml              # authoritative company registry
│   ├── db/
│   │   ├── capex.db                    # SQLite runtime trunk
│   │   └── dump.sql                    # auto-generated SQL dump (diff-friendly)
│   └── seeds/
│       └── metric_definitions.yaml     # canonical metric registry
├── docs/
│   ├── SYSTEM_DESIGN.md                # authoritative architecture doc
│   ├── RESTRUCTURING_PLAN.md           # phased rollout tracker
│   └── neocloud_capex_tracker_design_memo.pdf  # v0.5, historical
├── skills/
│   ├── fetch-company-report/
│   └── organize-sources/
├── src/capex/                          # the importable package
│   ├── fetch/         organize/        read/         extract/
│   ├── adapters/      protocol/        db/           query/
│   ├── validation/    exporters/       cli/
│   └── db/
│       ├── schema.py                   # Database wrapper + migrator
│       ├── dump.py                     # binary → SQL dump utility
│       ├── sync.py                     # YAML → DB sync functions
│       └── migrations/0001_init.sql
├── tests/
└── pyproject.toml
```

## Getting started

The DB layer is runnable today. Everything else is Phase 2+.

```bash
# Install (minimal, just what Phase 1 needs)
pip install -e .

# Apply migrations + sync both YAML-seeded tables
capex db sync-all

# Inspect the database
sqlite3 data/db/capex.db "SELECT * FROM companies"
sqlite3 data/db/capex.db "SELECT key, label FROM metric_definitions"
sqlite3 data/db/capex.db "SELECT * FROM audit_log"
```

After any DB-mutating command, `data/db/dump.sql` regenerates
automatically. Review that file in PRs to see exactly what changed.

## Contributing

Collaboration protocol is still being defined. Please do not open PRs with
implementation code until the protocol is agreed and posted here. For
exploration and feedback, see `docs/RESTRUCTURING_PLAN.md` and open an
issue.

## License

TBD. No license is set yet; all rights reserved until one is chosen.
