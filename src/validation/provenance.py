"""Validation Layer C — Provenance verification (placeholder).

For every extracted field, confirm that the claimed `quote` substring
actually exists verbatim in the source document at the claimed `locator`.
This is a mechanical, deterministic check with no LLM involvement.
Fabricated quotes are the single most common extraction failure mode;
this layer must never be skipped.
"""
from __future__ import annotations


def verify_quote(quote: str, source_text: str, locator: str | None = None) -> bool:
    """Placeholder. Not yet implemented."""
    raise NotImplementedError("Provenance verifier not yet implemented.")
