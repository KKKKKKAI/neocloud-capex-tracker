"""Layer 5 — Workbook write adapter.

openpyxl-based wrapper that writes agent-produced rows into the designated
input cells of the live workbook. Never touches formula cells, never
restructures sheets, never modifies the checks / derived_metrics /
dashboard / schema / golden sheets.

See docs/SYSTEM_DESIGN.md §6 for workbook design and write discipline.
"""
