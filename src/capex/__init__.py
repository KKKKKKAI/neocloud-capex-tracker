"""neocloud-capex-tracker core package.

The architecture is six layers (see docs/SYSTEM_DESIGN.md):

    1. fetch/       — source acquisition (SEC EDGAR, HKEXnews)
    2. organize/    — canonicalization of the source archive
    3. db/          — SQLite storage trunk (the system of record)
    4. read/ + extract/ + adapters/ — per-PDF extraction workers
    5. query/       — user-facing line-item lookup
    6. exporters/   — Excel, CSV, JSON outputs derived from the DB

Cross-cutting modules:

    protocol/       — versioned Pydantic interchange schemas
    validation/     — provenance + consistency checks
    cli/            — command-line entrypoints
"""
from __future__ import annotations

__version__ = "0.0.1"
PROTOCOL_VERSION = "0.1.0-draft"

__all__ = ["__version__", "PROTOCOL_VERSION"]
