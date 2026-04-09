"""Workbook writer — placeholder.

Responsibilities (when implemented):
    - Open the live workbook with openpyxl, preserving formulas.
    - Append agent-produced rows to the `source_data` table via structured
      references (never by raw cell coordinates).
    - Append an entry to the `audit_log` table for every write operation.
    - Regenerate the CSV mirror in data/csv/ after each write.
    - Invoke LibreOffice headless to recalculate formulas before any read.
    - Re-open the workbook in data_only mode and read back `all_checks_pass`
      and related check columns for downstream consumers.

Never writes to: checks, derived_metrics, dashboard, schema, golden.
"""
from __future__ import annotations

from typing import Any


def append_source_row(row: dict[str, Any]) -> None:
    """Placeholder. Not yet implemented."""
    raise NotImplementedError("Workbook writer not yet implemented.")


def recalc_formulas() -> None:
    """Invoke LibreOffice headless to force formula recalculation."""
    raise NotImplementedError("Formula recalc step not yet implemented.")
