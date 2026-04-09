"""User-facing line-item lookup.

Resolves a question like `{ticker, period, line_item}` against the
extractions cache, falls back to read-and-extract on cache miss, and
returns the value with full provenance.

Implementation lands in Phase 3.
"""
