"""Validation Layer E — Eval agent (placeholder).

Last-resort LLM-based audit. Reads extraction rows flagged as needing a
second look, re-reads the source span at the claimed locator, and answers
a narrow yes/no question:

    "Does the quote at locator X support the claim Y? supported / contradicted / not_in_span"

Design constraints:
    - Must use a different model family from the extractor to avoid shared biases.
    - Prompts must be narrow and mechanical. No open-ended 'is this analysis good?'.
    - Verdict is written to the `eval_agent_verdict` input column of the workbook.
    - Blocking vs flagging behavior is a deferred policy decision (SYSTEM_DESIGN §9).
"""
from __future__ import annotations


def audit(claim: str, quote: str, source_span: str) -> str:
    """Placeholder. Not yet implemented."""
    raise NotImplementedError("Eval agent not yet implemented.")
