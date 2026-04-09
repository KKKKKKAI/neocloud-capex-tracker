"""Layer 2 — Ingestion.

Downloads raw filings, normalizes them into canonical text with stable
page/section IDs, computes SHA-256 of the source document, and stores the
artifact. No LLM calls in this layer.

See docs/SYSTEM_DESIGN.md §4 for layer contract.
"""
