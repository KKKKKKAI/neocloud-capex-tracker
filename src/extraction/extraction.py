"""Extraction entry point — placeholder.

Responsibilities (when implemented):
    - Accept an IngestedDocument and a target schema.
    - Call the configured model backend via src.adapters.
    - Parse the model output against the protocol schema (hard gate).
    - Return a list of ExtractionRecord objects with full provenance.

Design notes:
    - This function is the ONLY place in the pipeline that talks to LLMs for
      extraction (eval agent is separate). Keeping LLM calls isolated to one
      layer is the single most important maintainability decision in this project.
    - Prompts must be model-agnostic. Per-model formatting quirks live in adapters.
    - Retries on schema-validation failure: at most one re-prompt with the parse
      error echoed back.
"""
from __future__ import annotations

from typing import Any


def extract(
    document: Any,  # IngestedDocument
    schema: Any,    # protocol.Schema
    backend: str = "claude",
) -> list[Any]:
    """Placeholder. Not yet implemented."""
    raise NotImplementedError("Extraction layer is not yet implemented.")
