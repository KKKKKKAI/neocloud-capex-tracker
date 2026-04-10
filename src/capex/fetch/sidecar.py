"""Sidecar JSON for raw archive entries.

Every file in `data/_sources/<TICKER>/_raw/` has a sibling sidecar JSON
recording where it came from, when, and how. The sidecar lives at
`<file>.fetch.json` (i.e. the original filename plus a `.fetch.json`
suffix — not a replacement extension). This keeps `msft-20250630.htm`
and `msft-20250630.htm.fetch.json` together in `ls` output.

The sidecar is the immutable on-disk archival truth. The `source_documents`
DB row mirrors it for queryability, but if the DB is ever lost, the
sidecars are sufficient to rebuild it via a recovery sweep.

Required fields validated by `validate_sidecar()`:
    raw_path          - repo-relative path to the data file
    sha256            - hex digest of the data file bytes
    source            - 'sec_edgar' or 'hkex'
    source_url        - URL the bytes came from
    form_type         - filing form (10-K, 10-Q, 20-F, HK-AR, HK-IR)
    filing_date       - ISO date the filing was published
    period_of_report  - ISO date of the period the filing covers
    ticker            - companies.ticker
    fetched_at        - ISO timestamp of the fetch
    fetcher_version   - version of the fetcher that produced this
    protocol_version  - protocol version pinned at fetch time

Optional fields:
    accession_number  - SEC only, e.g. '0000950170-25-100235'
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = (
    "raw_path",
    "sha256",
    "source",
    "source_url",
    "form_type",
    "filing_date",
    "period_of_report",
    "ticker",
    "fetched_at",
    "fetcher_version",
    "protocol_version",
)


def sidecar_path_for(data_path: Path) -> Path:
    """Return the sidecar JSON path for a given data file."""
    return data_path.with_name(data_path.name + ".fetch.json")


def write_sidecar(data_path: Path, metadata: dict[str, Any]) -> Path:
    """Write the sidecar JSON next to data_path, atomically.

    Validates required fields before writing. Uses temp + rename so a
    reader never observes a half-written sidecar.
    """
    validate_sidecar(metadata)
    sidecar = sidecar_path_for(data_path)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar.with_name(sidecar.name + ".tmp")
    payload = json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(sidecar)
    return sidecar


def read_sidecar(data_path: Path) -> dict[str, Any]:
    """Read and parse the sidecar JSON for a given data file."""
    sidecar = sidecar_path_for(data_path)
    if not sidecar.exists():
        raise FileNotFoundError(f"sidecar not found for {data_path}: {sidecar}")
    return json.loads(sidecar.read_text(encoding="utf-8"))


def validate_sidecar(metadata: dict[str, Any]) -> None:
    """Raise ValueError if required fields are missing or empty.

    This is a structural check, not a semantic one — it confirms the
    fetcher produced a complete record, not that the record makes sense.
    """
    missing = [f for f in REQUIRED_FIELDS if not metadata.get(f)]
    if missing:
        raise ValueError(f"sidecar missing required fields: {missing}")
