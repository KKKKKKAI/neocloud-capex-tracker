"""LLM re-verification of mechanically-flagged audit cells.

For each flagged cell, invoke an LLM (via capex.adapters.cli_backend) with
the filing text + the specific value to re-confirm. LLM returns a JSON
verdict that is written to audit_verdicts.

This is a scaffold — the CLIBackend call returns a default 'UNCERTAIN'
when no LLM CLI tool is available on the host (no `claude` / `codex` /
etc. on PATH). The prompt template lives at prompts/reverify.md.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "reverify.md"


def _load_prompt() -> str:
    if PROMPT_PATH.exists():
        return PROMPT_PATH.read_text()
    return (
        "You are auditing a financial data point. Given the filing text "
        "and the reported value, answer: is the value correct? Return JSON "
        "{verdict: PASS|FAIL|UNCERTAIN, found_value, delta_pct, explanation}."
    )


def reverify(cells, run_id: str, apply: bool = False) -> dict:
    """Scaffold: accumulates flagged cells + reports what would be asked.
    Real LLM invocation requires `claude` or compatible CLI installed."""
    flagged = [c for c in cells if c.classification == "flagged"]
    prompt = _load_prompt()
    results = {"total": len(flagged), "pass": 0, "fail": 0, "uncertain": 0}
    if not flagged:
        return results
    try:
        from ..adapters.cli_backend import CLIBackend
        backend = CLIBackend.auto()
    except Exception as exc:
        print(f"  (LLM backend unavailable: {exc}; skipping)")
        return results

    for cell in flagged[:20]:  # cap to first 20 to keep runs fast
        user = (
            f"Ticker: {cell.ticker}\n"
            f"Metric: {cell.metric_key}\n"
            f"Fiscal year: {cell.fiscal_year}\n"
            f"Period type: {cell.period_type}\n"
            f"Value (USD M): {cell.value_usd}\n"
            f"Extracting model: {cell.extracting_model}\n"
            f"Checks that failed: "
            f"{[r.check_name for r in cell.check_results if not r.passed]}\n"
        )
        try:
            resp = backend.extract(prompt, user)
            parsed = json.loads(resp)
            verdict = parsed.get("verdict", "UNCERTAIN")
        except Exception:
            verdict = "UNCERTAIN"
        cell.llm_verdict = verdict
        results[verdict.lower()] = results.get(verdict.lower(), 0) + 1
    return results
