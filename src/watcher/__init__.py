"""Layer 1 — Watcher.

Scheduled job that polls SEC EDGAR and non-US IR pages to detect newly
published filings. Zero LLM cost. Emits `NewFilingEvent` records which the
ingestion layer consumes.

See docs/SYSTEM_DESIGN.md §4 for layer contract.
"""
