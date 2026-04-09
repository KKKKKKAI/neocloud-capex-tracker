"""Read-only exporters that render the DB into external formats.

Excel is one of several — CSV, JSON, and Parquet are trivial once the DB
is the trunk. The openpyxl writer is no longer load-bearing.

Implementation lands in Phase 4.
"""
